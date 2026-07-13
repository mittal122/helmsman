"""CHANGE 5 — pre-fill deploy facts from the image itself, so the human/AI is asked only for what
inspection cannot know. `docker inspect` reveals the exposed ports, the USER the image runs as
(flagged if non-numeric — Kubernetes can't verify a named user for runAsNonRoot), declared
volumes, and entrypoint/cmd. Best-effort: returns {} if docker is unavailable or the pull fails.
"""
import json
import subprocess

PULL_TIMEOUT_S = 300
INSPECT_TIMEOUT_S = 30


def inspect_image(image: str, pull: bool = True) -> dict:
    if not image or image.startswith("-") or any(c.isspace() for c in image):
        return {}
    if pull:
        try:
            subprocess.run(["docker", "pull", image], capture_output=True, timeout=PULL_TIMEOUT_S)
        except Exception:
            pass  # maybe already present locally; inspect will tell us
    try:
        r = subprocess.run(["docker", "inspect", image], capture_output=True, text=True,
                           timeout=INSPECT_TIMEOUT_S)
    except Exception:
        return {}
    if r.returncode != 0:
        return {}
    try:
        data = json.loads(r.stdout)
    except Exception:
        return {}
    if not data:
        return {}
    cfg = data[0].get("Config") or {}
    ports = sorted({int(p.split("/")[0]) for p in (cfg.get("ExposedPorts") or {})
                    if p.split("/")[0].isdigit()})
    user = str(cfg.get("User") or "").strip()
    head = user.split(":")[0]
    user_numeric = int(head) if head.isdigit() else None
    return {
        "ports": ports,
        "user": user,
        "user_numeric": user_numeric,
        "user_is_named": bool(user) and user_numeric is None,
        "volumes": sorted((cfg.get("Volumes") or {}).keys()),
        "entrypoint": cfg.get("Entrypoint") or [],
        "cmd": cfg.get("Cmd") or [],
    }


def prefill_service(service: dict, insp: dict, warns: list) -> list:
    """Fill a service's unknown fields from image inspection. Returns the list of field names that
    inspection could NOT resolve and still need asking (e.g. no exposed port declared)."""
    filled = []
    if not insp:
        return filled
    # port: only when the service has no real port (8080 is the render placeholder)
    if service.get("workload", "deployment") == "deployment" and \
       (not service.get("port") or service.get("port") == 8080) and insp.get("ports"):
        service["port"] = insp["ports"][0]
        service["extra_ports"] = sorted(set(service.get("extra_ports", []) + insp["ports"][1:]))
        filled.append("port")
    # run_as_user: numeric UID from the image; a named user is flagged (can't verify runAsNonRoot)
    if service.get("run_as_user") is None and insp.get("user_numeric") is not None:
        service["run_as_user"] = insp["user_numeric"]
        filled.append("run_as_user")
    elif insp.get("user_is_named") and service.get("run_as_user") is None:
        warns.append(f"{service.get('name','')}: the image runs as named user '{insp['user']}' — "
                     f"Kubernetes runAsNonRoot needs a numeric UID; confirm it.")
    # declared volumes the manifest didn't mention (data dirs that need a PVC)
    if insp.get("volumes"):
        have = {v.get("mountPath") for v in service.get("volumes", [])}
        for i, mp in enumerate(insp["volumes"]):
            if mp not in have:
                nm = (service.get("name", "vol") + "-vol" + (str(i) if i else ""))
                service.setdefault("volumes", []).append({"name": nm, "mountPath": mp, "size": "1Gi"})
                filled.append("volumes")
    return filled


if __name__ == "__main__":
    # prefill logic is pure and testable without docker
    svc = {"name": "web", "workload": "deployment", "port": 8080, "run_as_user": None, "volumes": []}
    insp = {"ports": [3000, 9090], "user": "1001", "user_numeric": 1001, "user_is_named": False,
            "volumes": ["/data"]}
    filled = prefill_service(svc, insp, [])
    assert svc["port"] == 3000 and svc["extra_ports"] == [9090], svc
    assert svc["run_as_user"] == 1001
    assert svc["volumes"][0]["mountPath"] == "/data"
    assert set(filled) == {"port", "run_as_user", "volumes"}, filled
    w = []
    prefill_service({"name": "a", "run_as_user": None}, {"user": "node", "user_is_named": True}, w)
    assert any("named user 'node'" in x for x in w), w
    print("inspect.py prefill self-check OK")
