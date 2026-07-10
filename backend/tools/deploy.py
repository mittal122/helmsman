import json
import os
import subprocess
import tempfile
import yaml
from tools.manifests import build_values, CHART_DIR

# Fast-fail: cluster calls must never hang the pipeline. A wall-clock cap turns an
# unreachable cluster into a visible error instead of a silent stall.
KUBECTL_TIMEOUT_S = 15
HELM_TIMEOUT_S = 180

def cluster_reachable(timeout: int = 8) -> tuple[bool, str]:
    """Quick preflight: is the target cluster's API reachable? (ok, detail)."""
    try:
        r = subprocess.run(
            ["kubectl", "version", "-o", "json", "--request-timeout=5s"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return (False, "kubectl timed out — API server unreachable")
    except FileNotFoundError:
        return (False, "kubectl not installed")
    if r.returncode != 0:
        return (False, ((r.stderr or r.stdout).strip().splitlines() or ["unreachable"])[0][:200])
    try:
        v = json.loads(r.stdout or "{}").get("serverVersion", {})
        return (True, v.get("gitVersion", "reachable"))
    except (json.JSONDecodeError, AttributeError):
        return (True, "reachable")

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
            capture_output=True, text=True, check=True, timeout=HELM_TIMEOUT_S,
        )
    finally:
        os.unlink(vfile)

def get_replicas(name: str, namespace: str) -> tuple[int, int]:
    try:
        out = subprocess.run(
            ["kubectl", "get", "deploy", name, "-n", namespace, "-o", "json",
             "--request-timeout=8s"],
            capture_output=True, text=True, timeout=KUBECTL_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return (0, 0)
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

def detect_capabilities() -> dict:
    try:
        ic = subprocess.run(["kubectl", "get", "ingressclass", "-o", "name",
                             "--request-timeout=8s"],
                            capture_output=True, text=True, timeout=KUBECTL_TIMEOUT_S)
        ms = subprocess.run(["kubectl", "get", "apiservices", "v1beta1.metrics.k8s.io",
                             "--request-timeout=8s"],
                            capture_output=True, text=True, timeout=KUBECTL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"ingress_controller": False, "metrics_server": False}
    return {
        "ingress_controller": ic.returncode == 0 and bool(ic.stdout.strip()),
        "metrics_server": ms.returncode == 0,
    }
