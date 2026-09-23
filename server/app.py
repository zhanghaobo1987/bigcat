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
import os
import re
import secrets
import socket
import base64
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from importlib.metadata import version as _pkg_version

from flask import Flask, jsonify, request, send_from_directory, session
from flask_cors import CORS

from storage import Storage

def _read_version() -> str:
    # bigcat 版本号唯一来源：server/VERSION 文件，每次修改按 semver 递增
    try:
        with open(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION"),
            encoding="utf-8",
        ) as f:
            return f.read().strip() or "unknown"
    except Exception:
        return "unknown"


VERSION = _read_version()
VERSION_HASH = "bigcat"

STATIC_DIR = "static"

# 进程启动时间（用于 /api/admin/about 的运行时长）
STARTED_AT = time.time()

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


# ------------------------------------------------------------ TOTP (2FA, stdlib only)
def _new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode()


def _totp_verify(secret_b32: str, code, window: int = 1) -> bool:
    """Verify a 6-digit TOTP code (RFC 6238, SHA1, 30s step)."""
    import hmac
    import hashlib
    import struct

    try:
        key = base64.b32decode(secret_b32.strip().upper())
    except Exception:
        return False
    want = str(code).strip()
    if not want.isdigit():
        return False
    t = int(time.time()) // 30
    for dt in range(-window, window + 1):
        msg = struct.pack(">Q", t + dt)
        h = hmac.new(key, msg, hashlib.sha1).digest()
        o = h[-1] & 0x0F
        c = struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF
        if str(c % 10 ** 6).zfill(6) == want:
            return True
    return False


def create_app(db_path: str = "data/bigcat.db", static_dir: str = STATIC_DIR,
               enable_monitor: bool = True):
    app = Flask(__name__, static_folder=None)
    app.secret_key = secrets.token_hex(32)
    CORS(app)
    store = Storage(db_path)
    app.config["store"] = store
    app.config["static_dir"] = static_dir

    # ------------------------------------------------------------ themes helpers
    # 前台主题：兼容 Komari 主题包（zip 根目录含 komari-theme.json，内含 dist/）。
    # 上传的主题解压到 <db_dir>/themes/<short>/；"default" 为内置主题（static_dir）。
    import shutil as _shutil
    import zipfile as _zipfile

    THEMES_DIR = os.path.join(os.path.dirname(os.path.abspath(db_path)), "themes")
    os.makedirs(THEMES_DIR, exist_ok=True)
    app.config["themes_dir"] = THEMES_DIR

    _THEME_SHORT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
    _MAX_THEME_FILES = 10000
    _MAX_THEME_FILE_SIZE = 128 << 20
    _MAX_THEME_TOTAL_SIZE = 512 << 20

    def _theme_manifest(short):
        if not _THEME_SHORT_RE.match(short or ""):
            return None
        p = os.path.join(THEMES_DIR, short, "komari-theme.json")
        try:
            with open(p, encoding="utf-8") as f:
                m = json.load(f)
            if not isinstance(m, dict) or not m.get("name") or not m.get("short"):
                return None
            if m.get("short") != short:
                return None
            return m
        except Exception:
            return None

    def _active_theme_short():
        s = (store.get_setting("active_theme", "default") or "default").strip()
        if s != "default" and _theme_manifest(s):
            return s
        return "default"

    def _active_theme_dist():
        s = _active_theme_short()
        if s != "default":
            d = os.path.join(THEMES_DIR, s, "dist")
            if os.path.isfile(os.path.join(d, "index.html")):
                return d
        return app.config["static_dir"]

    # ------------------------------------------------------------ frontend
    @app.route("/")
    def index():
        return send_from_directory(_active_theme_dist(), "index.html")

    @app.route("/assets/<path:p>")
    def assets(p):
        return send_from_directory(_active_theme_dist() + "/assets", p)

    @app.route("/images/<path:p>")
    def images(p):
        return send_from_directory(_active_theme_dist() + "/images", p)

    @app.route("/manifest.json")
    def manifest():
        return send_from_directory(_active_theme_dist(), "manifest.json")

    @app.route("/favicon.ico")
    def favicon():
        # theme references /favicon.ico; fall back to a logo svg if missing
        try:
            return send_from_directory(_active_theme_dist(), "favicon.ico")
        except Exception:
            return send_from_directory(
                app.config["static_dir"] + "/images/logo", "linux.svg"
            )

    # bigcat 自带后台管理页：主题里的"后台登录/管理"按钮链向 /admin
    @app.route("/admin")
    @app.route("/admin/<path:_sub>")
    def admin_panel(_sub=None):
        return send_from_directory(
            os.path.dirname(os.path.abspath(__file__)), "admin.html"
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

    # ============================================================ v3 core
    # 后台监控主循环：离线检测 / 负载告警 / 延迟监测 / 到期提醒 / 流量报告。
    # 取代 v2 只在 agent 上报时搭便车检测的方式（单节点全离线时无上报触发）。
    SESSION_TTL = 30 * 86400

    def _online_threshold() -> int:
        try:
            return max(30, min(3600, int(store.get_setting("offline_threshold", "180") or 180)))
        except Exception:
            return 180

    def _node_online(c: dict, now: datetime) -> bool:
        grace = c.get("offline_grace") or 0
        threshold = grace if grace else _online_threshold()
        t = _parse_time(c.get("last_report_at"))
        if not t:
            return False
        return (now - t).total_seconds() < threshold

    # ---------------- 通知渠道 ----------------
    def _get_channels() -> list:
        try:
            ch = json.loads(store.get_setting("notify_channels", "[]") or "[]")
        except Exception:
            ch = []
        # 兼容 v2 的单 webhook 配置：自动迁移为一个渠道
        legacy_url = (store.get_setting("notify_webhook", "") or "").strip()
        if legacy_url and not any(
            x.get("type") == "webhook" and x.get("config", {}).get("url") == legacy_url
            for x in ch
        ):
            ch.append({
                "name": "默认 Webhook",
                "type": "webhook",
                "enabled": (store.get_setting("notify_enabled", "0") or "0") == "1",
                "config": {"url": legacy_url},
            })
            store.set_setting("notify_channels", json.dumps(ch))
        return ch

    def _save_channels(ch: list):
        store.set_setting("notify_channels", json.dumps(ch))

    def _render_msg(event: str, message: str) -> str:
        tpl = store.get_setting("notify_template", "【{event}】{message}")
        return tpl.replace("{event}", str(event)).replace("{message}", str(message))

    def _send_channel(ch: dict, text: str) -> bool:
        """经单个渠道发送，返回是否成功。"""
        try:
            typ = ch.get("type")
            cfg = ch.get("config", {}) or {}
            if typ == "webhook":
                url = (cfg.get("url") or "").strip()
                if not url:
                    return False
                req = urllib.request.Request(
                    url, data=json.dumps({"text": text}).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return 200 <= resp.status < 300
            if typ == "telegram":
                token = (cfg.get("bot_token") or "").strip()
                chat_id = str(cfg.get("chat_id") or "").strip()
                if not token or not chat_id:
                    return False
                api = (cfg.get("api_base") or "https://api.telegram.org/bot").rstrip("/")
                url = f"{api}{token}/sendMessage"
                body = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
                req = urllib.request.Request(
                    url, data=body, headers={"Content-Type": "application/json"},
                    method="POST")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return 200 <= resp.status < 300
            if typ == "bark":
                key = (cfg.get("key") or "").strip()
                if not key:
                    return False
                server = (cfg.get("server") or "https://api.day.app").rstrip("/")
                url = f"{server}/{key}"
                body = json.dumps({"title": "bigcat", "body": text}).encode("utf-8")
                req = urllib.request.Request(
                    url, data=body, headers={"Content-Type": "application/json"},
                    method="POST")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return 200 <= resp.status < 300
            return False
        except Exception:
            return False

    def _notify(event: str, message: str):
        """经所有启用的渠道发送通知，并写入事件日志。"""
        text = _render_msg(event, message)
        for ch in _get_channels():
            if ch.get("enabled"):
                threading.Thread(target=_send_channel, args=(ch, text), daemon=True).start()
        try:
            store.log_event("info", "notify", f"[{event}] {message}")
        except Exception:
            pass

    # ---------------- 离线检测 ----------------
    def _offline_check():
        now = datetime.now(timezone.utc)
        try:
            prev = json.loads(store.get_setting("notify_state", "{}") or "{}")
        except Exception:
            prev = {}
        cur, changed = {}, False
        for c in store.list_clients():
            online = _node_online(c, now)
            cur[c["uuid"]] = online
            if c["uuid"] in prev and prev[c["uuid"]] != online:
                name = c.get("name") or c["uuid"][:8]
                if online:
                    _notify("上线", f"节点「{name}」恢复在线")
                    store.log_event("info", "node", f"节点「{name}」恢复在线")
                elif c.get("notify_offline", 1):
                    grace = c.get("offline_grace") or _online_threshold()
                    _notify("离线", f"节点「{name}」离线（超过 {grace} 秒未上报）")
                    store.log_event("warn", "node", f"节点「{name}」离线")
            if prev.get(c["uuid"]) != online:
                changed = True
        if changed:
            store.set_setting("notify_state", json.dumps(cur))

    # ---------------- 负载告警 ----------------
    def _rule_metric_value(metric: str, r: dict) -> float:
        try:
            if metric == "cpu":
                return float(r.get("cpu", 0))
            if metric == "ram":
                return float(r.get("ram", 0)) * 100.0 / float(r.get("ram_total") or 1)
            if metric == "disk":
                return float(r.get("disk", 0)) * 100.0 / float(r.get("disk_total") or 1)
        except Exception:
            pass
        return 0.0

    def _alerts_tick():
        now = datetime.now(timezone.utc)
        for rule in store.list_alert_rules():
            if not rule.get("enabled"):
                continue
            try:
                last_fired = _parse_time(rule.get("last_fired_at"))
                if last_fired and (now - last_fired).total_seconds() < int(rule.get("interval_min", 2)) * 60:
                    continue
            except Exception:
                pass
            try:
                targets = json.loads(rule.get("clients") or "[]") or []
            except Exception:
                targets = []
            if not targets:
                targets = [c["uuid"] for c in store.list_clients()]
            window_start = _iso(now - timedelta(minutes=int(rule.get("interval_min", 2))))
            window_end = _iso(now)
            for uuid in targets:
                rows = store.query_records(uuid, window_start, window_end, limit=10000)
                if not rows:
                    continue
                vals = [_rule_metric_value(rule.get("metric", "cpu"), r) for r in rows]
                ratio = sum(1 for v in vals if v > float(rule.get("threshold", 90))) / len(vals)
                if ratio >= float(rule.get("ratio", 0.8)):
                    c = store.get_client(uuid) or {}
                    name = c.get("name") or uuid[:8]
                    _notify("负载告警",
                            f"{rule.get('name')}：节点「{name}」"
                            f"{rule.get('metric')} {ratio * 100:.0f}% 的时间超过 {rule.get('threshold')}%")
                    store.log_event("warn", "alert",
                                    f"负载告警「{rule.get('name')}」触发：{name}")
                    store.set_rule_fired(rule["id"])
                    break

    # ---------------- 延迟监测 ----------------
    def _do_ping(task: dict):
        target = (task.get("target") or "").strip()
        typ = (task.get("type") or "tcp").lower()
        t0 = time.time()
        try:
            if typ == "tcp":
                host, _, port = target.partition(":")
                port = int(port.strip() or 80)
                s = socket.create_connection((host.strip(), port), timeout=5)
                s.close()
            elif typ == "http":
                url = target if target.startswith("http") else "http://" + target
                req = urllib.request.Request(url, headers={"User-Agent": "bigcat-ping/1.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    resp.read(1024)
            elif typ == "icmp":
                host = target.split(":")[0].split("/")[0].strip()
                out = subprocess.run(["ping", "-c1", "-W2", host],
                                     capture_output=True, text=True, timeout=12)
                m = re.search(r"time[=<]([\d.]+)\s*ms", out.stdout)
                if not m:
                    return 0.0, False
                return float(m.group(1)), True
            else:
                return 0.0, False
            return (time.time() - t0) * 1000.0, True
        except Exception:
            return 0.0, False

    def _ping_tick(next_run: dict):
        now = time.time()
        for task in store.list_ping_tasks():
            if not task.get("enabled"):
                continue
            if now < next_run.get(task["id"], 0):
                continue
            latency, ok = _do_ping(task)
            store.insert_ping_result(task["id"], round(latency, 2), ok)
            next_run[task["id"]] = now + max(30, int(task.get("interval_sec") or 300))
        try:
            store.prune_ping_results(72)
        except Exception:
            pass

    # ---------------- 到期提醒 / 流量报告 ----------------
    def _fmt_bytes(n) -> str:
        n = float(n or 0)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024 or unit == "TB":
                return f"{n:.2f} {unit}"
            n /= 1024
        return f"{n:.2f} TB"

    def _expiry_check():
        if (store.get_setting("expiry_remind_enabled", "1") or "1") != "1":
            return
        try:
            days = int(store.get_setting("expiry_remind_days", "10") or 10)
        except Exception:
            days = 10
        today = datetime.now(timezone.utc).date()
        for c in store.list_clients():
            exp_s = (c.get("expired_at") or "").strip()
            if not exp_s:
                continue
            try:
                exp = datetime.fromisoformat(exp_s).date()
            except Exception:
                continue
            delta = (exp - today).days
            if 0 <= delta <= days:
                name = c.get("name") or c["uuid"][:8]
                _notify("到期提醒", f"节点「{name}」还有 {delta} 天到期（{exp.isoformat()}）")
                store.log_event("warn", "expiry", f"节点「{name}」{delta} 天后到期")

    def _traffic_reports_tick():
        today = datetime.now(timezone.utc).date()
        due = {}
        if store.get_setting("last_report_daily") != today.isoformat():
            due["daily"] = (today - timedelta(days=1), today, "日报")
        if today.weekday() == 0 and store.get_setting("last_report_weekly") != today.isoformat():
            due["weekly"] = (today - timedelta(days=7), today, "周报")
        if today.day == 1 and store.get_setting("last_report_monthly") != today.isoformat():
            due["monthly"] = (today - timedelta(days=30), today, "月报")
        if not due:
            return
        now_iso = _iso(datetime.now(timezone.utc))
        for c in store.list_clients():
            if not c.get("report_enabled"):
                continue
            types = [t.strip() for t in (c.get("report_types") or "").split(",") if t.strip()]
            name = c.get("name") or c["uuid"][:8]
            for key, (start_d, end_d, label) in due.items():
                if key not in types:
                    continue
                s = _iso(datetime(start_d.year, start_d.month, start_d.day, tzinfo=timezone.utc))
                t = store.sum_traffic(c["uuid"], s, now_iso)
                _notify("流量报告",
                        f"节点「{name}」{label}：上行 {_fmt_bytes(t['up'])} / "
                        f"下行 {_fmt_bytes(t['down'])}")
        for key in due:
            store.set_setting(f"last_report_{key}", today.isoformat())

    def _daily_tick():
        today = datetime.now(timezone.utc).date().isoformat()
        if store.get_setting("last_daily") == today:
            return
        store.set_setting("last_daily", today)
        try:
            _expiry_check()
        except Exception:
            pass
        try:
            _traffic_reports_tick()
        except Exception:
            pass

    def _monitor_tick(next_run: dict):
        try:
            store.prune_expired_sessions()
        except Exception:
            pass
        try:
            _offline_check()
        except Exception:
            pass
        try:
            _alerts_tick()
        except Exception:
            pass
        try:
            _ping_tick(next_run)
        except Exception:
            pass
        try:
            _daily_tick()
        except Exception:
            pass

    def _monitor_loop():
        next_run = {}
        while True:
            try:
                _monitor_tick(next_run)
            except Exception as e:  # noqa: BLE001
                print(f"[bigcat-monitor] tick failed: {e}")
            time.sleep(30)

    # expose for tests
    app.config["monitor_tick"] = _monitor_tick
    app.config["do_ping"] = _do_ping
    app.config["notify"] = _notify
    # ============ v3 core end ============

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
        out = store.get_public_settings()
        # Komari 兼容：theme 为当前主题 short，附带该主题的公开设置
        active = _active_theme_short()
        out["theme"] = active
        out["theme_settings"] = store.get_theme_settings(active)
        return out

    def rpc_get_version(params):
        return {"version": VERSION, "hash": VERSION_HASH}

    def rpc_get_me(params):
        if _admin_logged_in():
            return {"username": store.get_admin_username(), "logged_in": True, "uuid": ""}
        return {"username": "Guest", "logged_in": False, "uuid": ""}

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

    def _ping_records_payload(uuid="", task_id="", hours="4"):
        """Komari 形状的延迟记录：{count, records[{task_id,time,value,client}], tasks}。
        bigcat 的延迟探测是主控主动探测，不按节点区分；uuid 查询时仅返回
        探测目标与该节点 IP 匹配的任务，没有匹配则返回空。"""
        try:
            h = max(1, min(720, int(hours)))
        except (TypeError, ValueError):
            h = 4
        since = (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat()
        tasks = store.list_ping_tasks()
        if task_id:
            try:
                tid = int(task_id)
            except (TypeError, ValueError):
                return {"count": 0, "records": [], "tasks": []}
            tasks = [t for t in tasks if t.get("id") == tid]
        elif uuid:
            node = next((c for c in store.list_clients() if c.get("uuid") == uuid), None)
            if node is None:
                tasks = []
            else:
                ips = {str(node.get("ipv4") or ""), str(node.get("ipv6") or "")} - {""}
                tasks = [t for t in tasks
                         if any(ip and ip in str(t.get("target") or "") for ip in ips)]
        records = []
        for t in tasks:
            for r in store.ping_results(t.get("id"), since, limit=2000):
                ok = bool(r.get("ok"))
                lat = r.get("latency_ms")
                records.append({
                    "task_id": t.get("id"),
                    "time": r.get("time"),
                    "value": int(round(lat)) if (ok and lat is not None) else -1,
                })
        records.sort(key=lambda r: r.get("time") or "")
        return {
            "count": len(records),
            "records": records,
            "tasks": [{"id": t.get("id"), "name": t.get("name"),
                       "target": t.get("target"), "type": t.get("type")} for t in tasks],
        }

    def rpc_get_ping_metric_stats(params):
        now = datetime.now(timezone.utc)
        params = params or {}
        hours = float(params.get("hours") or 1)
        start = _parse_time(params.get("start") or params.get("start_time")) or (now - timedelta(hours=hours))
        end = _parse_time(params.get("end") or params.get("end_time")) or now
        return {"start": _iso(start), "end": _iso(end), "stats": [], "count": 0}

    def rpc_get_ping_records(params):
        return _ping_records_payload(
            uuid=str(params.get("uuid", "") or ""),
            task_id=str(params.get("task_id", "") or ""),
            hours=str(params.get("hours", "4") or "4"),
        )

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

    # ------------------------------------------------------------ Komari 兼容补齐
    # 主题可选功能：IP 信息插件（bigcat 未内置 GeoIP，返回未启用，主题会优雅降级）
    @app.route("/api/public/ip-info/v1/status")
    def api_ipinfo_status():
        return jsonify({"ok": True, "data": {
            "available": False,
            "version": "none",
            "schema_version": 1,
            "mainland_china_excluded": False,
            "capabilities": {"geo": False, "network": False, "reputation": False,
                             "media_unlock": False, "ai_unlock": False},
        }})

    @app.route("/api/public/ip-info/v1/lookup")
    def api_ipinfo_lookup():
        return jsonify({"ok": False, "message": "IP 信息功能未启用"}), 503

    @app.route("/api/public/ip-info/v1/latency")
    def api_ipinfo_latency():
        return jsonify({"ok": False, "message": "IP 信息功能未启用"}), 503

    @app.route("/api/recent/<client_uuid>")
    def api_recent(client_uuid):
        return jsonify(rpc_get_client_recent_records({"uuid": client_uuid}))

    @app.route("/api/records/load")
    def api_records_load():
        return jsonify(rpc_get_records_by_uuid({
            "uuid": request.args.get("uuid", ""),
            "hours": request.args.get("hours", "4"),
        }))

    @app.route("/api/records/ping")
    def api_records_ping():
        return jsonify(_ping_records_payload(
            uuid=request.args.get("uuid", ""),
            task_id=request.args.get("task_id", ""),
            hours=request.args.get("hours", "4"),
        ))

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
        # 离线/上线通知的状态变化检测 + 过期监控数据清理（无独立调度器，搭上报触发）
        try:
            _check_node_transitions()
        except Exception:
            pass
        try:
            _maybe_prune()
        except Exception:
            pass
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

    @app.route("/api/agent/tasks", methods=["GET"])
    def agent_tasks():
        client = _agent_auth()
        if not client:
            return jsonify({"error": "unauthorized"}), 401
        tasks = store.claim_exec_tasks(client["uuid"])
        return jsonify({"tasks": tasks})

    @app.route("/api/agent/task_result", methods=["POST"])
    def agent_task_result():
        client = _agent_auth()
        if not client:
            return jsonify({"error": "unauthorized"}), 401
        data = request.get_json(force=True, silent=True) or {}
        task_id = data.get("task_id")
        if task_id is None:
            return jsonify({"error": "task_id required"}), 400
        store.finish_exec_task(int(task_id), str(data.get("output", ""))[:100000],
                               bool(data.get("ok", True)))
        return jsonify({"ok": True})

    # ------------------------------------------------------------ admin helpers
    def _session_version() -> int:
        try:
            return int(store.get_setting("session_version", "0") or 0)
        except Exception:
            return 0

    def _is_online(c: dict) -> bool:
        return _node_online(c, datetime.now(timezone.utc))

    def _post_webhook(url: str, text: str, timeout: int = 8) -> bool:
        # 保留给旧版测试接口兼容；新逻辑走渠道 _send_channel
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps({"text": text}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return 200 <= resp.status < 300
        except Exception:
            return False

    def _check_node_transitions():
        # v2 兼容入口：agent 上报时顺带触发一次；权威检测由监控线程每 30s 执行
        _offline_check()

    def _maybe_prune():
        """Prune old records at most once per hour (no scheduler in Flask dev server)."""
        try:
            keep = max(1, min(720, int(store.get_setting("record_keep_hours", "24") or 24)))
        except Exception:
            keep = 24
        try:
            last = float(store.get_setting("last_prune_ts", "0") or 0)
        except Exception:
            last = 0
        if time.time() - last < 3600:
            return
        store.set_setting("last_prune_ts", str(time.time()))
        try:
            store.prune_records(keep)
        except Exception:
            pass

    # ------------------------------------------------------------ admin API
    # ------------------------------------------------------------ admin auth (v3)
    def _create_admin_session():
        sid = secrets.token_hex(16)
        store.create_session(sid, request.remote_addr or "",
                             request.headers.get("User-Agent", ""))
        session["sid"] = sid
        session["v"] = _session_version()
        store.log_event("info", "auth",
                        f"管理员登录成功（IP {request.remote_addr or '-'}）")
        return sid

    def _admin_logged_in() -> bool:
        sid = session.get("sid")
        s = store.get_session(sid) if sid else None
        if not s or session.get("v") != _session_version():
            return False
        exp = _parse_time(s.get("expires_at"))
        return bool(exp and exp >= datetime.now(timezone.utc))

    def _require_admin(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            sid = session.get("sid")
            s = store.get_session(sid) if sid else None
            if not s or session.get("v") != _session_version():
                return jsonify({"error": "login required"}), 401
            exp = _parse_time(s.get("expires_at"))
            if not exp or exp < datetime.now(timezone.utc):
                return jsonify({"error": "login required"}), 401
            try:
                last = _parse_time(s.get("last_seen"))
                if not last or (datetime.now(timezone.utc) - last).total_seconds() > 120:
                    store.touch_session(sid)
            except Exception:
                pass
            return fn(*a, **kw)
        return wrapper

    @app.route("/api/admin/setup", methods=["POST"])
    def admin_setup():
        # 仅在尚未创建管理员时可用（对应 admin 面板的首次初始化）
        if store.has_admin():
            return jsonify({"error": "admin already exists"}), 400
        data = request.get_json(force=True, silent=True) or {}
        username = (data.get("username") or "admin").strip() or "admin"
        password = data.get("password", "")
        if not password:
            return jsonify({"error": "password required"}), 400
        store.set_admin(username, password)
        _create_admin_session()
        return jsonify({"ok": True, "username": username})

    @app.route("/api/admin/login", methods=["POST"])
    def admin_login():
        data = request.get_json(force=True, silent=True) or {}
        if not store.verify_admin(data.get("password", ""), data.get("username", "")):
            store.log_event("warn", "auth",
                            f"管理员登录失败（IP {request.remote_addr or '-'}）")
            return jsonify({"error": "invalid username or password"}), 401
        if (store.get_setting("totp_enabled", "0") or "0") == "1":
            session["pre2fa"] = True
            return jsonify({"ok": False, "need_2fa": True})
        _create_admin_session()
        return jsonify({"ok": True, "username": store.get_admin_username()})

    @app.route("/api/admin/login/2fa", methods=["POST"])
    def admin_login_2fa():
        if not session.get("pre2fa"):
            return jsonify({"error": "login required"}), 401
        data = request.get_json(force=True, silent=True) or {}
        if _totp_verify(store.get_setting("totp_secret", ""), data.get("code", "")):
            session.pop("pre2fa", None)
            _create_admin_session()
            return jsonify({"ok": True, "username": store.get_admin_username()})
        return jsonify({"error": "验证码错误"}), 401

    @app.route("/api/admin/logout", methods=["POST"])
    def admin_logout():
        sid = session.pop("sid", None)
        if sid:
            try:
                store._conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
                store._conn.commit()
            except Exception:
                pass
        session.pop("pre2fa", None)
        return jsonify({"ok": True})

    @app.route("/api/admin/status")
    def admin_status():
        sid = session.get("sid")
        s = store.get_session(sid) if sid else None
        logged = bool(s) and session.get("v") == _session_version()
        if logged:
            exp = _parse_time(s.get("expires_at"))
            logged = bool(exp and exp > datetime.now(timezone.utc))
        return jsonify({
            "has_admin": store.has_admin(),
            "logged_in": logged,
            "username": store.get_admin_username() if logged else "",
            "nodes": len(store.list_clients()),
            "need_2fa": bool(session.get("pre2fa")),
        })

    @app.route("/api/admin/clients")
    @_require_admin
    def admin_clients():
        now = datetime.now(timezone.utc)
        today = now.date()
        out = []
        for c in store.list_clients():
            d = dict(c)
            d["online"] = _is_online(c)
            d["days_to_expiry"] = None
            exp_s = (c.get("expired_at") or "").strip()
            if exp_s:
                try:
                    d["days_to_expiry"] = (datetime.fromisoformat(exp_s).date() - today).days
                except Exception:
                    pass
            out.append(d)
        return jsonify(out)

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
            out = store.get_public_settings()
            out.update({
                "offline_threshold": store.get_setting("offline_threshold", "180"),
                "record_keep_hours": store.get_setting("record_keep_hours", "24"),
                "notify_enabled": store.get_setting("notify_enabled", "0"),
                "notify_webhook": store.get_setting("notify_webhook", ""),
                "notify_template": store.get_setting("notify_template", "【{event}】{message}"),
                "expiry_remind_enabled": store.get_setting("expiry_remind_enabled", "1"),
                "expiry_remind_days": store.get_setting("expiry_remind_days", "10"),
            })
            return jsonify(out)
        data = request.get_json(force=True, silent=True) or {}
        for k in ("sitename", "description", "theme"):
            if k in data:
                store.set_setting(k, str(data[k]))
        # 数值型配置做范围钳制
        if "offline_threshold" in data:
            try:
                v = max(30, min(3600, int(data["offline_threshold"])))
            except Exception:
                v = 180
            store.set_setting("offline_threshold", str(v))
        if "record_keep_hours" in data:
            try:
                v = max(1, min(720, int(data["record_keep_hours"])))
            except Exception:
                v = 24
            store.set_setting("record_keep_hours", str(v))
        if "notify_enabled" in data:
            store.set_setting("notify_enabled", "1" if str(data["notify_enabled"]) == "1" else "0")
        if "notify_webhook" in data:
            store.set_setting("notify_webhook", str(data["notify_webhook"]).strip()[:500])
        if "notify_template" in data:
            store.set_setting("notify_template", str(data["notify_template"])[:300])
        if "expiry_remind_enabled" in data:
            store.set_setting("expiry_remind_enabled",
                              "1" if str(data["expiry_remind_enabled"]) == "1" else "0")
        if "expiry_remind_days" in data:
            try:
                v = max(1, min(365, int(data["expiry_remind_days"])))
            except Exception:
                v = 10
            store.set_setting("expiry_remind_days", str(v))
        return jsonify({"ok": True})

    def _expiry_soon_list(within_days: int = 30):
        today = datetime.now(timezone.utc).date()
        out = []
        for c in store.list_clients():
            exp_s = (c.get("expired_at") or "").strip()
            if not exp_s:
                continue
            try:
                exp = datetime.fromisoformat(exp_s).date()
            except Exception:
                continue
            delta = (exp - today).days
            if 0 <= delta <= within_days:
                out.append({"uuid": c["uuid"], "name": c.get("name") or c["uuid"][:8],
                            "expired_at": exp_s, "days": delta})
        return sorted(out, key=lambda x: x["days"])

    @app.route("/api/admin/dashboard")
    @_require_admin
    def admin_dashboard():
        clients = store.list_clients()
        online = [c for c in clients if _is_online(c)]
        offline = [c for c in clients if not _is_online(c)]
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
        return jsonify({
            "total": len(clients),
            "online": len(online),
            "offline": len(offline),
            "db_size": store.db_size(),
            "records": store.count_records(),
            "reports_today": store.count_records_since(today),
            "offline_threshold": _online_threshold(),
            "offline_nodes": [
                {"uuid": c["uuid"], "name": c.get("name") or c["uuid"][:8],
                 "last_report_at": c.get("last_report_at")}
                for c in sorted(offline, key=lambda c: c.get("last_report_at") or "")[:10]
            ],
            "expiry_soon": _expiry_soon_list(),
            "nodes": [
                {"uuid": c["uuid"], "name": c.get("name") or c["uuid"][:8],
                 "online": _is_online(c), "last_report_at": c.get("last_report_at")}
                for c in clients
            ],
        })

    @app.route("/api/admin/notify/test", methods=["POST"])
    @_require_admin
    def admin_notify_test():
        data = request.get_json(force=True, silent=True) or {}
        # 渠道下标优先；否则兼容旧的 webhook 直发
        if "index" in data:
            try:
                ch = _get_channels()[int(data["index"])]
            except Exception:
                return jsonify({"error": "渠道不存在"}), 404
            ok = _send_channel(ch, _render_msg("测试", "bigcat 通知测试：渠道工作正常"))
            return jsonify({"ok": ok} if ok else {"error": "发送失败，请检查渠道配置"}), 200 if ok else 502
        url = (data.get("webhook") or store.get_setting("notify_webhook", "") or "").strip()
        if not url:
            return jsonify({"error": "webhook url required"}), 400
        ok = _post_webhook(url, "✅ bigcat 通知测试：webhook 工作正常", timeout=10)
        return jsonify({"ok": ok} if ok else {"error": "webhook 发送失败，请检查 URL"}), 200 if ok else 502

    @app.route("/api/admin/account", methods=["POST"])
    @_require_admin
    def admin_account():
        data = request.get_json(force=True, silent=True) or {}
        cur_username = store.get_admin_username()
        if not store.verify_admin(data.get("current_password", ""), cur_username):
            return jsonify({"error": "当前密码错误"}), 401
        username = (data.get("username") or "").strip()
        new_password = data.get("new_password", "")
        if username and username != cur_username and not new_password:
            return jsonify({"error": "修改用户名需同时设置新密码"}), 400
        if new_password:
            store.set_admin(username or cur_username, new_password)
        elif username and username != cur_username:
            return jsonify({"error": "修改用户名需同时设置新密码"}), 400
        return jsonify({"ok": True, "username": store.get_admin_username()})

    @app.route("/api/admin/sessions")
    @_require_admin
    def admin_sessions():
        my_sid = session.get("sid")
        out = []
        for s in store.list_sessions():
            out.append({
                "id": s["id"][:8] + "…",
                "ip": s.get("ip") or "",
                "user_agent": (s.get("user_agent") or "")[:120],
                "created_at": s.get("created_at"),
                "last_seen": s.get("last_seen"),
                "expires_at": s.get("expires_at"),
                "current": s["id"] == my_sid,
            })
        return jsonify({"sessions": out})

    @app.route("/api/admin/sessions/revoke_all", methods=["POST"])
    @_require_admin
    def admin_sessions_revoke_all():
        # 真正删除全部登录会话（含当前）：后续请求全部 401，当前用户需重新登录
        n = store.delete_all_sessions()
        store.log_event("warn", "auth", f"删除全部登录会话（{n} 个）")
        session.pop("sid", None)
        return jsonify({"ok": True, "deleted": n})

    @app.route("/api/admin/logs")
    @_require_admin
    def admin_logs():
        try:
            lines = max(50, min(1000, int(request.args.get("lines", 200))))
        except Exception:
            lines = 200
        try:
            out = subprocess.run(
                ["journalctl", "-u", "bigcat", "--no-pager", "-n", str(lines)],
                capture_output=True, text=True, timeout=10,
            )
            if out.returncode == 0 and out.stdout.strip():
                return jsonify({"source": "journalctl -u bigcat", "logs": out.stdout})
        except Exception:
            pass
        return jsonify({"source": "", "logs": "",
                        "hint": "未找到 systemd 日志（journalctl -u bigcat 不可用），请直接在服务器上查看"})

    @app.route("/api/admin/events")
    @_require_admin
    def admin_events():
        try:
            page = max(1, int(request.args.get("page", 1)))
        except Exception:
            page = 1
        try:
            per_page = max(10, min(200, int(request.args.get("per_page", 50))))
        except Exception:
            per_page = 50
        return jsonify({
            "total": store.count_events(),
            "events": store.list_events(per_page, (page - 1) * per_page),
        })

    @app.route("/api/admin/events", methods=["DELETE"])
    @_require_admin
    def admin_events_clear():
        n = store.clear_events()
        return jsonify({"ok": True, "deleted": n})

    @app.route("/api/admin/about")
    @_require_admin
    def admin_about():
        try:
            flask_v = _pkg_version("flask")
        except Exception:
            flask_v = "unknown"
        import platform
        return jsonify({
            "bigcat": VERSION,
            "python": platform.python_version(),
            "flask": flask_v,
            "platform": platform.platform(),
            "db_path": str(store.db_path),
            "db_size": store.db_size(),
            "records": store.count_records(),
            "uptime_sec": int(time.time() - STARTED_AT),
            "started_at": datetime.fromtimestamp(STARTED_AT, timezone.utc).isoformat().replace("+00:00", "Z"),
        })

    # ============================================================ v3 API
    # ---------------- 统计 ----------------
    @app.route("/api/admin/stats/traffic")
    @_require_admin
    def admin_stats_traffic():
        try:
            hours = max(1, min(168, int(request.args.get("hours", 24))))
        except Exception:
            hours = 24
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        bucket_sec = max(300, int(hours * 3600 / 48))
        rows = store.traffic_series(_iso(start), _iso(end), bucket_sec)
        return jsonify({
            "labels": [r["time"][11:16] if len(r["time"]) > 16 else r["time"] for r in rows],
            "up": [r["up"] for r in rows],
            "down": [r["down"] for r in rows],
        })

    @app.route("/api/admin/stats/rank")
    @_require_admin
    def admin_stats_rank():
        try:
            hours = max(1, min(168, int(request.args.get("hours", 24))))
        except Exception:
            hours = 24
        end = datetime.now(timezone.utc)
        start = _iso(end - timedelta(hours=hours))
        end_s = _iso(end)
        out = []
        for c in store.list_clients():
            t = store.sum_traffic(c["uuid"], start, end_s)
            m = store.avg_metrics(c["uuid"], start)
            out.append({
                "uuid": c["uuid"],
                "name": c.get("name") or c["uuid"][:8],
                "up": t["up"], "down": t["down"], "total": t["up"] + t["down"],
                "avg_cpu": m["cpu"], "avg_mem": m["mem"],
                "online": _is_online(c),
            })
        return jsonify(out)

    @app.route("/api/admin/stats/ping")
    @_require_admin
    def admin_stats_ping():
        try:
            hours = max(1, min(720, int(request.args.get("hours", 24))))
        except Exception:
            hours = 24
        since = _iso(datetime.now(timezone.utc) - timedelta(hours=hours))
        return jsonify(store.ping_stats(since))

    # ---------------- 延迟监测任务 ----------------
    @app.route("/api/admin/ping/tasks", methods=["GET"])
    @_require_admin
    def admin_ping_tasks():
        return jsonify(store.list_ping_tasks())

    @app.route("/api/admin/ping/tasks", methods=["POST"])
    @_require_admin
    def admin_ping_task_add():
        data = request.get_json(force=True, silent=True) or {}
        name = (data.get("name") or "").strip()
        target = (data.get("target") or "").strip()
        if not name or not target:
            return jsonify({"error": "name 和 target 必填"}), 400
        typ = (data.get("type") or "tcp").strip().lower()
        if typ not in ("tcp", "http", "icmp"):
            typ = "tcp"
        try:
            interval = max(30, min(86400, int(data.get("interval_sec", 300))))
        except Exception:
            interval = 300
        task = store.add_ping_task(name, target, typ, interval)
        store.log_event("info", "ping", f"新增延迟任务「{name}」（{typ} {target}）")
        return jsonify(task)

    @app.route("/api/admin/ping/tasks/<int:task_id>", methods=["PUT"])
    @_require_admin
    def admin_ping_task_update(task_id):
        data = request.get_json(force=True, silent=True) or {}
        fields = {}
        if "name" in data:
            fields["name"] = str(data["name"]).strip()
        if "target" in data:
            fields["target"] = str(data["target"]).strip()
        if "type" in data and str(data["type"]).lower() in ("tcp", "http", "icmp"):
            fields["type"] = str(data["type"]).lower()
        if "interval_sec" in data:
            try:
                fields["interval_sec"] = max(30, min(86400, int(data["interval_sec"])))
            except Exception:
                pass
        if "enabled" in data:
            fields["enabled"] = 1 if str(data["enabled"]) == "1" else 0
        store.update_ping_task(task_id, fields)
        return jsonify({"ok": True})

    @app.route("/api/admin/ping/tasks/<int:task_id>", methods=["DELETE"])
    @_require_admin
    def admin_ping_task_delete(task_id):
        store.delete_ping_task(task_id)
        store.log_event("info", "ping", f"删除延迟任务 {task_id}")
        return jsonify({"ok": True})

    @app.route("/api/admin/ping/results")
    @_require_admin
    def admin_ping_results():
        try:
            hours = max(1, min(720, int(request.args.get("hours", 24))))
        except Exception:
            hours = 24
        since = _iso(datetime.now(timezone.utc) - timedelta(hours=hours))
        task_id = request.args.get("task_id")
        try:
            task_id = int(task_id) if task_id else None
        except Exception:
            task_id = None
        return jsonify(store.ping_results(task_id, since, limit=2000))

    # ---------------- 负载告警规则 ----------------
    @app.route("/api/admin/alerts", methods=["GET"])
    @_require_admin
    def admin_alerts():
        return jsonify(store.list_alert_rules())

    @app.route("/api/admin/alerts", methods=["POST"])
    @_require_admin
    def admin_alert_add():
        data = request.get_json(force=True, silent=True) or {}
        name = (data.get("name") or "").strip() or "未命名规则"
        metric = str(data.get("metric", "cpu")).lower()
        if metric not in ("cpu", "ram", "disk"):
            metric = "cpu"
        try:
            threshold = max(1, min(100, float(data.get("threshold", 90))))
        except Exception:
            threshold = 90
        try:
            ratio = max(0.1, min(1.0, float(data.get("ratio", 0.8))))
        except Exception:
            ratio = 0.8
        try:
            interval = max(1, min(1440, int(data.get("interval_min", 2))))
        except Exception:
            interval = 2
        clients = data.get("clients") or []
        if not isinstance(clients, list):
            clients = []
        rule = store.add_alert_rule(name, metric, threshold, ratio, interval,
                                    json.dumps([str(x) for x in clients]))
        store.log_event("info", "alert", f"新增告警规则「{name}」")
        return jsonify(rule)

    @app.route("/api/admin/alerts/<int:rule_id>", methods=["PUT"])
    @_require_admin
    def admin_alert_update(rule_id):
        data = request.get_json(force=True, silent=True) or {}
        fields = {}
        if "name" in data:
            fields["name"] = str(data["name"]).strip()
        if "metric" in data and str(data["metric"]).lower() in ("cpu", "ram", "disk"):
            fields["metric"] = str(data["metric"]).lower()
        for k in ("threshold", "ratio", "interval_min", "enabled"):
            if k in data:
                fields[k] = data[k]
        store.update_alert_rule(rule_id, fields)
        return jsonify({"ok": True})

    @app.route("/api/admin/alerts/<int:rule_id>", methods=["DELETE"])
    @_require_admin
    def admin_alert_delete(rule_id):
        store.delete_alert_rule(rule_id)
        return jsonify({"ok": True})

    # ---------------- 通知 ----------------
    @app.route("/api/admin/notify/channels", methods=["GET"])
    @_require_admin
    def admin_notify_channels():
        return jsonify(_get_channels())

    @app.route("/api/admin/notify/channels", methods=["POST"])
    @_require_admin
    def admin_notify_channel_add():
        data = request.get_json(force=True, silent=True) or {}
        typ = str(data.get("type", "webhook")).lower()
        if typ not in ("webhook", "telegram", "bark"):
            return jsonify({"error": "未知渠道类型"}), 400
        ch = _get_channels()
        ch.append({
            "name": (data.get("name") or typ).strip()[:50],
            "type": typ,
            "enabled": bool(data.get("enabled", True)),
            "config": data.get("config") or {},
        })
        _save_channels(ch)
        return jsonify({"ok": True, "index": len(ch) - 1})

    @app.route("/api/admin/notify/channels/<int:index>", methods=["PUT"])
    @_require_admin
    def admin_notify_channel_update(index):
        data = request.get_json(force=True, silent=True) or {}
        ch = _get_channels()
        if not (0 <= index < len(ch)):
            return jsonify({"error": "渠道不存在"}), 404
        c = ch[index]
        if "name" in data:
            c["name"] = str(data["name"]).strip()[:50]
        if "enabled" in data:
            c["enabled"] = bool(data["enabled"])
        if "config" in data:
            c["config"] = data["config"] or {}
        _save_channels(ch)
        return jsonify({"ok": True})

    @app.route("/api/admin/notify/channels/<int:index>", methods=["DELETE"])
    @_require_admin
    def admin_notify_channel_delete(index):
        ch = _get_channels()
        if not (0 <= index < len(ch)):
            return jsonify({"error": "渠道不存在"}), 404
        ch.pop(index)
        _save_channels(ch)
        return jsonify({"ok": True})

    @app.route("/api/admin/notify/offline")
    @_require_admin
    def admin_notify_offline():
        return jsonify([{
            "uuid": c["uuid"],
            "name": c.get("name") or c["uuid"][:8],
            "online": _is_online(c),
            "notify_offline": c.get("notify_offline", 1),
            "offline_grace": c.get("offline_grace") or _online_threshold(),
        } for c in store.list_clients()])

    @app.route("/api/admin/notify/offline/<client_uuid>", methods=["PUT"])
    @_require_admin
    def admin_notify_offline_update(client_uuid):
        data = request.get_json(force=True, silent=True) or {}
        fields = {}
        if "notify_offline" in data:
            fields["notify_offline"] = 1 if str(data["notify_offline"]) == "1" else 0
        if "offline_grace" in data:
            try:
                fields["offline_grace"] = max(30, min(86400, int(data["offline_grace"])))
            except Exception:
                pass
        if not store.get_client(client_uuid):
            return jsonify({"error": "node not found"}), 404
        store.update_client(client_uuid, fields)
        return jsonify({"ok": True})

    @app.route("/api/admin/notify/traffic", methods=["GET"])
    @_require_admin
    def admin_notify_traffic_get():
        return jsonify([{
            "uuid": c["uuid"],
            "name": c.get("name") or c["uuid"][:8],
            "report_enabled": c.get("report_enabled", 0),
            "report_types": c.get("report_types") or "",
        } for c in store.list_clients()])

    @app.route("/api/admin/notify/traffic", methods=["PUT"])
    @_require_admin
    def admin_notify_traffic_put():
        data = request.get_json(force=True, silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"error": "invalid body"}), 400
        for uuid, cfg in data.items():
            if not store.get_client(uuid):
                continue
            fields = {}
            if isinstance(cfg, dict):
                if "report_enabled" in cfg:
                    fields["report_enabled"] = 1 if str(cfg["report_enabled"]) == "1" else 0
                if "report_types" in cfg:
                    types = [t for t in str(cfg["report_types"]).split(",") if t in ("daily", "weekly", "monthly")]
                    fields["report_types"] = ",".join(types)
                store.update_client(uuid, fields)
        return jsonify({"ok": True})

    # ---------------- 2FA ----------------
    @app.route("/api/admin/2fa")
    @_require_admin
    def admin_2fa_status():
        return jsonify({"enabled": (store.get_setting("totp_enabled", "0") or "0") == "1"})

    @app.route("/api/admin/2fa/setup", methods=["POST"])
    @_require_admin
    def admin_2fa_setup():
        secret = _new_totp_secret()
        session["totp_pending"] = secret
        user = store.get_admin_username()
        uri = (f"otpauth://totp/bigcat:{user}?secret={secret}"
               f"&issuer=bigcat&algorithm=SHA1&digits=6&period=30")
        return jsonify({"secret": secret, "uri": uri})

    @app.route("/api/admin/2fa/enable", methods=["POST"])
    @_require_admin
    def admin_2fa_enable():
        data = request.get_json(force=True, silent=True) or {}
        secret = session.get("totp_pending")
        if not secret:
            return jsonify({"error": "请先生成密钥"}), 400
        if _totp_verify(secret, data.get("code", "")):
            store.set_setting("totp_secret", secret)
            store.set_setting("totp_enabled", "1")
            session.pop("totp_pending", None)
            store.log_event("info", "auth", "管理员启用了双重认证")
            return jsonify({"ok": True})
        return jsonify({"error": "验证码错误"}), 400

    @app.route("/api/admin/2fa/disable", methods=["POST"])
    @_require_admin
    def admin_2fa_disable():
        data = request.get_json(force=True, silent=True) or {}
        if not store.verify_admin(data.get("password", ""), store.get_admin_username()):
            return jsonify({"error": "密码错误"}), 401
        store.set_setting("totp_enabled", "0")
        store.log_event("warn", "auth", "管理员关闭了双重认证")
        return jsonify({"ok": True})

    # ---------------- 远程执行 ----------------
    @app.route("/api/admin/exec", methods=["POST"])
    @_require_admin
    def admin_exec():
        data = request.get_json(force=True, silent=True) or {}
        command = (data.get("command") or "").strip()
        uuids = data.get("client_uuids") or []
        if not command or not uuids:
            return jsonify({"error": "command 和 client_uuids 必填"}), 400
        created = [store.add_exec_task(u, command[:2000])["id"]
                   for u in uuids if store.get_client(u)]
        store.log_event("warn", "exec",
                        f"管理员下发远程执行命令（{len(created)} 个节点）: {command[:120]}")
        return jsonify({"ok": True, "task_ids": created})

    @app.route("/api/admin/exec/tasks")
    @_require_admin
    def admin_exec_tasks():
        try:
            limit = max(10, min(500, int(request.args.get("limit", 50))))
        except Exception:
            limit = 50
        rows = store.list_exec_tasks(limit)
        names = {c["uuid"]: (c.get("name") or c["uuid"][:8]) for c in store.list_clients()}
        for r in rows:
            r["client_name"] = names.get(r["client_uuid"], r["client_uuid"][:8])
        return jsonify(rows)

    # ---------------- 数据库维护 ----------------
    @app.route("/api/admin/db/info")
    @_require_admin
    def admin_db_info():
        cur = store._conn.cursor()
        ping_n = cur.execute("SELECT COUNT(*) FROM ping_results").fetchone()[0]
        sess_n = cur.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        return jsonify({
            "db_size": store.db_size(),
            "records": store.count_records(),
            "events": store.count_events(),
            "ping_results": ping_n,
            "sessions": sess_n,
            "record_keep_hours": store.get_setting("record_keep_hours", "24"),
        })

    @app.route("/api/admin/db/vacuum", methods=["POST"])
    @_require_admin
    def admin_db_vacuum():
        res = store.vacuum()
        store.log_event("info", "db",
                        f"数据库压缩完成：{res['before']} → {res['after']} 字节")
        return jsonify({"ok": True, **res})

    # ============================================================ theme & skin
    def _theme_info(short, manifest=None):
        m = manifest or _theme_manifest(short) or {}
        return {
            "name": m.get("name", short),
            "short": short,
            "description": m.get("description", ""),
            "version": m.get("version", ""),
            "author": m.get("author", ""),
            "url": m.get("url", ""),
            "preview": m.get("preview", ""),
        }

    def _install_theme_zip(data: bytes):
        """校验并解压主题 zip，返回主题信息；失败 raise ValueError(msg)。"""
        import io as _io
        try:
            zf = _zipfile.ZipFile(_io.BytesIO(data))
        except Exception:
            raise ValueError("不是有效的 ZIP 文件")
        names = zf.namelist()
        if len(names) > _MAX_THEME_FILES:
            raise ValueError("主题文件数量超限")
        total = 0
        for info in zf.infolist():
            if info.is_dir():
                continue
            if info.file_size > _MAX_THEME_FILE_SIZE:
                raise ValueError(f"主题文件 {info.filename} 超过大小限制")
            total += info.file_size
            if total > _MAX_THEME_TOTAL_SIZE:
                raise ValueError("主题解压后总大小超限")
        if "komari-theme.json" not in names:
            raise ValueError("缺少 komari-theme.json（需放在压缩包根目录）")
        try:
            manifest = json.loads(zf.read("komari-theme.json").decode("utf-8"))
        except Exception:
            raise ValueError("komari-theme.json 不是有效的 JSON")
        name = str(manifest.get("name") or "").strip()
        short = str(manifest.get("short") or "").strip()
        if not name or not short:
            raise ValueError("komari-theme.json 缺少必填字段 name / short")
        if short == "default" or not _THEME_SHORT_RE.match(short):
            raise ValueError("short 无效（仅允许字母、数字、下划线、连字符，且不能为 default）")
        if "dist/index.html" not in names:
            raise ValueError("主题包内缺少 dist/index.html")
        dest = os.path.join(THEMES_DIR, short)
        base = os.path.realpath(dest)
        if os.path.isdir(dest):
            _shutil.rmtree(dest)
        os.makedirs(dest, exist_ok=True)
        try:
            for info in zf.infolist():
                rel = info.filename.replace("\\", "/")
                target = os.path.realpath(os.path.join(dest, rel))
                if target != base and not target.startswith(base + os.sep):
                    continue  # 路径穿越，跳过
                if info.is_dir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    _shutil.copyfileobj(src, out)
        except Exception as e:
            _shutil.rmtree(dest, ignore_errors=True)
            raise ValueError(f"解压主题失败: {e}")
        return _theme_info(short, manifest)

    def _download_theme(url: str) -> bytes:
        m = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", url)
        if m:
            owner, repo = m.group(1), m.group(2)
            req = urllib.request.Request(
                f"https://api.github.com/repos/{owner}/{repo}/releases/latest",
                headers={"User-Agent": "bigcat", "Accept": "application/vnd.github+json"})
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    rel = json.loads(r.read().decode("utf-8"))
            except Exception as e:
                raise ValueError(f"获取 GitHub release 失败: {e}")
            zip_url = next((a.get("browser_download_url") for a in (rel.get("assets") or [])
                            if str(a.get("name") or "").lower().endswith(".zip")), None)
            if not zip_url:
                raise ValueError("该仓库最新 release 中没有 zip 资源")
            url = zip_url
        req = urllib.request.Request(url, headers={"User-Agent": "bigcat"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read(_MAX_THEME_TOTAL_SIZE + 1)
        except Exception as e:
            raise ValueError(f"下载主题失败: {e}")
        if len(data) > _MAX_THEME_TOTAL_SIZE:
            raise ValueError("主题文件过大")
        return data

    @app.route("/api/admin/theme/list")
    @_require_admin
    def admin_theme_list():
        active = _active_theme_short()
        themes = [{
            "name": "默认主题（内置 LuminaPlus）", "short": "default",
            "description": "bigcat 内置前台主题", "version": VERSION,
            "author": "bigcat", "url": "", "preview": "",
            "active": active == "default",
        }]
        try:
            entries = sorted(os.listdir(THEMES_DIR))
        except Exception:
            entries = []
        for e in entries:
            m = _theme_manifest(e)
            if not m:
                continue
            info = _theme_info(e, m)
            info["active"] = (e == active)
            themes.append(info)
        return jsonify({"themes": themes, "active": active})

    @app.route("/api/admin/theme/upload", methods=["POST"])
    @_require_admin
    def admin_theme_upload():
        f = request.files.get("file")
        if not f:
            return jsonify({"error": "请选择主题 zip 文件"}), 400
        data = f.read()
        if len(data) > _MAX_THEME_TOTAL_SIZE:
            return jsonify({"error": "文件过大"}), 400
        try:
            info = _install_theme_zip(data)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"ok": True, "theme": info})

    @app.route("/api/admin/theme/import", methods=["POST"])
    @_require_admin
    def admin_theme_import():
        body = request.get_json(force=True, silent=True) or {}
        url = (body.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return jsonify({"error": "请输入 http(s) 链接"}), 400
        try:
            data = _download_theme(url)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        try:
            info = _install_theme_zip(data)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"ok": True, "theme": info})

    @app.route("/api/admin/theme/set")
    @_require_admin
    def admin_theme_set():
        short = (request.args.get("theme") or "").strip()
        if short != "default" and not _theme_manifest(short):
            return jsonify({"error": "主题不存在"}), 404
        store.set_setting("active_theme", short)
        return jsonify({"ok": True, "theme": short})

    @app.route("/api/admin/theme/delete", methods=["POST"])
    @_require_admin
    def admin_theme_delete():
        body = request.get_json(force=True, silent=True) or {}
        short = (body.get("short") or "").strip()
        if short == "default" or not _THEME_SHORT_RE.match(short):
            return jsonify({"error": "不能删除该主题"}), 400
        if _active_theme_short() == short:
            return jsonify({"error": "该主题正在使用中，请先切换到其他主题"}), 400
        d = os.path.join(THEMES_DIR, short)
        if not os.path.isdir(d) or not _theme_manifest(short):
            return jsonify({"error": "主题不存在"}), 404
        _shutil.rmtree(d, ignore_errors=True)
        return jsonify({"ok": True})

    @app.route("/api/admin/theme/settings", methods=["GET", "POST"])
    @_require_admin
    def admin_theme_settings():
        body = request.get_json(force=True, silent=True) if request.method == "POST" else {}
        short = ((request.args.get("theme") or (body or {}).get("theme")
                  or _active_theme_short()) or "").strip()
        if request.method == "GET":
            return jsonify({"theme": short, "settings": store.get_theme_settings(short)})
        if not isinstance(body, dict):
            return jsonify({"error": "设置必须为 JSON 对象"}), 400
        body = {k: v for k, v in body.items() if k != "theme"}
        store.set_theme_settings(short, body)
        return jsonify({"ok": True})

    @app.route("/api/admin/theme/preview/<short>")
    @_require_admin
    def admin_theme_preview(short):
        m = _theme_manifest(short)
        if not m:
            return jsonify({"error": "主题不存在"}), 404
        rel = str(m.get("preview") or "").replace("\\", "/").lstrip("/")
        if not rel or ".." in rel:
            return jsonify({"error": "无预览图"}), 404
        return send_from_directory(os.path.join(THEMES_DIR, short), rel)

    # ------------------------------------------------------------ admin skin
    @app.route("/api/admin/ip-info/v1/refresh", methods=["POST"])
    @_require_admin
    def api_ipinfo_refresh():
        return jsonify({"ok": False, "message": "IP 信息功能未启用"}), 503

    @app.route("/api/admin/ping")
    @_require_admin
    def api_admin_ping():
        # 主题期望裸数组 [{id, interval, name, loss, clients, type, target, weight}]
        tasks = store.list_ping_tasks() if hasattr(store, "list_ping_tasks") else []
        return jsonify([{
            "id": t.get("id"), "interval": t.get("interval_sec") or 60,
            "name": t.get("name") or "", "loss": 0, "clients": [],
            "type": t.get("type") or "icmp", "target": t.get("target") or "",
            "weight": 0,
        } for t in tasks])

    @app.route("/api/admin/client/list")
    @_require_admin
    def api_admin_client_list():
        # 主题期望裸数组 [{uuid, name, group, region, weight}]
        return jsonify([{
            "uuid": c.get("uuid"), "name": c.get("name") or "",
            "group": c.get("group") or "", "region": c.get("region") or "",
            "weight": c.get("weight") or 0,
        } for c in store.list_clients()])

    _SKIN_ACCENT_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

    def _get_skin():
        try:
            skin = json.loads(store.get_setting("admin_skin", "") or "")
        except Exception:
            skin = {}
        mode = skin.get("mode") if skin.get("mode") in ("dark", "light") else "dark"
        accent = skin.get("accent")
        if not _SKIN_ACCENT_RE.match(str(accent or "")):
            accent = "#2f81f7"
        return {"mode": mode, "accent": accent}

    @app.route("/api/admin/skin", methods=["GET", "PUT"])
    @_require_admin
    def admin_skin():
        if request.method == "GET":
            return jsonify(_get_skin())
        body = request.get_json(force=True, silent=True) or {}
        mode = body.get("mode") if body.get("mode") in ("dark", "light") else "dark"
        accent = str(body.get("accent") or "")
        if not _SKIN_ACCENT_RE.match(accent):
            return jsonify({"error": "强调色格式无效（需 #rrggbb）"}), 400
        skin = {"mode": mode, "accent": accent}
        store.set_setting("admin_skin", json.dumps(skin))
        return jsonify({"ok": True, "skin": skin})

    # ------------------------------------------------------------ 配置备份与恢复
    @app.route("/api/admin/backup", methods=["GET"])
    @_require_admin
    def admin_backup_download():
        payload = {
            "format": "bigcat-backup",
            "version": 1,
            "app_version": VERSION,
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "data": store.export_config(),
        }
        body = json.dumps(payload, ensure_ascii=False)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        resp = app.response_class(body, mimetype="application/json")
        resp.headers["Content-Disposition"] = f"attachment; filename=bigcat-backup-{stamp}.json"
        return resp

    @app.route("/api/admin/backup/restore", methods=["POST"])
    @_require_admin
    def admin_backup_restore():
        f = request.files.get("backup")
        if f is None:
            return jsonify({"error": "请上传备份文件（字段名 backup）"}), 400
        raw = f.read(64 * 1024 * 1024 + 1)
        if len(raw) > 64 * 1024 * 1024:
            return jsonify({"error": "备份文件过大（上限 64MB）"}), 400
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return jsonify({"error": "备份文件不是有效的 JSON"}), 400
        if not isinstance(payload, dict) or payload.get("format") != "bigcat-backup":
            return jsonify({"error": "不是有效的 bigcat 备份文件"}), 400
        try:
            store.import_config(payload.get("data") or {})
        except ValueError as e:
            return jsonify({"error": f"备份数据无效：{e}"}), 400
        except Exception as e:
            return jsonify({"error": f"恢复失败：{e}"}), 500
        return jsonify({"ok": True})

    # ============================================================ v3 API end

    # ------------------------------------------------------------ SPA fallback
    # 必须注册在所有路由之后：/traffic、/instance/xxx 等前端路由刷新或直连时
    # 返回 index.html 交给前端路由接管；未知 API 路径返回 JSON 404
    @app.route("/<path:p>")
    def spa_fallback(p):
        if p.startswith("api/"):
            return jsonify({"error": "not found"}), 404
        return send_from_directory(_active_theme_dist(), "index.html")

    # 后台监控线程：离线检测 / 告警 / 延迟监测 / 到期提醒 / 流量报告。
    # enable_monitor=False 时（单元测试）不启动。
    if enable_monitor and not app.config.get("monitor_started"):
        app.config["monitor_started"] = True
        threading.Thread(target=_monitor_loop, daemon=True, name="bigcat-monitor").start()
        print(f"[bigcat] monitor thread started (interval 30s)")

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
