"""On-demand port-forward so any workload has a clickable local URL.

A ClusterIP service isn't reachable from the browser; the standard local access is
`kubectl port-forward`. We start one on a free local port and hand the UI
`http://127.0.0.1:<port>` — the user just clicks.

ponytail: processes owned by uvicorn, keyed by an arbitrary string (app name for the
deploy flow, "ns/name" for the manage console). Single-user local design.
"""
import socket
import subprocess

_procs: dict[str, subprocess.Popen] = {}

def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port

def start(key: str, namespace: str, target: str, port: int) -> int:
    """Start (or restart) a port-forward. target is 'svc/<name>' or 'deploy/<name>'.
    Returns the local port bound on 127.0.0.1."""
    stop(key)
    lport = _free_port()
    p = subprocess.Popen(
        ["kubectl", "port-forward", "-n", namespace, target,
         f"{lport}:{port}", "--address", "127.0.0.1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    _procs[key] = p
    return lport

def is_running(key: str) -> bool:
    p = _procs.get(key)
    return bool(p and p.poll() is None)

def stop(key: str) -> None:
    p = _procs.pop(key, None)
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=3)
        except Exception:
            p.kill()

def stop_all() -> None:
    for k in list(_procs):
        stop(k)
