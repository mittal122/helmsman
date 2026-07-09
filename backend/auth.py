import hmac
import os
from fastapi import Header, HTTPException

def require_token(authorization: str | None = Header(None)) -> None:
    token = os.environ.get("AUTH_TOKEN")
    if not token:
        return None  # default-open: no token configured (dev/kind heritage)
    expected = "Bearer " + token
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="invalid or missing token")
    return None
