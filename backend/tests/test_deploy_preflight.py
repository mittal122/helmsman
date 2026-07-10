import json
import subprocess
from tools import deploy

def test_reachable_parses_server_version(monkeypatch):
    class _R:
        returncode = 0
        stdout = json.dumps({"serverVersion": {"gitVersion": "v1.30.0"}})
        stderr = ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
    ok, detail = deploy.cluster_reachable()
    assert ok is True and detail == "v1.30.0"

def test_unreachable_on_nonzero(monkeypatch):
    class _R:
        returncode = 1
        stdout = ""
        stderr = "The connection to the server localhost:8080 was refused"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
    ok, detail = deploy.cluster_reachable()
    assert ok is False and "refused" in detail

def test_unreachable_on_timeout(monkeypatch):
    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="kubectl", timeout=8)
    monkeypatch.setattr(subprocess, "run", _boom)
    ok, detail = deploy.cluster_reachable()
    assert ok is False and "timed out" in detail

def test_unreachable_when_kubectl_missing(monkeypatch):
    def _boom(*a, **k):
        raise FileNotFoundError("kubectl")
    monkeypatch.setattr(subprocess, "run", _boom)
    ok, detail = deploy.cluster_reachable()
    assert ok is False and "not installed" in detail
