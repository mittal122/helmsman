import json

import pytest
from fastapi.testclient import TestClient

import intake
import main


def test_build_prompt_is_deterministic_and_self_contained():
    p = intake.build_prompt({"app_description": "a Rails app"})
    assert "Rails" in p
    assert '"services"' in p and "Return ONLY the JSON" in p
    # same input -> same prompt (no LLM, no randomness)
    assert p == intake.build_prompt({"app_description": "a Rails app"})


def test_ingest_complete_stack_has_no_missing_and_normalizes():
    r = intake.ingest(json.dumps({
        "application": {"name": "shop", "namespace": "prod"},
        "services": [
            {"name": "web", "image": "org/web:1.2.3", "port": 3000, "replicas": 2,
             "env": {"APP_ENV": "prod"}, "secrets": {"DB_PASSWORD": "s"},
             "health": {"type": "http", "path": "/healthz"}},
            {"name": "db", "image": "postgres:16", "port": 5432, "published": False,
             "env": {"POSTGRES_PASSWORD": "pw"},
             "volumes": [{"name": "pgdata", "mountPath": "/var/lib/postgresql/data", "size": "2Gi"}]},
        ],
    }), {"mode": "autonomous"})
    assert r["missing"] == []
    assert r["cfg"]["name"] == "shop" and r["cfg"]["namespace"] == "prod"
    assert r["cfg"]["mode"] == "autonomous"
    web = next(s for s in r["cfg"]["services"] if s["name"] == "web")
    db = next(s for s in r["cfg"]["services"] if s["name"] == "db")
    assert web["probe"] == {"type": "http", "path": "/healthz"}
    assert db["probe"] == {"type": "tcp"}                       # no health + port -> tcp, never http
    assert db["secrets"] == {"POSTGRES_PASSWORD": "pw"} and db["env"] == {}
    assert db["volumes"][0]["size"] == "2Gi"
    intake.validate_services(r["cfg"]["services"])              # complete -> passes strict gate


def test_ingest_reports_missing_without_assuming():
    r = intake.ingest(json.dumps({"services": [{"name": "web", "port": "nope"}]}))
    fields = {(m["service"], m["field"]) for m in r["missing"]}
    assert ("web", "image") in fields and ("web", "port") in fields


def test_ingest_auto_classifies_credential_env_to_secret():
    r = intake.ingest(json.dumps({"services": [
        {"name": "a", "image": "a:1", "port": 80, "env": {"API_TOKEN": "t", "COLOR": "blue"}}]}))
    a = r["cfg"]["services"][0]
    assert a["secrets"] == {"API_TOKEN": "t"} and a["env"] == {"COLOR": "blue"}


def test_ingest_coerces_bad_names_and_flat_single_service():
    r = intake.ingest(json.dumps({"name": "My App", "image": "i:1", "port": 80}))  # flat, no wrapper
    assert r["cfg"]["services"][0]["name"] == "my-app"
    assert r["missing"] == []


@pytest.mark.parametrize("bad", ["not json", "[]", "{}", '{"application": {}}'])
def test_ingest_rejects_malformed(bad):
    with pytest.raises(ValueError):
        intake.ingest(bad)


def test_validate_services_rejects_incomplete():
    with pytest.raises(ValueError):
        intake.validate_services([{"name": "x", "image": "", "port": 80}])
    with pytest.raises(ValueError):
        intake.validate_services([{"name": "x", "image": "i:1", "port": 0}])


def test_intake_endpoints_and_deploy_passthrough(monkeypatch):
    # don't actually run a deploy — stub the coordinator
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(main, "coordinator_run", _noop)
    with TestClient(main.app) as c:
        # prompt endpoint
        p = c.post("/intake/prompt", json={"app_description": "x"})
        assert p.status_code == 200 and "services" in p.json()["prompt"]
        # ingest a complete stack
        blob = json.dumps({"services": [{"name": "web", "image": "org/web:1.0", "port": 8080}]})
        g = c.post("/intake/ingest", json={"response": blob, "name": "app", "mode": "manual"})
        assert g.status_code == 200 and g.json()["missing"] == []
        cfg = g.json()["cfg"]
        # deploy via the services passthrough
        d = c.post("/deploy", json={"name": cfg["name"], "namespace": cfg["namespace"],
                                    "mode": cfg["mode"], "services": cfg["services"]})
        assert d.status_code == 200, d.text
        # malformed JSON -> 422
        assert c.post("/intake/ingest", json={"response": "nope"}).status_code == 422
        # deploy with an incomplete service -> 422
        assert c.post("/deploy", json={"name": "app", "services": [{"name": "web", "image": "", "port": 80}]}
                      ).status_code == 422
