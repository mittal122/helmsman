import json
import subprocess
from tools import monitor

def _pod(name, phase="Running", waiting=None, last_term=None):
    cs = {"name": "app", "state": {}, "lastState": {}}
    if waiting:
        cs["state"] = {"waiting": {"reason": waiting, "message": "boom"}}
    if last_term:
        cs["lastState"] = {"terminated": {"reason": last_term}}
    pod = {"metadata": {"name": name}, "status": {"phase": phase, "containerStatuses": [cs]}}
    return pod

def test_detects_crashloop_and_imagepull():
    items = [_pod("a", waiting="CrashLoopBackOff"), _pod("b", waiting="ImagePullBackOff")]
    out = monitor._failures_from_pods(items)
    types = {f["type"] for f in out}
    assert types == {"CrashLoopBackOff", "ImagePullBackOff"}
    assert all(f["pod"] in ("a", "b") for f in out)

def test_detects_oomkilled():
    out = monitor._failures_from_pods([_pod("a", last_term="OOMKilled")])
    assert out[0]["type"] == "OOMKilled"

def test_detects_pending_without_container_statuses():
    pod = {"metadata": {"name": "p"}, "status": {"phase": "Pending"}}
    out = monitor._failures_from_pods([pod])
    assert out[0]["type"] == "Pending"

def test_healthy_pod_no_failures():
    assert monitor._failures_from_pods([_pod("a")]) == []

def test_detect_failures_returns_empty_on_kubectl_error(monkeypatch):
    class _R: returncode = 1; stdout = ""; stderr = "nope"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
    assert monitor.detect_failures("x", "default") == []

class _Run:
    def __init__(self, rc, out=""): self.returncode, self.stdout, self.stderr = rc, out, ""

def test_get_metrics_parses_top(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
        lambda *a, **k: _Run(0, "demo-abc 5m 40Mi\ndemo-def 3m 38Mi\n"))
    m = monitor.get_metrics("demo", "default")
    assert m == [{"pod": "demo-abc", "cpu": "5m", "memory": "40Mi"},
                 {"pod": "demo-def", "cpu": "3m", "memory": "38Mi"}]

def test_get_metrics_empty_when_unavailable(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Run(1))
    assert monitor.get_metrics("demo", "default") == []

def test_get_logs_returns_stdout(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Run(0, "line1\nline2"))
    assert monitor.get_logs("demo", "default") == "line1\nline2"
