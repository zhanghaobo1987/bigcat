#!/usr/bin/env python3
"""
bigcat agent.

Collects host metrics with psutil and reports them to a bigcat server,
using the Komari v2 report shape (protocol/v2) over plain HTTPS POST.

Usage:
    python3 agent.py --server http://MASTER:25774 --token <node-token> [--interval 2]

Register a node first (on the master):
    curl -X POST http://MASTER:25774/api/agent/register -d '{"name":"hk-1"}'
"""
import argparse
import json
import platform
import socket
import time
import urllib.request
import urllib.error


def _post(url: str, payload: dict, token: str = "", timeout: int = 10) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        print(f"[agent] HTTP {e.code}: {e.read().decode()[:200]}")
    except Exception as e:  # noqa: BLE001
        print(f"[agent] request failed: {e}")
    return {}


def collect_basic_info():
    import psutil

    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()
    du = psutil.disk_usage("/")
    uname = platform.uname()
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    cpu_name = line.split(":", 1)[1].strip()
                    break
            else:
                cpu_name = uname.processor or uname.machine
    except Exception:
        cpu_name = uname.processor or uname.machine
    return {
        "cpu_name": cpu_name,
        "arch": uname.machine,
        "cpu_cores": psutil.cpu_count(logical=True) or 0,
        "os": f"{uname.system} {uname.release}",
        "kernel_version": uname.version,
        "mem_total": vm.total,
        "swap_total": sm.total,
        "disk_total": du.total,
        "version": "bigcat-agent/1.0.0",
        "name": socket.gethostname(),
    }


def collect_report(prev_net):
    """Build a Komari v2 Report dict. Returns (report, new_prev_net)."""
    import psutil

    cpu_usage = psutil.cpu_percent(interval=None)
    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()
    try:
        load1, load5, load15 = psutil.getloadavg()
    except Exception:
        load1 = load5 = load15 = 0.0
    du = psutil.disk_usage("/")
    net = psutil.net_io_counters()
    now = time.time()

    up = down = 0
    if prev_net is not None:
        dt = max(now - prev_net["t"], 0.001)
        up = max(int((net.bytes_sent - prev_net["sent"]) / dt), 0)
        down = max(int((net.bytes_recv - prev_net["recv"]) / dt), 0)
    new_prev = {"t": now, "sent": net.bytes_sent, "recv": net.bytes_recv}

    conns = psutil.net_connections(kind="inet")
    tcp = sum(1 for c in conns if c.type == socket.SOCK_STREAM)
    udp = sum(1 for c in conns if c.type == socket.SOCK_DGRAM)

    report = {
        "cpu": {"usage": round(cpu_usage, 2)},
        "ram": {"total": vm.total, "used": vm.total - vm.available},
        "swap": {"total": sm.total, "used": sm.used},
        "load": {"load1": round(load1, 2), "load5": round(load5, 2),
                 "load15": round(load15, 2)},
        "disk": {"total": du.total, "used": du.used},
        "network": {
            "up": up, "down": down,
            "totalUp": net.bytes_sent, "totalDown": net.bytes_recv,
        },
        "connections": {"tcp": tcp, "udp": udp},
        "uptime": int(time.time() - psutil.boot_time()),
        "process": len(psutil.pids()),
        "message": "",
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return report, new_prev


def main():
    ap = argparse.ArgumentParser(description="bigcat agent")
    ap.add_argument("--server", required=True, help="master URL, e.g. http://1.2.3.4:25774")
    ap.add_argument("--token", required=True, help="node token from /api/agent/register")
    ap.add_argument("--interval", type=float, default=2.0, help="report interval seconds")
    args = ap.parse_args()

    server = args.server.rstrip("/")
    print(f"[agent] reporting to {server} every {args.interval}s")

    # send static info once
    try:
        info = collect_basic_info()
        _post(f"{server}/api/agent/basicinfo", {"info": info}, token=args.token)
        print(f"[agent] basic info sent: {info['os']} / {info['cpu_cores']} cores")
    except Exception as e:  # noqa: BLE001
        print(f"[agent] basicinfo failed: {e}")

    prev_net = None
    # warm up cpu_percent
    try:
        import psutil
        psutil.cpu_percent(interval=None)
    except Exception:
        pass

    while True:
        try:
            report, prev_net = collect_report(prev_net)
            _post(f"{server}/api/agent/report", {"report": report}, token=args.token)
        except Exception as e:  # noqa: BLE001
            print(f"[agent] report failed: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
