"""On-demand port-forward so any workload has a clickable local URL.

A ClusterIP service isn't reachable from the browser; the standard local access is
`kubectl port-forward`. We start one on a free local port and hand the UI
`http://127.0.0.1:<port>` — the user just clicks.

Lifecycle: forwards are backend processes, so a closed browser can't stop them
directly. Each forward carries a last-seen timestamp; the UI heartbeats the forwards
it still has open, and a reaper (see main.py) stops any forward not heartbeated within
a TTL — so closing the window (even a crash/kill) auto-cleans the forward. A pagehide
beacon is the fast path for a normal close.

ponytail: processes owned by uvicorn, keyed by an arbitrary string (app name for the
deploy flow, "ns/name" for the manage console). Single-user local design.
"""
import socket
import subprocess
import time

_procs: dict[str, subprocess.Popen] = {}
_seen: dict[str, float] = {}   # key -> monotonic timestamp of last heartbeat/start

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
    _seen[key] = time.monotonic()
    return lport

def touch(key: str) -> None:
    """Heartbeat: mark a forward as still-in-use by an open UI."""
    if key in _procs:
        _seen[key] = time.monotonic()

def is_running(key: str) -> bool:
    p = _procs.get(key)
    return bool(p and p.poll() is None)

def active() -> list[str]:
    return list(_procs)

def stop(key: str) -> None:
    _seen.pop(key, None)
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

def reap(ttl: float = 30.0) -> list[str]:
    """Stop every forward not heartbeated within `ttl` seconds. Returns the keys reaped."""
    now = time.monotonic()
    # reap both TTL-expired forwards AND ones whose kubectl child already exited on its own
    # (pod deleted / connection dropped) — else a self-exited Popen lingers while the UI
    # keeps heart-beating its key.
    dead = [k for k, t in list(_seen.items())
            if now - t > ttl or (k in _procs and _procs[k].poll() is not None)]
    for k in dead:
        stop(k)
    return dead
