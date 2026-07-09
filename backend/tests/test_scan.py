import json
import subprocess
from tools import scan

def test_scan_image_flags_findings(monkeypatch):
    payload = {"Results": [{"Vulnerabilities": [
        {"VulnerabilityID": "CVE-1", "Severity": "CRITICAL", "PkgName": "openssl", "Title": "bad"}]}]}
    class _R:
        returncode = 0
        stdout = json.dumps(payload)
        stderr = ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
    r = scan.scan_image("img:1")
    assert r["available"] is True and r["ok"] is False
    assert r["findings"][0]["id"] == "CVE-1"

def test_scan_image_clean(monkeypatch):
    class _R:
        returncode = 0; stdout = json.dumps({"Results": []}); stderr = ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
    r = scan.scan_image("img:1")
    assert r["available"] is True and r["ok"] is True and r["findings"] == []

def test_scan_image_graceful_when_absent(monkeypatch):
    def _boom(*a, **k): raise FileNotFoundError("trivy")
    monkeypatch.setattr(subprocess, "run", _boom)
    r = scan.scan_image("img:1")
    assert r["available"] is False and r["ok"] is True   # skip, never silently fail-open as "clean"
    assert "skipped" in r["summary"]
