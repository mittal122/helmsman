import os
import subprocess
import tempfile
import yaml

CHART_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "chart"))

DEFAULT_RESOURCES = {
    "requests": {"cpu": "50m", "memory": "64Mi"},
    "limits": {"cpu": "500m", "memory": "256Mi"},
}

def build_values(cfg: dict) -> dict:
    replicas = int(cfg.get("replicas", 2))
    values = {
        "name": cfg["name"],
        "image": cfg["image"],
        "port": int(cfg.get("port", 8080)),
        "replicas": replicas,
        "env": dict(cfg.get("env") or {}),
        "secrets": dict(cfg.get("secrets") or {}),
        "ingress": {
            "enabled": bool(cfg.get("ingress_host")),
            "host": cfg.get("ingress_host") or "",
        },
        "hpa": {
            "enabled": bool(cfg.get("hpa_enabled")),
            "minReplicas": int(cfg.get("hpa_min", 2)),
            "maxReplicas": int(cfg.get("hpa_max", 5)),
            "targetCPU": int(cfg.get("hpa_cpu", 80)),
        },
        "pdb": {"enabled": replicas > 1, "minAvailable": 1},
    }
    # multi-service (compose) extras — only emitted when set, so a single-service render is
    # byte-identical to before (the chart's own defaults apply when these are absent).
    if cfg.get("command"):
        values["command"] = list(cfg["command"])
    if cfg.get("args"):
        values["args"] = list(cfg["args"])
    if cfg.get("extra_ports"):
        values["extraPorts"] = [int(p) for p in cfg["extra_ports"]]
    if cfg.get("run_as_user") is not None:
        values["runAsUser"] = int(cfg["run_as_user"])
    if cfg.get("volumes"):
        values["volumes"] = list(cfg["volumes"])
        values["writableRoot"] = True          # a stateful service needs to write its data dir
        values["dropCapabilities"] = False     # a stateful image's entrypoint needs CHOWN/SETUID
        if cfg.get("run_as_user") is not None:
            values["fsGroup"] = int(cfg["run_as_user"])   # PVC writable by that uid
        else:
            values["runAsNonRoot"] = False     # let the stateful image drop privileges itself
    if cfg.get("probe"):
        values["probe"] = dict(cfg["probe"])
    if cfg.get("stack"):
        values["stack"] = cfg["stack"]
        # Compose deploys arbitrary third-party images that set their own USER (often a NAME
        # like `USER node`/`appuser`). runAsNonRoot:true makes k8s refuse a non-numeric user
        # ("cannot verify user is non-root"). Respect the image's USER unless the compose file
        # gave a numeric one. (The user's OWN single-service app keeps runAsNonRoot enforced.)
        if cfg.get("run_as_user") is None:
            values["runAsNonRoot"] = False
    if cfg.get("resources"):                    # compose partial -> overlay on chart defaults
        r = {"requests": dict(DEFAULT_RESOURCES["requests"]), "limits": dict(DEFAULT_RESOURCES["limits"])}
        r["requests"].update((cfg["resources"].get("requests") or {}))
        r["limits"].update((cfg["resources"].get("limits") or {}))
        values["resources"] = r
    return values

def render(cfg: dict) -> str:
    values = build_values(cfg)
    ns = cfg.get("namespace", "default")
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(values, f)
        vfile = f.name
    try:
        out = subprocess.run(
            ["helm", "template", values["name"], CHART_DIR, "-f", vfile, "--namespace", ns],
            capture_output=True, text=True, check=True,
        )
        return out.stdout
    finally:
        os.unlink(vfile)
