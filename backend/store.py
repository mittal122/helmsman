"""Durable store — event history + audit log.

Production uses Postgres (set DATABASE_URL); with no DATABASE_URL (local/dev/tests) it
falls back to an in-memory ring buffer, so nothing breaks without a database. Every
write is best-effort: a store failure must NEVER break a deploy or an action.

Two streams:
- events: the deploy activity stream (survives restart -> history/replay).
- audit:  every mutating action (who/what/when/ok) -> compliance trail.
"""
import collections
import json
import os

DATABASE_URL = os.environ.get("DATABASE_URL", "")
_MAX_MEM = 5000

# ---------- in-memory (default) ----------
class _InMem:
    def __init__(self):
        self.events = collections.deque(maxlen=_MAX_MEM)
        self.audit = collections.deque(maxlen=_MAX_MEM)
        self.users = {}     # email -> {id,email,pw_hash,role,active}
        self._uid = 0
    async def init(self): return
    async def append_event(self, e): self.events.append(e)
    async def recent_events(self, limit): return list(self.events)[-limit:]
    async def append_audit(self, r): self.audit.append(r)
    async def recent_audit(self, limit): return list(self.audit)[-limit:][::-1]
    async def healthy(self): return True
    async def close(self): return
    # users
    async def user_create(self, email, pw_hash, role):
        if email in self.users: raise ValueError("email already exists")
        self._uid += 1
        self.users[email] = {"id": self._uid, "email": email, "pw_hash": pw_hash, "role": role, "active": True}
        return self.users[email]
    async def user_get(self, email): return self.users.get(email)
    async def user_list(self):
        return [{"email": u["email"], "role": u["role"], "active": u["active"]} for u in self.users.values()]
    async def user_set_role(self, email, role):
        if email in self.users: self.users[email]["role"] = role
    async def user_set_active(self, email, active):
        if email in self.users: self.users[email]["active"] = active
    async def user_count(self): return len(self.users)

# ---------- postgres (production) ----------
class _Postgres:
    def __init__(self, dsn): self.dsn = dsn; self.pool = None
    async def init(self):
        import asyncpg
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=5)
        async with self.pool.acquire() as c:
            await c.execute("""CREATE TABLE IF NOT EXISTS events(
                id BIGSERIAL PRIMARY KEY, ts DOUBLE PRECISION, type TEXT, stage TEXT,
                message TEXT, data JSONB, created TIMESTAMPTZ DEFAULT now())""")
            await c.execute("""CREATE TABLE IF NOT EXISTS audit(
                id BIGSERIAL PRIMARY KEY, actor TEXT, action TEXT, target TEXT,
                ok BOOLEAN, detail TEXT, created TIMESTAMPTZ DEFAULT now())""")
            await c.execute("""CREATE TABLE IF NOT EXISTS users(
                id BIGSERIAL PRIMARY KEY, email TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'viewer', active BOOLEAN NOT NULL DEFAULT true,
                created TIMESTAMPTZ DEFAULT now())""")
    async def append_event(self, e):
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO events(ts,type,stage,message,data) VALUES($1,$2,$3,$4,$5)",
                e.get("ts"), e.get("type"), e.get("stage"), e.get("message"),
                json.dumps(e.get("data") or {}))
    async def recent_events(self, limit):
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                "SELECT ts,type,stage,message,data FROM events ORDER BY id DESC LIMIT $1", limit)
        return [dict(ts=r["ts"], type=r["type"], stage=r["stage"], message=r["message"],
                     data=json.loads(r["data"] or "{}")) for r in reversed(rows)]
    async def append_audit(self, r):
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO audit(actor,action,target,ok,detail) VALUES($1,$2,$3,$4,$5)",
                r["actor"], r["action"], r["target"], r["ok"], r.get("detail", ""))
    async def recent_audit(self, limit):
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                "SELECT actor,action,target,ok,detail,created FROM audit ORDER BY id DESC LIMIT $1", limit)
        return [dict(actor=r["actor"], action=r["action"], target=r["target"],
                     ok=r["ok"], detail=r["detail"], created=str(r["created"])) for r in rows]
    async def healthy(self):
        try:
            async with self.pool.acquire() as c:
                await c.execute("SELECT 1")
            return True
        except Exception:
            return False
    async def close(self):
        if self.pool:
            await self.pool.close()
    # users
    async def user_create(self, email, pw_hash, role):
        async with self.pool.acquire() as c:
            try:
                r = await c.fetchrow(
                    "INSERT INTO users(email,pw_hash,role) VALUES($1,$2,$3) RETURNING id,email,role,active",
                    email, pw_hash, role)
            except Exception as e:
                raise ValueError("email already exists") from e
        return dict(r)
    async def user_get(self, email):
        async with self.pool.acquire() as c:
            r = await c.fetchrow("SELECT id,email,pw_hash,role,active FROM users WHERE email=$1", email)
        return dict(r) if r else None
    async def user_list(self):
        async with self.pool.acquire() as c:
            rows = await c.fetch("SELECT email,role,active FROM users ORDER BY email")
        return [dict(r) for r in rows]
    async def user_set_role(self, email, role):
        async with self.pool.acquire() as c:
            await c.execute("UPDATE users SET role=$2 WHERE email=$1", email, role)
    async def user_set_active(self, email, active):
        async with self.pool.acquire() as c:
            await c.execute("UPDATE users SET active=$2 WHERE email=$1", email, active)
    async def user_count(self):
        async with self.pool.acquire() as c:
            return int(await c.fetchval("SELECT count(*) FROM users"))

_impl = None

def backend_name() -> str:
    return "postgres" if isinstance(_impl, _Postgres) else "memory"

async def init() -> str:
    """Pick the backend. Returns a human status; falls back to memory on any DB error."""
    global _impl
    if DATABASE_URL:
        try:
            _impl = _Postgres(DATABASE_URL)
            await _impl.init()
            return "postgres"
        except Exception as e:
            _impl = _InMem()
            return f"postgres unavailable ({e}) — using in-memory"
    _impl = _InMem()
    return "memory"

async def append_event(e: dict) -> None:
    if _impl:
        try:
            await _impl.append_event(e)
        except Exception:
            pass
async def recent_events(limit: int = 200) -> list:
    return await _impl.recent_events(limit) if _impl else []
async def append_audit(actor: str, action: str, target: str, ok: bool = True, detail: str = "") -> None:
    if _impl:
        try:
            await _impl.append_audit({"actor": actor, "action": action, "target": target,
                                      "ok": ok, "detail": detail})
        except Exception:
            pass
async def recent_audit(limit: int = 200) -> list:
    return await _impl.recent_audit(limit) if _impl else []
async def healthy() -> bool:
    return await _impl.healthy() if _impl else True
async def close() -> None:
    global _impl
    if _impl:
        await _impl.close()
    _impl = None

# ---------- users (durable identity) ----------
async def user_create(email: str, pw_hash: str, role: str) -> dict:
    if not _impl:
        raise RuntimeError("store not initialized")
    return await _impl.user_create(email, pw_hash, role)
async def user_get(email: str):
    return await _impl.user_get(email) if _impl else None
async def user_list() -> list:
    return await _impl.user_list() if _impl else []
async def user_set_role(email: str, role: str) -> None:
    if _impl:
        await _impl.user_set_role(email, role)
async def user_set_active(email: str, active: bool) -> None:
    if _impl:
        await _impl.user_set_active(email, active)
async def user_count() -> int:
    return await _impl.user_count() if _impl else 0
