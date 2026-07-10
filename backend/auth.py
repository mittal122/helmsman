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
import logging
import os
import secrets
import time

import jwt
from argon2 import PasswordHasher
from fastapi import Depends, Header, HTTPException, Request

import store

log = logging.getLogger("helmsman")
ROLES = {"viewer": 1, "operator": 2, "admin": 3}

# JWT signing key: env only. NO hardcoded fallback (a known secret in the repo could
# forge tokens). If unset, use a random per-process secret — sessions won't survive a
# restart, which nudges production to set JWT_SECRET while staying safe by default.
_env_secret = os.environ.get("JWT_SECRET") or os.environ.get("AUTH_TOKEN")
if _env_secret:
    JWT_SECRET = _env_secret
else:
    JWT_SECRET = secrets.token_urlsafe(48)
    log.warning("JWT_SECRET not set — using an ephemeral random secret (sessions reset on restart)")
JWT_TTL_S = int(os.environ.get("JWT_TTL_S", "43200"))   # 12h
_ph = PasswordHasher()
_user_ctx: contextvars.ContextVar = contextvars.ContextVar("user", default=None)

# ---------- passwords ----------
def hash_password(p: str) -> str:
    return _ph.hash(p)

def verify_password(pw_hash: str, p: str) -> bool:
    try:
        return bool(_ph.verify(pw_hash, p))
    except Exception:
        return False

# precomputed hash of a random string: verify a login attempt against THIS when the email
# doesn't exist, so a missing user costs the same argon2 time as a real one (no timing
# oracle that distinguishes valid from invalid emails).
DUMMY_HASH = _ph.hash(secrets.token_urlsafe(16))

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
        return True   # fail CLOSED: if we can't tell, assume configured -> enforce auth

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
            # re-consult the store so deactivation/role-change take effect immediately —
            # a signed JWT alone would keep a revoked/demoted user's access until expiry.
            # Store hiccup (returns None/raises) falls back to the token so a transient DB
            # blip / in-memory restart doesn't lock everyone out; an explicit inactive
            # record still revokes (deactivation sets active=False, keeping the record).
            rec = None
            try:
                rec = await store.user_get(payload["sub"])
            except Exception:
                rec = None
            if rec is not None and not rec.get("active", True):
                raise HTTPException(status_code=401, detail="account deactivated")
            role = rec.get("role") if rec else payload.get("role", "viewer")
            u = {"email": payload["sub"], "role": role}
            _user_ctx.set(u); return u
    # secure by default: open (no-auth) admin mode only when EXPLICITLY enabled AND
    # nothing is configured. Production leaves ALLOW_OPEN_DEV unset -> always enforced.
    if os.environ.get("ALLOW_OPEN_DEV") == "1" and not await _auth_configured():
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
