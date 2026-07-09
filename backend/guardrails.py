import base64
import copy

REDACTED = "••••"

def secret_variants(secrets: dict) -> set[str]:
    out: set[str] = set()
    for v in (secrets or {}).values():
        if not v:
            continue
        s = str(v)
        out.add(s)
        out.add(base64.b64encode(s.encode()).decode())
    return out

def _redact_str(s: str, variants: set[str]) -> str:
    for v in variants:
        if v and v in s:
            s = s.replace(v, REDACTED)
    return s

def redact(obj, variants: set[str]):
    if not variants:
        return obj
    obj = copy.deepcopy(obj)

    def walk(x):
        if isinstance(x, str):
            return _redact_str(x, variants)
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        return x

    return walk(obj)
