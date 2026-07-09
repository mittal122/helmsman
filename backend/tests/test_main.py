from fastapi.testclient import TestClient
import main

def test_deploy_accepts_config_and_returns_id(monkeypatch):
    async def fake_run(cfg, bus, approvals):
        return None
    monkeypatch.setattr(main, "coordinator_run", fake_run)
    client = TestClient(main.app)
    r = client.post("/deploy", json={"name": "app", "image": "i:1",
                                     "namespace": "default", "port": 8080, "replicas": 2})
    assert r.status_code == 200
    assert "deployment_id" in r.json()

def test_root_serves_ui():
    client = TestClient(main.app)
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]

def test_rejects_non_rfc1123_name():
    client = TestClient(main.app)
    r = client.post("/deploy", json={"name": "--evil", "image": "i:1"})
    assert r.status_code == 422

def test_approve_resolves(monkeypatch):
    called = {}
    monkeypatch.setattr(main.approvals, "resolve",
                        lambda k, a: called.update(k=k, a=a) or True)
    client = TestClient(main.app)
    r = client.post("/approve", json={"name": "demo", "approved": True})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert called == {"k": "demo", "a": True}
