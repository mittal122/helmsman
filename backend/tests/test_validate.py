import subprocess
from tools import validate

class _R:
    def __init__(self, rc, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err

def test_valid_manifests_pass(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R(0))
    ok, issues = validate.validate("kind: Deployment", "default")
    assert ok is True
    assert issues == []

def test_schema_failure_reported(monkeypatch):
    calls = {"n": 0}
    def fake(*a, **k):
        calls["n"] += 1
        return _R(1, err="boom") if calls["n"] == 1 else _R(0)
    monkeypatch.setattr(subprocess, "run", fake)
    ok, issues = validate.validate("bad", "default")
    assert ok is False
    assert any("schema" in i for i in issues)
