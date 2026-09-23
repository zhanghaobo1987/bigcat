"""
bigcat storage layer.

SQLite-backed persistence for nodes (clients) and their metric history.
Mirrors the Komari data model (database/models) in a simplified form.
"""
import json
import sqlite3
import threading
import time
import uuid as uuid_lib
from pathlib import Path


class Storage:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    # ------------------------------------------------------------------ schema
    def _init_schema(self):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS clients (
                    uuid            TEXT PRIMARY KEY,
                    token           TEXT UNIQUE NOT NULL,
                    name            TEXT DEFAULT '',
                    cpu_name        TEXT DEFAULT '',
                    virtualization  TEXT DEFAULT '',
                    arch            TEXT DEFAULT '',
                    cpu_cores       INTEGER DEFAULT 0,
                    os              TEXT DEFAULT '',
                    kernel_version  TEXT DEFAULT '',
                    gpu_name        TEXT DEFAULT '',
                    ipv4            TEXT DEFAULT '',
                    ipv6            TEXT DEFAULT '',
                    region          TEXT DEFAULT '',
                    remark          TEXT DEFAULT '',
                    public_remark   TEXT DEFAULT '',
                    mem_total       INTEGER DEFAULT 0,
                    swap_total      INTEGER DEFAULT 0,
                    disk_total      INTEGER DEFAULT 0,
                    version         TEXT DEFAULT '',
                    weight          INTEGER DEFAULT 0,
                    price           REAL DEFAULT 0,
                    billing_cycle   INTEGER DEFAULT 0,
                    auto_renewal    INTEGER DEFAULT 0,
                    currency        TEXT DEFAULT '$',
                    expired_at      TEXT,
                    group_name      TEXT DEFAULT '',
                    tags            TEXT DEFAULT '',
                    hidden          INTEGER DEFAULT 0,
                    traffic_limit   INTEGER DEFAULT 0,
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL,
                    last_report_at  TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    client          TEXT NOT NULL,
                    time            TEXT NOT NULL,
                    cpu             REAL DEFAULT 0,
                    gpu             REAL DEFAULT 0,
                    ram             INTEGER DEFAULT 0,
                    ram_total       INTEGER DEFAULT 0,
                    swap            INTEGER DEFAULT 0,
                    swap_total      INTEGER DEFAULT 0,
                    load            REAL DEFAULT 0,
                    disk            INTEGER DEFAULT 0,
                    disk_total      INTEGER DEFAULT 0,
                    net_in          INTEGER DEFAULT 0,
                    net_out         INTEGER DEFAULT 0,
                    net_total_up    INTEGER DEFAULT 0,
                    net_total_down  INTEGER DEFAULT 0,
                    traffic_up      INTEGER DEFAULT 0,
                    traffic_down    INTEGER DEFAULT 0,
                    process         INTEGER DEFAULT 0,
                    connections     INTEGER DEFAULT 0,
                    connections_udp INTEGER DEFAULT 0,
                    uptime          INTEGER DEFAULT 0
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_records_client ON records(client)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_records_time ON records(time)")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self._conn.commit()
            # default settings
            defaults = {
                "sitename": "bigcat Monitor",
                "description": "A lightweight VPS monitor, Komari-compatible.",
                "theme": "LuminaPlus",
                "admin_password": "",  # set on first run via CLI
            }
            for k, v in defaults.items():
                cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v))
            self._conn.commit()

    # ------------------------------------------------------------------ clients
    def _row_to_client(self, row) -> dict:
        d = dict(row)
        # map internal column names back to komari json names
        d["group"] = d.pop("group_name", "")
        return d

    def add_client(self, name: str = "", token: str = "") -> dict:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        client_uuid = str(uuid_lib.uuid4())
        if not token:
            token = uuid_lib.uuid4().hex + uuid_lib.uuid4().hex[:16]
        with self._lock:
            self._conn.execute(
                """INSERT INTO clients(uuid, token, name, created_at, updated_at)
                   VALUES(?, ?, ?, ?, ?)""",
                (client_uuid, token, name, now, now),
            )
            self._conn.commit()
        return self.get_client(client_uuid)

    def get_client(self, client_uuid: str):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM clients WHERE uuid = ?", (client_uuid,)
            ).fetchone()
        return self._row_to_client(row) if row else None

    def get_client_by_token(self, token: str):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM clients WHERE token = ?", (token,)
            ).fetchone()
        return self._row_to_client(row) if row else None

    def list_clients(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM clients ORDER BY weight DESC, created_at ASC"
            ).fetchall()
        return [self._row_to_client(r) for r in rows]

    def update_client(self, client_uuid: str, fields: dict) -> bool:
        allowed = {
            "name", "cpu_name", "virtualization", "arch", "cpu_cores", "os",
            "kernel_version", "gpu_name", "ipv4", "ipv6", "region", "remark",
            "public_remark", "mem_total", "swap_total", "disk_total", "version",
            "weight", "price", "billing_cycle", "auto_renewal", "currency",
            "expired_at", "group", "tags", "hidden", "traffic_limit",
        }
        sets, vals = [], []
        for k, v in fields.items():
            col = "group_name" if k == "group" else k
            if k not in allowed:
                continue
            sets.append(f"{col} = ?")
            vals.append(v)
        if not sets:
            return False
        sets.append("updated_at = ?")
        vals.append(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        vals.append(client_uuid)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE clients SET {', '.join(sets)} WHERE uuid = ?", vals
            )
            self._conn.commit()
        return cur.rowcount > 0

    def touch_client(self, client_uuid: str):
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock:
            self._conn.execute(
                "UPDATE clients SET last_report_at = ?, updated_at = ? WHERE uuid = ?",
                (now, now, client_uuid),
            )
            self._conn.commit()

    def delete_client(self, client_uuid: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM clients WHERE uuid = ?", (client_uuid,))
            self._conn.execute("DELETE FROM records WHERE client = ?", (client_uuid,))
            self._conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------ records
    def insert_record(self, client_uuid: str, rec: dict):
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        cols = (
            "client", "time", "cpu", "gpu", "ram", "ram_total", "swap",
            "swap_total", "load", "disk", "disk_total", "net_in", "net_out",
            "net_total_up", "net_total_down", "traffic_up", "traffic_down",
            "process", "connections", "connections_udp", "uptime",
        )
        vals = [client_uuid, rec.get("time", now)] + [rec.get(c, 0) for c in cols[2:]]
        with self._lock:
            self._conn.execute(
                f"INSERT INTO records({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
                vals,
            )
            self._conn.commit()

    def query_records(self, client_uuid: str, since: str, until: str, limit: int = 500):
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM records
                   WHERE client = ? AND time >= ? AND time <= ?
                   ORDER BY time ASC LIMIT ?""",
                (client_uuid, since, until, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def latest_record(self, client_uuid: str):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM records WHERE client = ? ORDER BY time DESC LIMIT 1",
                (client_uuid,),
            ).fetchone()
        return dict(row) if row else None

    def prune_records(self, keep_hours: int = 24):
        """Drop records older than keep_hours to bound DB size."""
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - keep_hours * 3600)
        )
        with self._lock:
            self._conn.execute("DELETE FROM records WHERE time < ?", (cutoff,))
            self._conn.commit()

    def count_records(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM records").fetchone()
        return int(row["n"]) if row else 0

    def count_records_since(self, since_iso: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE time >= ?", (since_iso,)
            ).fetchone()
        return int(row["n"]) if row else 0

    def db_size(self) -> int:
        try:
            return self.db_path.stat().st_size
        except Exception:
            return 0

    # ------------------------------------------------------------------ settings
    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str):
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    def get_public_settings(self) -> dict:
        return {
            "sitename": self.get_setting("sitename"),
            "description": self.get_setting("description"),
            "theme": self.get_setting("theme"),
            "private_site": False,
            "allow_register": False,
        }

    # ------------------------------------------------------------------ auth
    def verify_admin(self, password: str, username: str = "") -> bool:
        import hashlib

        stored = self.get_setting("admin_password")
        if not stored:
            return False
        if stored != hashlib.sha256(password.encode()).hexdigest():
            return False
        # 用户名校验：未设置用户名时只校验密码，保持向后兼容
        stored_user = self.get_setting("admin_username") or "admin"
        if username:
            return username == stored_user
        return True

    def set_admin(self, username: str, password: str):
        import hashlib

        self.set_setting("admin_username", username or "admin")
        self.set_setting("admin_password", hashlib.sha256(password.encode()).hexdigest())

    def set_admin_password(self, password: str):
        """向后兼容：只改密码，保留已有用户名（默认为 admin）"""
        import hashlib

        if not self.get_setting("admin_username"):
            self.set_setting("admin_username", "admin")
        self.set_setting("admin_password", hashlib.sha256(password.encode()).hexdigest())

    def get_admin_username(self) -> str:
        return self.get_setting("admin_username") or "admin"

    def has_admin(self) -> bool:
        return bool(self.get_setting("admin_password"))
