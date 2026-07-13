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

def test_get_replicas_desired_is_spec_not_status(monkeypatch):
    # desired must come from spec.replicas (the true target), not status.replicas
    # (current pod count that dips during churn -> premature "live")
    class _R:
        returncode = 0
        stdout = json.dumps({"spec": {"replicas": 3},
                             "status": {"readyReplicas": 1, "replicas": 1}})
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
    assert deploy.get_replicas("app", "ns") == (1, 3)  # 1 ready of 3 desired, not 1/1

def test_probe_url_none_when_unreachable():
    from tools import deploy
    # nothing listening on this port -> connection refused -> None (not an exception)
    assert deploy.probe_url("http://127.0.0.1:1/", timeout=1, attempts=1) is None

def test_run_smoke_tests_pass_and_fail():
    from tools import deploy
    # unreachable endpoint -> fail (got None != expected 200)
    r = deploy.run_smoke_tests([{"via": "web", "path": "/api", "expect_status": 200}],
                               {"web": "http://127.0.0.1:1"})
    assert r[0]["ok"] is False and r[0]["got"] is None
    # no endpoint for the named service -> fail with an error, not a crash
    r2 = deploy.run_smoke_tests([{"via": "ghost", "path": "/"}], {})
    assert r2[0]["ok"] is False and "no reachable endpoint" in r2[0]["error"]

def test_smoke_error_report_never_leaks_secret_values(monkeypatch):
    from tools import deploy, monitor
    monkeypatch.setattr(monitor, "get_logs", lambda *a, **k: "line1\nline2\nFATAL: boom")
    monkeypatch.setattr(monitor, "get_events", lambda *a, **k: ["BackOff: restarting"])
    rep = deploy.smoke_error_report("Verify", "api", "default",
                                    {"path": "/api", "expect_status": 200},
                                    {"DB_PASSWORD": "supersecret", "JWT": "abcd"})
    assert rep["secret_shape"] == [{"key": "DB_PASSWORD", "length": 11}, {"key": "JWT", "length": 4}]
    assert "supersecret" not in str(rep)              # values never appear
    assert rep["last_log_lines"][-1] == "FATAL: boom"  # LAST lines (the exception)
    assert rep["pod_events"] == ["BackOff: restarting"]

def test_image_inspection_prefill():
    from tools import inspect as im
    svc = {"name": "web", "workload": "deployment", "port": 8080, "run_as_user": None, "volumes": []}
    filled = im.prefill_service(svc, {"ports": [3000, 9090], "user": "1001",
                                      "user_numeric": 1001, "user_is_named": False,
                                      "volumes": ["/data"]}, [])
    assert svc["port"] == 3000 and svc["run_as_user"] == 1001 and svc["volumes"][0]["mountPath"] == "/data"
    assert set(filled) == {"port", "run_as_user", "volumes"}
    # a named (non-numeric) user is flagged, not silently used
    w = []
    im.prefill_service({"name": "a", "run_as_user": None}, {"user": "node", "user_is_named": True}, w)
    assert any("named user 'node'" in x for x in w)

def test_intake_inspect_endpoint(monkeypatch):
    from fastapi.testclient import TestClient
    import main
    monkeypatch.setattr(main.image_inspect, "inspect_image",
                        lambda img, **k: {"ports": [3000], "user_numeric": 1001, "user_is_named": False, "volumes": []})
    with TestClient(main.app) as c:
        r = c.post("/intake/inspect", json={"services": [
            {"name": "web", "image": "org/web:1", "workload": "deployment", "port": 8080, "run_as_user": None}]})
        assert r.status_code == 200
        s = r.json()["services"][0]
        assert s["port"] == 3000 and s["run_as_user"] == 1001
