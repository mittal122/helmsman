import yaml

HOURS = 730
PRICE = {"cpu_hour": 0.0335, "gb_hour": 0.0045}   # rough blended on-demand; a tuning knob

def _cpu(v) -> float:
    s = str(v)
    return float(s[:-1]) / 1000 if s.endswith("m") else float(s)

_UNIT = {"Ki": 1 / (1024 ** 2), "Mi": 1 / 1024, "Gi": 1.0, "Ti": 1024.0}
def _gib(v) -> float:
    s = str(v)
    for u, f in _UNIT.items():
        if s.endswith(u):
            return float(s[:-2]) * f
    return float(s) / (1024 ** 3)   # bare bytes

def estimate(manifests: str) -> dict:
    vcpu = 0.0
    gib = 0.0
    for doc in yaml.safe_load_all(manifests):
        if not isinstance(doc, dict) or doc.get("kind") != "Deployment":
            continue
        spec = doc.get("spec", {})
        replicas = int(spec.get("replicas", 1))
        for c in spec.get("template", {}).get("spec", {}).get("containers", []):
            req = (c.get("resources") or {}).get("requests") or {}
            if "cpu" in req:
                vcpu += _cpu(req["cpu"]) * replicas
            if "memory" in req:
                gib += _gib(req["memory"]) * replicas
    cpu_usd = round(vcpu * PRICE["cpu_hour"] * HOURS, 2)
    mem_usd = round(gib * PRICE["gb_hour"] * HOURS, 2)
    return {"monthly_usd": round(cpu_usd + mem_usd, 2),
            "breakdown": {"cpu_usd": cpu_usd, "mem_usd": mem_usd},
            "assumptions": f"requests-based, {HOURS} h/mo, blended on-demand pricing"}
