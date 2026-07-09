import base64
import copy
import json

REDACTED = "••••"

def secret_variants(secrets: dict) -> set[str]:
    out: set[str] = set()
    for v in (secrets or {}).values():
        if not v:
            continue
        s = str(v)
        out.add(s)
        out.add(base64.b64encode(s.encode()).decode())
        esc = json.dumps(s)[1:-1]   # go/helm `quote`-style escaping of " \ newline tab
        if esc != s:
            out.add(esc)
    return out

def _redact_str(s: str, variants: set[str]) -> str:
    for v in variants:
        if v and v in s:
            s = s.replace(v, REDACTED)
    return s

def redact(obj, variants: set[str]):
    # ponytail: no secrets registered -> nothing to redact; returns the original by
    # reference (callers in this path must not assume they got a fresh copy)
    if not variants:
        return obj
    obj = copy.deepcopy(obj)

    def walk(x):
        if isinstance(x, str):
            return _redact_str(x, variants)
        if isinstance(x, bytes):
            return _redact_str(x.decode("utf-8", "replace"), variants)
        if isinstance(x, dict):
            return {(_redact_str(k, variants) if isinstance(k, str) else k): walk(v)
                    for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        if isinstance(x, tuple):
            return tuple(walk(v) for v in x)
        return x

    return walk(obj)
