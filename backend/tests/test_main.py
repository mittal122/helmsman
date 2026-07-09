from fastapi.testclient import TestClient
import main

def test_deploy_accepts_config_and_returns_id(monkeypatch):
    async def fake_run(cfg, bus):
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
