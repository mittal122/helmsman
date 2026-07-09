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

def test_kube_score_critical_blocks(monkeypatch):
    seq = [_R(0), _R(0), _R(0, out="[CRITICAL] Deployment/x: something bad")]
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: seq.pop(0))
    ok, issues = validate.validate("kind: Deployment", "default")
    assert ok is False
    assert any("kube-score" in i for i in issues)

def test_kube_score_warnings_pass(monkeypatch):
    seq = [_R(0), _R(0), _R(0, out="[WARNING] minor")]
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: seq.pop(0))
    ok, issues = validate.validate("kind: Deployment", "default")
    assert ok is True

def test_kube_score_exec_error_reported(monkeypatch):
    seq = [_R(0), _R(0), _R(2, err="boom")]
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: seq.pop(0))
    ok, issues = validate.validate("kind: Deployment", "default")
    assert ok is False
    assert any("exec error" in i for i in issues)
