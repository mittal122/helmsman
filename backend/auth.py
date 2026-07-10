"""Identity + RBAC.

Multi-user auth: accounts (argon2-hashed passwords) in the durable store, short-lived
JWT sessions, three roles (viewer < operator < admin). The single operator token
(AUTH_TOKEN) is kept as a bootstrap-admin credential so existing flows/local dev work.

Enforcement model (backward-compatible):
- A valid JWT or the operator token -> that identity.
- Nothing valid AND no auth configured (no AUTH_TOKEN, no users) -> open dev mode
  (synthetic admin), so local `./run.sh` keeps working with zero setup.
- Nothing valid but auth IS configured -> 401.
"""
import contextvars
import hmac
import os
import time

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, Header, HTTPException, Request

import store

ROLES = {"viewer": 1, "operator": 2, "admin": 3}
JWT_SECRET = (os.environ.get("JWT_SECRET") or os.environ.get("AUTH_TOKEN")
              or "dev-insecure-secret-change-me-in-production-please")   # >=32 bytes
JWT_TTL_S = int(os.environ.get("JWT_TTL_S", "43200"))   # 12h
_ph = PasswordHasher()
_user_ctx: contextvars.ContextVar = contextvars.ContextVar("user", default=None)

# ---------- passwords ----------
def hash_password(p: str) -> str:
    return _ph.hash(p)

def verify_password(pw_hash: str, p: str) -> bool:
    try:
        _ph.verify(pw_hash, p)
        return True
    except Exception:
        return False

# ---------- JWT ----------
def make_token(email: str, role: str) -> str:
    now = int(time.time())
    return jwt.encode({"sub": email, "role": role, "iat": now, "exp": now + JWT_TTL_S},
                      JWT_SECRET, algorithm="HS256")

def _decode(token: str):
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except Exception:
        return None

async def _auth_configured() -> bool:
    if os.environ.get("AUTH_TOKEN"):
        return True
    try:
        return (await store.user_count()) > 0
    except Exception:
        return False

# ---------- identity dependency ----------
async def current_user(request: Request, authorization: str = Header(None)) -> dict:
    tok = None
    if authorization and authorization.startswith("Bearer "):
        tok = authorization[7:]
    if tok is None:
        tok = request.cookies.get("helmsman_session")

    op = os.environ.get("AUTH_TOKEN")
    if op and tok and hmac.compare_digest(tok, op):
        u = {"email": "operator@local", "role": "admin"}
        _user_ctx.set(u); return u
    if tok:
        payload = _decode(tok)
        if payload and payload.get("sub"):
            u = {"email": payload["sub"], "role": payload.get("role", "viewer")}
            _user_ctx.set(u); return u
    if not await _auth_configured():
        u = {"email": "local@dev", "role": "admin"}
        _user_ctx.set(u); return u
    raise HTTPException(status_code=401, detail="authentication required")

def require_role(min_role: str):
    async def dep(user: dict = Depends(current_user)) -> dict:
        if ROLES.get(user["role"], 0) < ROLES.get(min_role, 99):
            raise HTTPException(status_code=403, detail=f"requires {min_role} role")
        return user
    return dep

def actor() -> str:
    u = _user_ctx.get()
    return u["email"] if u else "system"

# backward-compat alias — operator-level gate
require_token = require_role("operator")
