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
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **k: (captured.__setitem__("cmd", cmd), _FakeProc())[1])
    lport = portforward.start("ns/api", "ns", "svc/api-svc", 80)
    assert lport == 51999
    assert "port-forward" in captured["cmd"] and "svc/api-svc" in captured["cmd"] and "51999:80" in captured["cmd"]
    assert portforward.is_running("ns/api") is True
    portforward.stop("ns/api")
    assert "ns/api" not in portforward._procs and portforward.is_running("ns/api") is False

def test_start_restarts_existing(monkeypatch):
    monkeypatch.setattr(portforward, "_free_port", lambda: 52000)
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **k: _FakeProc())
    portforward.start("k", "ns", "deploy/app", 8080)
    first = portforward._procs["k"]
    portforward.start("k", "ns", "deploy/app", 8080)      # restart -> old terminated
    assert portforward._procs["k"] is not first
    portforward.stop_all()
    assert portforward._procs == {}
