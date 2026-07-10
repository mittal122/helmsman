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

def test_reap_stops_only_stale_forwards(monkeypatch):
    import time
    monkeypatch.setattr(portforward, "_free_port", lambda: 53000)
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **k: _FakeProc())
    portforward.start("stale", "ns", "svc/a", 80)
    portforward.start("fresh", "ns", "svc/b", 80)
    portforward._seen["stale"] = time.monotonic() - 100     # not heartbeated in 100s
    portforward.touch("fresh")                              # recently heartbeated
    dead = portforward.reap(30)
    assert dead == ["stale"]
    assert not portforward.is_running("stale") and portforward.is_running("fresh")
    portforward.stop_all()

def test_touch_ignores_unknown_key():
    portforward.touch("does-not-exist")                    # no-op, no error
    assert "does-not-exist" not in portforward._seen
