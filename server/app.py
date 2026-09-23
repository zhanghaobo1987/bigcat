"""
bigcat server.

A Komari-compatible VPS monitoring server implemented with Flask.
Serves the LuminaPlus theme frontend and exposes the same public API
surface Komari themes expect:

  POST /api/rpc2                      JSON-RPC 2.0 over HTTP (methods below)
  WS   /api/rpc2                      JSON-RPC 2.0 over WebSocket (theme prefers WS)
  GET  /api/nodes                     node list (legacy REST)
  GET  /api/public                    public site settings
  GET  /api/version                   server version
  GET  /api/recent/<uuid>             recent reports of a node
  GET  /api/records/load             history records (uuid, load_type, hours)

  POST /api/agent/register            register a node, returns uuid+token
  POST /api/agent/report              agent metric report (token auth)
  POST /api/agent/basicinfo           agent static info (token auth)

  POST /api/admin/login               admin login
  *    /api/admin/...                 node management (admin auth)

JSON-RPC methods implemented:
  public:getNodesInformation, public:getPublicSettings, public:getVersion,
  public:getMe, public:queryMetrics, public:getClientRecentRecords,
  public:getRecordsByUUID, public:listMetricDefinitions
"""
import functools
import json
import secrets
import time
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, request, send_from_directory, session
from flask_cors import CORS

from storage import Storage

VERSION = "1.0.0"
VERSION_HASH = "bigcat"

STATIC_DIR = "static"

# Komari metric definitions (internal/metricstore)
METRIC_DEFS = {
    "cpu.usage":        {"type": "gauge",   "unit": "%",     "description": "CPU usage percentage"},
    "gpu.usage":        {"type": "gauge",   "unit": "%",     "description": "GPU usage percentage"},
    "memory.used":      {"type": "gauge",   "unit": "bytes", "description": "RAM used"},
    "swap.used":        {"type": "gauge",   "unit": "bytes", "description": "Swap used"},
    "load.average":     {"type": "gauge",   "unit": "",      "description": "System load average"},
    "disk.used":        {"type": "gauge",   "unit": "bytes", "description": "Disk used"},
    "net.in.rate":      {"type": "gauge",   "unit": "bytes/s","description": "Network in rate"},
    "net.out.rate":     {"type": "gauge",   "unit": "bytes/s","description": "Network out rate"},
    "net.total.up":     {"type": "counter", "unit": "bytes", "description": "Network total upload"},
    "net.total.down":   {"type": "counter", "unit": "bytes", "description": "Network total download"},
    "traffic.up":       {"type": "gauge",   "unit": "bytes", "description": "Traffic upload delta"},
    "traffic.down":     {"type": "gauge",   "unit": "bytes", "description": "Traffic download delta"},
    "process.count":    {"type": "gauge",   "unit": "count", "description": "Process count"},
    "connections.tcp":  {"type": "gauge",   "unit": "count", "description": "TCP connections"},
    "connections.udp":  {"type": "gauge",   "unit": "count", "description": "UDP connections"},
}

# record column -> metric key
RECORD_METRIC_MAP = {
    "cpu": "cpu.usage",
    "gpu": "gpu.usage",
    "ram": "memory.used",
    "swap": "swap.used",
    "load": "load.average",
    "disk": "disk.used",
    "net_in": "net.in.rate",
    "net_out": "net.out.rate",
    "net_total_up": "net.total.up",
    "net_total_down": "net.total.down",
    "traffic_up": "traffic.up",
    "traffic_down": "traffic.down",
    "process": "process.count",
    "connections": "connections.tcp",
    "connections_udp": "connections.udp",
}


def create_app(db_path: str = "data/bigcat.db", static_dir: str = STATIC_DIR):
    app = Flask(__name__, static_folder=None)
    app.secret_key = secrets.token_hex(32)
    CORS(app)
    store = Storage(db_path)
    app.config["store"] = store
    app.config["static_dir"] = static_dir

    # ------------------------------------------------------------ frontend
    @app.route("/")
    def index():
        return send_from_directory(app.config["static_dir"], "index.html")

    @app.route("/assets/<path:p>")
    def assets(p):
        return send_from_directory(app.config["static_dir"] + "/assets", p)

    @app.route("/images/<path:p>")
    def images(p):
        return send_from_directory(app.config["static_dir"] + "/images", p)

    @app.route("/manifest.json")
    def manifest():
        return send_from_directory(app.config["static_dir"], "manifest.json")

    @app.route("/favicon.ico")
    def favicon():
        # theme references /favicon.ico; fall back to a logo svg if missing
        try:
            return send_from_directory(app.config["static_dir"], "favicon.ico")
        except Exception:
            return send_from_directory(
                app.config["static_dir"] + "/images/logo", "linux.svg"
            )

    # ------------------------------------------------------------ helpers
    def _public_node(c: dict) -> dict:
        """Node info as seen by guests (mirrors komari publicGetNodesInformation)."""
        out = dict(c)
        for k in ("ipv4", "ipv6", "remark", "version", "token"):
            out.pop(k, None)
        return out

    def _parse_time(v):
        if not v:
            return None
        try:
            s = str(v)
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return datetime.fromisoformat(s)
        except Exception:
            return None

    def _iso(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _query_series(metric_key, entity_ids, start, end, max_points):
        """Build queryMetrics-style series from the records table."""
        col = next((c for c, m in RECORD_METRIC_MAP.items() if m == metric_key), None)
        if col is None:
            return []
        series = []
        for eid in entity_ids:
            rows = store.query_records(
                eid,
                _iso(start),
                _iso(end),
                limit=max( max_points * 2, 2000),
            )
            points = [
                {"time": r["time"], "value": float(r[col] or 0), "count": 1}
                for r in rows
            ]
            # downsample to max_points
            if len(points) > max_points and max_points > 0:
                step = len(points) / max_points
                sampled = []
                i = 0.0
                while int(i) < len(points):
                    sampled.append(points[int(i)])
                    i += step
                points = sampled
            series.append(
                {
                    "metric_key": metric_key,
                    "entity_id": eid,
                    "type": METRIC_DEFS[metric_key]["type"],
                    "unit": METRIC_DEFS[metric_key]["unit"],
                    "downsampled": len(rows) > max_points,
                    "fill_empty": False,
                    "max_points": max_points,
                    "count": len(points),
                    "points": points,
                }
            )
        return series

    def _report_from_record(client_uuid, r: dict) -> dict:
        c = store.get_client(client_uuid) or {}
        return {
            "uuid": client_uuid,
            "cpu": {
                "name": c.get("cpu_name", ""),
                "cores": c.get("cpu_cores", 0),
                "arch": c.get("arch", ""),
                "usage": r.get("cpu", 0),
            },
            "ram": {"total": r.get("ram_total", 0), "used": r.get("ram", 0)},
            "swap": {"total": r.get("swap_total", 0), "used": r.get("swap", 0)},
            "load": {
                "load1": r.get("load", 0),
                "load5": r.get("load", 0),
                "load15": r.get("load", 0),
            },
            "disk": {"total": r.get("disk_total", 0), "used": r.get("disk", 0)},
            "network": {
                "up": r.get("net_out", 0),
                "down": r.get("net_in", 0),
                "totalUp": r.get("net_total_up", 0),
                "totalDown": r.get("net_total_down", 0),
            },
            "connections": {
                "tcp": r.get("connections", 0),
                "udp": r.get("connections_udp", 0),
            },
            "uptime": r.get("uptime", 0),
            "process": r.get("process", 0),
            "message": "",
            "updated_at": r.get("time", ""),
        }

    # ------------------------------------------------------------ JSON-RPC
    def rpc_get_nodes_information(params):
        clients = store.list_clients()
        return [_public_node(c) for c in clients if not c.get("hidden")]

    def rpc_get_public_settings(params):
        return store.get_public_settings()

    def rpc_get_version(params):
        return {"version": VERSION, "hash": VERSION_HASH}

    def rpc_get_me(params):
        return {"username": "Guest", "logged_in": False}

    def rpc_list_metric_definitions(params):
        return [
            {"name": k, "type": v["type"], "unit": v["unit"],
             "description": v["description"], "retention_days": 1}
            for k, v in METRIC_DEFS.items()
        ]

    def rpc_query_metrics(params):
        params = params or {}
        keys = params.get("metric_keys") or params.get("metrics") or []
        if params.get("metric_key"):
            keys = [params["metric_key"]] + keys
        if not keys:
            raise ValueError("metric_keys is required")
        entity_ids = params.get("entity_ids") or []
        if params.get("entity_id"):
            entity_ids = [params["entity_id"]] + entity_ids
        if not entity_ids:
            entity_ids = [c["uuid"] for c in rpc_get_nodes_information({})]

        now = datetime.now(timezone.utc)
        end = _parse_time(params.get("end") or params.get("end_time")) or now
        start = _parse_time(params.get("start") or params.get("start_time"))
        if start is None:
            hours = float(params.get("hours") or 4)
            start = end - timedelta(hours=hours)
        max_points = int(params.get("max_points") or 500)

        series = []
        for key in keys:
            if key not in METRIC_DEFS:
                raise ValueError(f"unknown metric key: {key}")
            series.extend(_query_series(key, entity_ids, start, end, max_points))
        return {
            "start": _iso(start),
            "end": _iso(end),
            "server_downsample_default": True,
            "default_points": 500,
            "series": series,
            "count": len(series),
        }

    def rpc_get_client_recent_records(params):
        params = params or {}
        client_uuid = params.get("uuid") or params.get("entity_id") or ""
        if not client_uuid:
            raise ValueError("UUID is required")
        rows = store.query_records(
            client_uuid,
            _iso(datetime.now(timezone.utc) - timedelta(minutes=10)),
            _iso(datetime.now(timezone.utc)),
            limit=600,
        )
        return [_report_from_record(client_uuid, r) for r in rows]

    def rpc_get_records_by_uuid(params):
        params = params or {}
        client_uuid = params.get("uuid", "")
        if not client_uuid:
            raise ValueError("UUID is required")
        hours = float(params.get("hours") or 4)
        now = datetime.now(timezone.utc)
        rows = store.query_records(
            client_uuid, _iso(now - timedelta(hours=hours)), _iso(now), limit=2000
        )
        # legacy Record shape
        return [
            {
                "client": r["client"],
                "time": r["time"],
                "cpu": r["cpu"],
                "ram": r["ram"],
                "ram_total": r["ram_total"],
                "swap": r["swap"],
                "swap_total": r["swap_total"],
                "load": r["load"],
                "disk": r["disk"],
                "disk_total": r["disk_total"],
                "net_in": r["net_in"],
                "net_out": r["net_out"],
                "net_total_up": r["net_total_up"],
                "net_total_down": r["net_total_down"],
                "process": r["process"],
                "connections": r["connections"],
                "connections_udp": r["connections_udp"],
            }
            for r in rows
        ]

    def rpc_get_public_ping_tasks(params):
        return []

    def rpc_get_ping_metric_stats(params):
        now = datetime.now(timezone.utc)
        params = params or {}
        hours = float(params.get("hours") or 1)
        start = _parse_time(params.get("start") or params.get("start_time")) or (now - timedelta(hours=hours))
        end = _parse_time(params.get("end") or params.get("end_time")) or now
        return {"start": _iso(start), "end": _iso(end), "stats": [], "count": 0}

    def rpc_get_ping_records(params):
        return []

    RPC_METHODS = {
        "public:getNodesInformation": rpc_get_nodes_information,
        "public:getPublicSettings": rpc_get_public_settings,
        "public:getVersion": rpc_get_version,
        "public:getMe": rpc_get_me,
        "public:queryMetrics": rpc_query_metrics,
        "public:getClientRecentRecords": rpc_get_client_recent_records,
        "public:getRecordsByUUID": rpc_get_records_by_uuid,
        "public:listMetricDefinitions": rpc_list_metric_definitions,
        "public:getPublicPingTasks": rpc_get_public_ping_tasks,
        "public:getPingMetricStats": rpc_get_ping_metric_stats,
        "public:getPingRecords": rpc_get_ping_records,
        # legacy aliases
        "getNodesInformation": rpc_get_nodes_information,
        "queryMetrics": rpc_query_metrics,
    }

    def _dispatch_rpc(body: dict) -> dict:
        """Shared JSON-RPC 2.0 dispatcher used by both HTTP and WebSocket."""
        req_id = body.get("id")
        method = body.get("method", "")
        params = body.get("params") or {}
        handler = RPC_METHODS.get(method)
        if handler is None:
            return {
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        try:
            result = handler(params)
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except ValueError as e:
            return {
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32602, "message": str(e)},
            }
        except Exception as e:  # noqa: BLE001
            return {
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32603, "message": f"Internal error: {e}"},
            }

    @app.route("/api/rpc2", methods=["POST"])
    def rpc2():
        body = request.get_json(force=True, silent=True) or {}
        return jsonify(_dispatch_rpc(body))

    # WebSocket transport for /api/rpc2 (the theme tries WS first,
    # falls back to HTTP POST). Same JSON-RPC methods.
    try:
        from flask_sock import Sock
        _sock = Sock(app)

        @_sock.route("/api/rpc2")
        def rpc2_ws(ws):
            while True:
                msg = ws.receive()
                if msg is None:
                    break
                try:
                    body = json.loads(msg)
                except Exception:
                    continue
                # support batch requests
                if isinstance(body, list):
                    resp = [_dispatch_rpc(b) for b in body]
                else:
                    resp = _dispatch_rpc(body)
                ws.send(json.dumps(resp))
    except ImportError:
        pass

    # ------------------------------------------------------------ legacy REST
    @app.route("/api/nodes")
    def api_nodes():
        return jsonify(rpc_get_nodes_information({}))

    @app.route("/api/public")
    def api_public():
        return jsonify(rpc_get_public_settings({}))

    @app.route("/api/version")
    def api_version():
        return jsonify(rpc_get_version({}))

    @app.route("/api/me")
    def api_me():
        return jsonify(rpc_get_me({}))

    @app.route("/api/recent/<client_uuid>")
    def api_recent(client_uuid):
        return jsonify(rpc_get_client_recent_records({"uuid": client_uuid}))

    @app.route("/api/records/load")
    def api_records_load():
        return jsonify(rpc_get_records_by_uuid({
            "uuid": request.args.get("uuid", ""),
            "hours": request.args.get("hours", "4"),
        }))

    # ------------------------------------------------------------ agent API
    def _agent_auth():
        token = request.headers.get("Authorization", "")
        if token.startswith("Bearer "):
            token = token[7:]
        if not token:
            token = request.args.get("token", "")
        if not token:
            data = request.get_json(force=True, silent=True) or {}
            token = data.get("token", "")
        if not token:
            return None
        return store.get_client_by_token(token)

    @app.route("/api/agent/register", methods=["POST"])
    def agent_register():
        data = request.get_json(force=True, silent=True) or {}
        client = store.add_client(name=data.get("name", ""))
        return jsonify({"uuid": client["uuid"], "token": client["token"]})

    @app.route("/api/agent/basicinfo", methods=["POST"])
    def agent_basicinfo():
        client = _agent_auth()
        if not client:
            return jsonify({"error": "unauthorized"}), 401
        data = request.get_json(force=True, silent=True) or {}
        info = data.get("info", data)
        fields = {}
        mapping = {
            "cpu_name": "cpu_name", "cpuName": "cpu_name",
            "virtualization": "virtualization",
            "arch": "arch",
            "cpu_cores": "cpu_cores", "cpuCores": "cpu_cores",
            "os": "os", "os_name": "os",
            "kernel_version": "kernel_version",
            "gpu_name": "gpu_name",
            "ipv4": "ipv4", "ipv6": "ipv6",
            "region": "region",
            "mem_total": "mem_total", "memTotal": "mem_total",
            "swap_total": "swap_total",
            "disk_total": "disk_total",
            "version": "version",
        }
        for k, col in mapping.items():
            if k in info:
                fields[col] = info[k]
        if "name" in info and not client.get("name"):
            fields["name"] = info["name"]
        store.update_client(client["uuid"], fields)
        return jsonify({"ok": True})

    @app.route("/api/agent/report", methods=["POST"])
    def agent_report():
        client = _agent_auth()
        if not client:
            return jsonify({"error": "unauthorized"}), 401
        data = request.get_json(force=True, silent=True) or {}
        rep = data.get("report", data)
        cpu = rep.get("cpu", {})
        ram = rep.get("ram", {})
        swap = rep.get("swap", {})
        load = rep.get("load", {})
        disk = rep.get("disk", {})
        net = rep.get("network", {})
        conn = rep.get("connections", {})
        rec = {
            "time": rep.get("updated_at")
            or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "cpu": cpu.get("usage", 0),
            "gpu": (rep.get("gpu") or {}).get("average_usage", 0),
            "ram": ram.get("used", 0),
            "ram_total": ram.get("total", 0),
            "swap": swap.get("used", 0),
            "swap_total": swap.get("total", 0),
            "load": load.get("load1", 0),
            "disk": disk.get("used", 0),
            "disk_total": disk.get("total", 0),
            "net_in": net.get("down", 0),    # bytes/s down -> in
            "net_out": net.get("up", 0),     # bytes/s up -> out
            "net_total_up": net.get("totalUp", 0),
            "net_total_down": net.get("totalDown", 0),
            "traffic_up": net.get("up", 0),
            "traffic_down": net.get("down", 0),
            "process": rep.get("process", 0),
            "connections": conn.get("tcp", 0),
            "connections_udp": conn.get("udp", 0),
            "uptime": rep.get("uptime", 0),
        }
        store.insert_record(client["uuid"], rec)
        store.touch_client(client["uuid"])
        # update totals if agent reports them
        totals = {}
        if ram.get("total"):
            totals["mem_total"] = ram["total"]
        if swap.get("total"):
            totals["swap_total"] = swap["total"]
        if disk.get("total"):
            totals["disk_total"] = disk["total"]
        if cpu.get("cores"):
            totals["cpu_cores"] = cpu["cores"]
        if cpu.get("name"):
            totals["cpu_name"] = cpu["name"]
        if cpu.get("arch"):
            totals["arch"] = cpu["arch"]
        if totals:
            store.update_client(client["uuid"], totals)
        return jsonify({"ok": True})

    # ------------------------------------------------------------ admin API
    def _require_admin(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            if not session.get("admin"):
                return jsonify({"error": "login required"}), 401
            return fn(*a, **kw)
        return wrapper

    @app.route("/api/admin/login", methods=["POST"])
    def admin_login():
        data = request.get_json(force=True, silent=True) or {}
        if store.verify_admin(data.get("password", ""), data.get("username", "")):
            session["admin"] = True
            return jsonify({"ok": True, "username": store.get_admin_username()})
        return jsonify({"error": "invalid username or password"}), 401

    @app.route("/api/admin/logout", methods=["POST"])
    def admin_logout():
        session.pop("admin", None)
        return jsonify({"ok": True})

    @app.route("/api/admin/status")
    def admin_status():
        return jsonify({
            "has_admin": store.has_admin(),
            "logged_in": bool(session.get("admin")),
            "nodes": len(store.list_clients()),
        })

    @app.route("/api/admin/clients")
    @_require_admin
    def admin_clients():
        return jsonify(store.list_clients())

    @app.route("/api/admin/client/add", methods=["POST"])
    @_require_admin
    def admin_client_add():
        data = request.get_json(force=True, silent=True) or {}
        client = store.add_client(name=data.get("name", ""))
        return jsonify(client)

    @app.route("/api/admin/client/<client_uuid>", methods=["POST"])
    @_require_admin
    def admin_client_edit(client_uuid):
        data = request.get_json(force=True, silent=True) or {}
        ok = store.update_client(client_uuid, data)
        return jsonify({"ok": ok})

    @app.route("/api/admin/client/<client_uuid>", methods=["DELETE"])
    @_require_admin
    def admin_client_delete(client_uuid):
        return jsonify({"ok": store.delete_client(client_uuid)})

    @app.route("/api/admin/settings", methods=["GET", "POST"])
    @_require_admin
    def admin_settings():
        if request.method == "GET":
            return jsonify(store.get_public_settings())
        data = request.get_json(force=True, silent=True) or {}
        for k in ("sitename", "description", "theme"):
            if k in data:
                store.set_setting(k, str(data[k]))
        return jsonify({"ok": True})

    return app


def main():
    import argparse

    ap = argparse.ArgumentParser(description="bigcat server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=25774)
    ap.add_argument("--db", default="data/bigcat.db")
    ap.add_argument("--set-admin", metavar="USER:PASS",
                    help="set (or reset) the admin account and exit; "
                         "format 'username:password', or plain 'password' "
                         "(username keeps existing value, default admin)")
    args = ap.parse_args()

    app = create_app(db_path=args.db)
    store: Storage = app.config["store"]
    if args.set_admin:
        if ":" in args.set_admin:
            username, password = args.set_admin.split(":", 1)
            store.set_admin(username.strip() or "admin", password)
        else:
            store.set_admin_password(args.set_admin)
        print(f"admin account updated (username: {store.get_admin_username()})")
        return
    if not store.has_admin():
        print("NOTE: no admin account set. Run with --set-admin <user:pass> first.")
    print(f"bigcat server v{VERSION} listening on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
