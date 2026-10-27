import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from uuid import uuid4

from .errors import Rejection
from .logging_config import audit_event


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Database:
    def __init__(self, path, environment):
        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connection() as c:
            c.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS controls (id INTEGER PRIMARY KEY CHECK(id=1), halted INTEGER NOT NULL);
                INSERT OR IGNORE INTO controls VALUES (1, 0);
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY, terms TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY, approval_id TEXT UNIQUE, kind TEXT NOT NULL,
                    payload TEXT NOT NULL, hash TEXT NOT NULL, status TEXT NOT NULL,
                    result TEXT, order_id TEXT, reserve_cents INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL, submitted_at REAL, terminal_observed_at REAL,
                    daily_risk_cents INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS placement_gate (id INTEGER PRIMARY KEY CHECK(id=1), request_id TEXT UNIQUE NOT NULL);
                CREATE TABLE IF NOT EXISTS order_states (order_id TEXT PRIMARY KEY, state TEXT NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, record TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS oauth (
                    kind TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, expires REAL NOT NULL,
                    PRIMARY KEY(kind, key));
            """)
            if "terminal_observed_at" not in {r[1] for r in c.execute("PRAGMA table_info(requests)")}:
                c.execute("ALTER TABLE requests ADD COLUMN terminal_observed_at REAL")
            if "daily_risk_cents" not in {r[1] for r in c.execute("PRAGMA table_info(requests)")}:
                c.execute("ALTER TABLE requests ADD COLUMN daily_risk_cents INTEGER NOT NULL DEFAULT 0")
                for old in c.execute("SELECT * FROM requests WHERE kind='place' AND submitted_at IS NOT NULL").fetchall():
                    if old["status"] == "rejected":
                        continue
                    terms = json.loads(old["payload"])
                    price = terms["limit_price_cents"]
                    cost = terms["contracts"] * ((price if terms["action"] == "buy" else 100 - price) + 100)
                    c.execute("UPDATE requests SET daily_risk_cents=? WHERE request_id=?", (cost, old["request_id"]))
            row = c.execute("SELECT value FROM metadata WHERE key='environment'").fetchone()
            if row and row[0] != environment:
                raise ValueError("Use a separate database for each Kalshi environment")
            c.execute("INSERT OR IGNORE INTO metadata VALUES ('environment', ?)", (environment,))
        path.chmod(0o600)

    @contextmanager
    def connection(self):
        c = sqlite3.connect(self.path, timeout=5)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA synchronous=FULL")
        try:
            with c:
                yield c
        finally:
            c.close()

    def audit(self, event, **fields):
        record = audit_event(event, **fields)
        with self.connection() as c:
            c.execute("INSERT INTO audit(record) VALUES (?)", (canonical(record),))

    def halted(self):
        with self.connection() as c:
            return bool(c.execute("SELECT halted FROM controls WHERE id=1").fetchone()[0])

    def halt(self, halted):
        with self.connection() as c:
            c.execute("UPDATE controls SET halted=? WHERE id=1", (int(halted),))
        self.audit("owner_kill_switch", halted=halted)

    def approve(self, terms):
        approval_id = str(uuid4())
        with self.connection() as c:
            c.execute("INSERT INTO approvals VALUES (?, ?, ?)",
                      (approval_id, canonical(terms), time.time() + 900))
        self.audit("owner_approval", approval_id=approval_id, terms=terms)
        return approval_id

    def get(self, request_id):
        with self.connection() as c:
            row = c.execute("SELECT * FROM requests WHERE request_id=?", (str(request_id),)).fetchone()
            return dict(row) if row else None

    def claim(self, request_id, payload, *, terms=None, approval_id=None, kind="place", require_approval=True):
        """Reserve IDs and a global placement gate in one durable transaction, before any HTTP write."""
        request_id = str(request_id)
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            existing = c.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                if existing["hash"] != digest(payload) or existing["kind"] != kind:
                    raise Rejection("request_id_conflict")
                return dict(existing)
            if kind == "place":
                if (c.execute("SELECT 1 FROM placement_gate").fetchone() or c.execute(
                    "SELECT 1 FROM requests WHERE kind='place' AND status IN ('validating', 'submitting', 'unknown')"
                ).fetchone()):
                    raise Rejection("unresolved_execution_blocks_placement")
                if require_approval or approval_id is not None:
                    approval = c.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
                    if not approval or approval["expires"] < time.time() or approval["terms"] != canonical(terms):
                        raise Rejection("missing_expired_or_mismatched_owner_approval")
                    if c.execute("SELECT 1 FROM requests WHERE approval_id=?", (approval_id,)).fetchone():
                        raise Rejection("approval_already_used")
            c.execute("INSERT INTO requests(request_id, approval_id, kind, payload, hash, status, created) "
                      "VALUES (?, ?, ?, ?, ?, 'validating', ?)",
                      (request_id, approval_id, kind, canonical(payload), digest(payload), time.time()))
            if kind == "place":
                c.execute("INSERT INTO placement_gate VALUES (1, ?)", (request_id,))
        return None

    def check_approval(self, approval_id):
        with self.connection() as c:
            row = c.execute("SELECT expires FROM approvals WHERE approval_id=?", (str(approval_id),)).fetchone()
            if not row or row[0] < time.time():
                raise Rejection("owner_approval_expired")

    def daily_budget(self, limit, now=None):
        now = time.time() if now is None else now
        with self.connection() as c:
            used = c.execute("SELECT COALESCE(SUM(daily_risk_cents), 0) FROM requests WHERE submitted_at>=?",
                             (int(now) // 86400 * 86400,)).fetchone()[0]
        return {"limit_cents": limit, "committed_cents": used, "remaining_cents": max(0, limit - used),
                "resets_at_unix": (int(now) // 86400 + 1) * 86400, "timezone": "UTC"}

    def submitting(self, request_id, reserve, daily_limit=10000):
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT halted FROM controls WHERE id=1").fetchone()[0]:
                raise Rejection("owner_kill_switch_active")
            now = time.time()
            committed = c.execute("SELECT COALESCE(SUM(daily_risk_cents), 0) FROM requests WHERE submitted_at>=?",
                                  (int(now) // 86400 * 86400,)).fetchone()[0]
            if committed + reserve > daily_limit:
                raise Rejection("owner_daily_order_cost_limit")
            c.execute("UPDATE requests SET status='submitting', reserve_cents=?, daily_risk_cents=?, submitted_at=? WHERE request_id=?",
                      (reserve, reserve, now, str(request_id)))

    def finish(self, request_id, result, *, reserve=None, keep_gate=False):
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE requests SET status=?, result=?, order_id=COALESCE(?, order_id) WHERE request_id=?",
                      (result["status"], canonical(result), result.get("order_id"), str(request_id)))
            if reserve is not None:
                c.execute("UPDATE requests SET reserve_cents=? WHERE request_id=?", (reserve, str(request_id)))
            if result["status"] == "rejected":
                c.execute("UPDATE requests SET daily_risk_cents=0 WHERE request_id=?", (str(request_id),))
            if result["status"] in {"filled", "cancelled", "expired", "rejected"}:
                c.execute("UPDATE requests SET terminal_observed_at=COALESCE(terminal_observed_at, ?) WHERE request_id=?",
                          (time.time(), str(request_id)))
            if keep_gate:
                row = c.execute("SELECT kind FROM requests WHERE request_id=?", (str(request_id),)).fetchone()
                if row and row[0] == "place":
                    c.execute("INSERT OR IGNORE INTO placement_gate VALUES (1, ?)", (str(request_id),))
            else:
                c.execute("DELETE FROM placement_gate WHERE request_id=?", (str(request_id),))
            if result.get("order_id"):
                c.execute("INSERT OR REPLACE INTO order_states VALUES (?, ?, ?)",
                          (result["order_id"], canonical(result), time.time()))
        self.audit("execution_result", request_id=str(request_id), result=result)

    def observe(self, order_id, result):
        with self.connection() as c:
            c.execute("INSERT OR REPLACE INTO order_states VALUES (?, ?, ?)",
                      (str(order_id), canonical(result), time.time()))

    def reservations(self):
        with self.connection() as c:
            return [dict(r) for r in c.execute("SELECT * FROM requests WHERE kind='place' AND reserve_cents>0")]

    def clear_reserve(self, request_id):
        with self.connection() as c:
            c.execute("UPDATE requests SET reserve_cents=0 WHERE request_id=?", (request_id,))

    def for_order(self, order_id):
        with self.connection() as c:
            row = c.execute("SELECT * FROM requests WHERE order_id=? AND kind='place'", (str(order_id),)).fetchone()
            return dict(row) if row else None

    def recover_validating(self):
        """Only at startup, after the server has acquired the exclusive process lock."""
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            for row in c.execute("SELECT request_id FROM requests WHERE status='validating'").fetchall():
                result = {"status": "rejected", "reason": "interrupted_before_submission",
                          "request_id": row[0], "submitted": False}
                c.execute("UPDATE requests SET status='rejected', result=? WHERE request_id=?",
                          (canonical(result), row[0]))
                c.execute("DELETE FROM placement_gate WHERE request_id=?", (row[0],))

    def secret_put(self, kind, key, value, expires):
        with self.connection() as c:
            c.execute("DELETE FROM oauth WHERE expires<?", (time.time(),))
            if c.execute("SELECT count(*) FROM oauth").fetchone()[0] >= 5000:
                raise Rejection("oauth_capacity_reached")
            c.execute("INSERT OR REPLACE INTO oauth VALUES (?, ?, ?, ?)", (kind, key, canonical(value), expires))

    def secret_get(self, kind, key, consume=False):
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT value, expires FROM oauth WHERE kind=? AND key=?", (kind, key)).fetchone()
            if consume:
                c.execute("DELETE FROM oauth WHERE kind=? AND key=?", (kind, key))
            return json.loads(row[0]) if row and row[1] > time.time() else None

    def revoke_oauth(self):
        with self.connection() as c:
            c.execute("DELETE FROM oauth WHERE kind IN ('access', 'refresh', 'code', 'pending')")
