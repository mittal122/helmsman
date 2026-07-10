import subprocess
from tools import portforward

class _FakeProc:
    def __init__(self): self._alive = True
    def poll(self): return None if self._alive else 0
    def terminate(self): self._alive = False
    def wait(self, timeout=None): return 0
    def kill(self): self._alive = False

def test_start_returns_local_port_and_registers(monkeypatch):
    monkeypatch.setattr(portforward, "_free_port", lambda: 51999)
    captured = {}
    def _popen(cmd, **k): captured["cmd"] = cmd; return _FakeProc()
    monkeypatch.setattr(subprocess, "Popen", _popen)
    lport = portforward.start("app", "ns", 8080)
    assert lport == 51999
    assert "port-forward" in captured["cmd"] and "svc/app" in captured["cmd"] and "51999:8080" in captured["cmd"]
    assert "app" in portforward._procs
    portforward.stop("app")
    assert "app" not in portforward._procs

def test_start_restarts_existing(monkeypatch):
    monkeypatch.setattr(portforward, "_free_port", lambda: 52000)
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **k: _FakeProc())
    portforward.start("app", "ns", 8080)
    first = portforward._procs["app"]
    portforward.start("app", "ns", 8080)          # restart -> old terminated, new proc
    assert portforward._procs["app"] is not first
    portforward.stop_all()
    assert portforward._procs == {}
