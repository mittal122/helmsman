import json
import subprocess

_FAIL_WAITING = {
    "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull",
    "CreateContainerConfigError", "CreateContainerError", "InvalidImageName",
}

def _failures_from_pods(items: list) -> list[dict]:
    out: list[dict] = []
    for pod in items:
        name = pod.get("metadata", {}).get("name", "?")
        st = pod.get("status", {})
        css = st.get("containerStatuses") or []
        for cs in css:
            waiting = (cs.get("state") or {}).get("waiting") or {}
            if waiting.get("reason") in _FAIL_WAITING:
                out.append({"pod": name, "container": cs.get("name"),
                            "type": waiting["reason"], "message": waiting.get("message", "")})
            term = (cs.get("lastState") or {}).get("terminated") or {}
            if term.get("reason") == "OOMKilled":
                out.append({"pod": name, "container": cs.get("name"),
                            "type": "OOMKilled", "message": "container was OOM-killed"})
        if st.get("phase") == "Pending" and not css:
            out.append({"pod": name, "container": None,
                        "type": "Pending", "message": "pod is pending (unscheduled or waiting)"})
    return out

def detect_failures(name: str, namespace: str) -> list[dict]:
    try:
        r = subprocess.run(
            ["kubectl", "get", "pods", "-l", f"app.kubernetes.io/name={name}",
             "-n", namespace, "-o", "json", "--request-timeout=8s"],
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        return []
    if r.returncode != 0:
        return []
    items = json.loads(r.stdout).get("items", [])
    return _failures_from_pods(items)

def get_metrics(name: str, namespace: str) -> list[dict]:
    try:
        r = subprocess.run(
            ["kubectl", "top", "pods", "-l", f"app.kubernetes.io/name={name}",
             "-n", namespace, "--no-headers", "--request-timeout=8s"],
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        return []
    if r.returncode != 0:
        return []
    rows: list[dict] = []
    for ln in r.stdout.splitlines():
        parts = ln.split()
        if len(parts) >= 3:
            rows.append({"pod": parts[0], "cpu": parts[1], "memory": parts[2]})
    return rows

def get_logs(name: str, namespace: str, tail: int = 20, previous: bool = False) -> str:
    args = ["kubectl", "logs", "-l", f"app.kubernetes.io/name={name}",
            "-n", namespace, "--tail", str(tail), "--all-containers", "--prefix",
            "--request-timeout=10s"]
    if previous:
        args.append("--previous")   # the CRASHED instance's logs (why it died)
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return ""
    return r.stdout if r.returncode == 0 else ""

def crash_logs(name: str, namespace: str, tail: int = 60) -> str:
    """The most useful logs for a crash-looping pod. A CrashLoopBackOff container is constantly
    restarting, so its CURRENT logs are often empty/partial — the PREVIOUS (crashed) instance's
    logs hold the real error (e.g. 'database is uninitialized and superuser password is not
    specified'). Try previous first, fall back to current."""
    prev = get_logs(name, namespace, tail, previous=True)
    if prev.strip():
        return prev
    return get_logs(name, namespace, tail)
