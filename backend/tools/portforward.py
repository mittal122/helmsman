"""Auto port-forward so the deployed app has a clickable local URL.

A ClusterIP service isn't reachable from the browser; the standard local access
is `kubectl port-forward`. When a deploy goes live we start one on a free local
port and hand the UI `http://127.0.0.1:<port>` — the user just clicks.

ponytail: process-owned by uvicorn, keyed by app name; single-deploy-by-design.
A new deploy (or stop) tears down the prior forward. Not multi-tenant safe.
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

def start(name: str, namespace: str, svc_port: int) -> int:
    """Start (or restart) a port-forward to svc/<name>; return the local port."""
    stop(name)
    lport = _free_port()
    p = subprocess.Popen(
        ["kubectl", "port-forward", "-n", namespace, f"svc/{name}",
         f"{lport}:{svc_port}", "--address", "127.0.0.1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    _procs[name] = p
    return lport

def stop(name: str) -> None:
    p = _procs.pop(name, None)
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=3)
        except Exception:
            p.kill()

def stop_all() -> None:
    for n in list(_procs):
        stop(n)
