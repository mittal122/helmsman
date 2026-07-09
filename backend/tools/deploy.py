import json
import os
import subprocess
import tempfile
import yaml
from tools.manifests import build_values, CHART_DIR

def install(cfg: dict) -> None:
    values = build_values(cfg)
    ns = cfg.get("namespace", "default")
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(values, f)
        vfile = f.name
    try:
        subprocess.run(
            ["helm", "upgrade", "--install", values["name"], CHART_DIR,
             "-f", vfile, "--namespace", ns, "--create-namespace"],
            capture_output=True, text=True, check=True,
        )
    finally:
        os.unlink(vfile)

def get_replicas(name: str, namespace: str) -> tuple[int, int]:
    out = subprocess.run(
        ["kubectl", "get", "deploy", name, "-n", namespace, "-o", "json"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        return (0, 0)
    status = json.loads(out.stdout).get("status", {})
    return (int(status.get("readyReplicas", 0)), int(status.get("replicas", 0)))

def get_endpoint(name: str, namespace: str, port: int) -> dict:
    return {
        "service": f"{name}.{namespace}.svc.cluster.local",
        "port": port,
        "port_forward": f"kubectl port-forward -n {namespace} svc/{name} {port}:{port}",
    }
