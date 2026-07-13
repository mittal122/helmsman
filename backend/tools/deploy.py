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
    obj = json.loads(out.stdout)
    ready = int((obj.get("status") or {}).get("readyReplicas", 0))
    # desired = spec.replicas (the true target). status.replicas is the current pod
    # count, which dips during churn — using it lets a crash-looping rollout declare
    # "live" prematurely (e.g. 1/1 when 3 were requested).
    desired = int((obj.get("spec") or {}).get("replicas", 0))
    return (ready, desired)

def get_endpoint(name: str, namespace: str, port: int) -> dict:
    return {
        "service": f"{name}.{namespace}.svc.cluster.local",
        "port": port,
        "port_forward": f"kubectl port-forward -n {namespace} svc/{name} {port}:{port}",
    }

def probe_url(url: str, timeout: int = 4, attempts: int = 6, delay: float = 1.0):
    """V4 — actually hit the endpoint to prove the app RESPONDS (not just 'started'). Returns the
    HTTP status (any status, incl. 4xx/5xx, means it's answering) or None if unreachable.
    Retries a few times: a just-started port-forward tunnel takes a second or two to accept
    connections, so a single immediate probe races it and false-negatives."""
    import time
    import urllib.request
    import urllib.error
    for i in range(attempts):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code        # a 401/404/500 still proves the server is up and answering
        except Exception:
            if i < attempts - 1:
                time.sleep(delay)
    return None                  # still not answering after retries

def run_smoke_tests(tests: list, endpoints: dict) -> list:
    """CHANGE 4 — execute the app-author's smoke tests through the browser-facing entrypoints.
    `endpoints` = {service_name: base_url}. Returns a result per test. 'pods Running' is not
    success; these are. Retries each check (the app may still be warming up)."""
    results = []
    for t in tests:
        via = str(t.get("via") or "").strip()
        base = endpoints.get(via) or (next(iter(endpoints.values())) if len(endpoints) == 1 else "")
        path = str(t.get("path") or "/")
        expect = int(t.get("expect_status") or 200)
        if not base:
            results.append({"test": t, "ok": False, "got": None, "expect": expect,
                            "error": f"no reachable endpoint for '{via or '(published service)'}'"})
            continue
        url = base.rstrip("/") + "/" + path.lstrip("/")
        got = probe_url(url, attempts=8, delay=1.5)
        results.append({"test": t, "ok": (got == expect), "got": got, "expect": expect, "url": url})
    return results

def smoke_error_report(stage: str, service: str, namespace: str, failed, secrets: dict) -> dict:
    """Structured, AI-pasteable failure report. Secrets appear as {key,length} only — never
    values. Log lines are the LAST lines (the actual exception), not the first."""
    from tools import monitor
    try:
        logs = monitor.get_logs(service, namespace, tail=60)
    except Exception:
        logs = ""
    try:
        events = monitor.get_events(service, namespace)
    except Exception:
        events = []
    return {
        "stage": stage,
        "service": service,
        "pod_events": events[-8:],
        "last_log_lines": logs.strip().splitlines()[-20:],
        "failed_smoke_test": failed,
        "secret_shape": [{"key": k, "length": len(str(v))} for k, v in (secrets or {}).items()],
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
