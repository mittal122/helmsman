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
    r = subprocess.run(
        ["kubectl", "get", "pods", "-l", f"app.kubernetes.io/name={name}",
         "-n", namespace, "-o", "json"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return []
    items = json.loads(r.stdout).get("items", [])
    return _failures_from_pods(items)
