import json

import pytest
from fastapi.testclient import TestClient

import intake
import main


def test_build_prompt_is_deterministic_and_self_contained():
    p = intake.build_prompt()
    assert '"services"' in p and "Return ONE strict JSON" in p
    assert "connects_to" in p and "smoke_tests" in p and "CONNECTION WIRING" in p
    # same input -> same prompt (no LLM, no randomness)
    assert p == intake.build_prompt()


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


def test_ingest_maps_ingress_and_scaling_to_chart_keys():
    r = intake.ingest(json.dumps({"services": [
        {"name": "web", "image": "nginx:1.27", "port": 80,
         "ingress": {"host": "shop.example.com"}, "scaling": {"min": 2, "max": 6, "cpu": 70}}]}))
    w = r["cfg"]["services"][0]
    assert w["ingress_host"] == "shop.example.com"
    assert w["hpa_enabled"] and w["hpa_min"] == 2 and w["hpa_max"] == 6 and w["hpa_cpu"] == 70
    # renders real Ingress + HPA from those keys (no chart change)
    from tools import manifests
    y = manifests.render({**w, "namespace": "default", "stack": "shop"})
    assert "kind: Ingress" in y and "HorizontalPodAutoscaler" in y
    assert 'host: "shop.example.com"' in y and "averageUtilization: 70" in y


def test_ingest_worker_and_cronjob_workloads():
    r = intake.ingest(json.dumps({"services": [
        {"name": "mailer", "image": "org/mailer:2", "type": "worker"},
        {"name": "nightly", "image": "org/backup:2", "type": "cronjob", "schedule": "0 3 * * *"}]}))
    assert r["missing"] == []                                   # worker needs no port
    mailer = next(s for s in r["cfg"]["services"] if s["name"] == "mailer")
    nightly = next(s for s in r["cfg"]["services"] if s["name"] == "nightly")
    assert mailer["workload"] == "worker" and mailer["published"] is False and mailer["hpa_enabled"] is False
    assert nightly["workload"] == "cronjob" and nightly["schedule"] == "0 3 * * *"
    intake.validate_services(r["cfg"]["services"])
    # renders: worker -> Deployment no Service; cronjob -> CronJob
    from tools import manifests
    yw = manifests.render({**mailer, "namespace": "default", "stack": "s"})
    yc = manifests.render({**nightly, "namespace": "default", "stack": "s"})
    assert "kind: Deployment" in yw and "kind: Service" not in yw
    assert "kind: CronJob" in yc and 'schedule: "0 3 * * *"' in yc


def test_ingest_build_from_source():
    r = intake.ingest(json.dumps({"services": [
        {"name": "api", "port": 8000,
         "build": {"git_repo": "https://github.com/org/api.git", "subdir": "api"}}]}))
    assert r["missing"] == []
    api = r["cfg"]["services"][0]
    assert api["image"] == "" and api["build"]["git_repo"] == "https://github.com/org/api.git"
    assert api["build"]["subdir"] == "api"
    assert "build ← https://github.com/org/api.git" in r["summary"]
    intake.validate_services(r["cfg"]["services"])


def test_ingest_build_without_repo_is_missing_and_rejected():
    r = intake.ingest(json.dumps({"services": [{"name": "api", "port": 80, "build": {"subdir": "x"}}]}))
    assert ("api", "build.git_repo") in {(m["service"], m["field"]) for m in r["missing"]}
    with pytest.raises(ValueError):
        intake.validate_services([{"name": "api", "image": "", "port": 80, "build": {"subdir": "x"}}])


def test_c1_rewires_localhost_to_service_name():
    # backend points DB_HOST at localhost; there's a postgres sibling -> rewired to 'db'
    r = intake.ingest(json.dumps({"services": [
        {"name": "backend", "image": "org/api:1", "port": 8000,
         "env": {"DB_HOST": "localhost", "APP_ENV": "prod"},
         "secrets": {"DATABASE_URL": "postgres://u:p@localhost:5432/app"}},
        {"name": "db", "image": "postgres:16", "port": 5432, "published": False,
         "secrets": {"POSTGRES_PASSWORD": "pw"}}]}))
    be = next(s for s in r["cfg"]["services"] if s["name"] == "backend")
    assert be["env"]["DB_HOST"] == "db"                              # bare host rewired
    assert "@db:5432" in be["secrets"]["DATABASE_URL"]              # URL host rewired
    assert be["env"]["APP_ENV"] == "prod"                           # untouched
    assert any("rewired to service 'db'" in w for w in r["warnings"])

def test_c1_leaves_self_bind_alone_but_warns_when_ambiguous():
    # a bare HOST with no dependency hint and 2 candidates -> not rewritten, just warned
    r = intake.ingest(json.dumps({"services": [
        {"name": "web", "image": "org/web:1", "port": 80, "env": {"HOST": "localhost"}},
        {"name": "api", "image": "org/api:1", "port": 8000},
        {"name": "worker", "image": "org/w:1", "type": "worker"}]}))
    web = next(s for s in r["cfg"]["services"] if s["name"] == "web")
    assert web["env"]["HOST"] == "localhost"                         # not rewritten (self-bind risk)
    assert any("points at localhost" in w for w in r["warnings"])

def test_c3_c4_database_probe_and_volume_autofixed():
    # a DB with an http health check and no volume -> tcp probe + a data volume auto-attached
    r = intake.ingest(json.dumps({"services": [
        {"name": "db", "image": "postgres:16", "port": 5432, "published": False,
         "secrets": {"POSTGRES_PASSWORD": "pw"},
         "health": {"type": "http", "path": "/"}}]}))
    db = r["cfg"]["services"][0]
    assert db["probe"] == {"type": "tcp"}                            # C3: http -> tcp
    assert db["volumes"] and db["volumes"][0]["mountPath"] == "/var/lib/postgresql/data"  # C4
    assert r["missing"] == []


def test_c2_app_inherits_db_password():
    # DB password known; backend's empty DB password gets set to match, mismatch warns
    r = intake.ingest(json.dumps({"services": [
        {"name": "db", "image": "postgres:16", "port": 5432, "published": False,
         "secrets": {"POSTGRES_PASSWORD": "s3cret"},
         "volumes": [{"name": "d", "mountPath": "/var/lib/postgresql/data", "size": "1Gi"}]},
        {"name": "backend", "image": "org/api:1", "port": 8000,
         "secrets": {"DB_PASSWORD": None}}]}))
    be = next(s for s in r["cfg"]["services"] if s["name"] == "backend")
    assert be["secrets"]["DB_PASSWORD"] == "s3cret"                  # inherited
    assert any("match the database's password" in w for w in r["warnings"])
    # a mismatching app password is warned, not overwritten
    r2 = intake.ingest(json.dumps({"services": [
        {"name": "db", "image": "postgres:16", "port": 5432, "published": False,
         "secrets": {"POSTGRES_PASSWORD": "s3cret"},
         "volumes": [{"name": "d", "mountPath": "/var/lib/postgresql/data", "size": "1Gi"}]},
        {"name": "backend", "image": "org/api:1", "port": 8000, "secrets": {"DB_PASSWORD": "different"}}]}))
    be2 = next(s for s in r2["cfg"]["services"] if s["name"] == "backend")
    assert be2["secrets"]["DB_PASSWORD"] == "different"             # not overwritten
    assert any("differs from the database's password" in w for w in r2["warnings"])


def test_browser_backend_wiring_and_ingress_routes():
    # browser→backend: route /api to the backend on the frontend's ingress + inject a relative base
    r = intake.ingest(json.dumps({"application": {"name": "shop"}, "services": [
        {"name": "web", "image": "org/web:1", "port": 80, "published": True,
         "ingress": {"host": "shop.example.com"},
         "connects_to": [{"service": "api", "from": "browser", "path_prefix": "/api", "port": 8000,
                          "browser_base": {"env": "VITE_API_URL", "baked_at_build": False}}]},
        {"name": "api", "image": "org/api:1", "port": 8000}]}))
    web = next(s for s in r["cfg"]["services"] if s["name"] == "web")
    assert web["ingress_routes"] == [{"path": "/api", "service": "api", "port": 8000}]
    assert web["env"]["VITE_API_URL"] == "/api" and r["healing_prompt"] == ""
    # the chart renders /api -> api and / -> web on the same ingress
    from tools import manifests
    y = manifests.render({**web, "namespace": "default", "stack": "shop"})
    assert "path: /api" in y and "name: api" in y and "kind: Ingress" in y

def test_browser_baked_base_triggers_healing_prompt():
    r = intake.ingest(json.dumps({"services": [
        {"name": "web", "image": "org/web:1", "port": 80, "published": True,
         "connects_to": [{"service": "api", "from": "browser", "path_prefix": "/api",
                          "browser_base": {"env": "REACT_APP_API", "baked_at_build": True}}]},
        {"name": "api", "image": "org/api:1", "port": 8000}]}))
    assert "baked into the build" in r["healing_prompt"] and "/api" in r["healing_prompt"]

def test_server_side_connection_uses_service_name_not_healed():
    # a server-side (backend->db) connection is NOT a browser problem -> no healing, no route
    r = intake.ingest(json.dumps({"services": [
        {"name": "api", "image": "org/api:1", "port": 8000,
         "connects_to": [{"service": "db", "hostname_in_code": "db", "port": 5432, "from": "server"}]},
        {"name": "db", "image": "postgres:16", "port": 5432, "secrets": {"POSTGRES_PASSWORD": "x"}}]}))
    api = next(s for s in r["cfg"]["services"] if s["name"] == "api")
    assert "ingress_routes" not in api and r["healing_prompt"] == ""


def test_change2_validation_rich_schema():
    r = intake.ingest(json.dumps({"application": {"name": "app"}, "services": [
        {"name": "web", "image": "org/web:latest", "port": 80, "published": True,
         "connects_to": [{"service": "api", "hostname_in_code": "backend", "port": 8000}],
         "secrets": {"API_KEY": {"value": None, "required_to_boot": True}},
         "env": {"LOG_LEVEL": {"value": "info", "required_to_boot": False}}},
        {"name": "api", "image": "org/api:1", "port": 8000, "max_safe_replicas": 1,
         "replicas": 3, "replica_constraint_reason": "in-memory websocket engine"}]}))
    errs = " | ".join(r["errors"])
    assert "must be version-pinned" in errs                       # (i) :latest rejected
    assert "not a service in this manifest" in errs               # (d) hostname 'backend' != a service
    assert "max_safe_replicas=1" in errs and "websocket" in errs  # (h) replica safety
    # (a) empty required secret -> a consolidated question, not a silent deploy
    assert ("web", "secrets.API_KEY") in {(q["service"], q["field"]) for q in r["missing"]}
    # rich env flattened for the chart; required-to-boot captured
    web = next(s for s in r["cfg"]["services"] if s["name"] == "web")
    assert web["env"]["LOG_LEVEL"] == "info" and web["required_secrets"] == ["API_KEY"]

def test_change2_deploy_gate_rejects_latest_tag():
    # the strict /deploy gate re-runs the hard errors -> 422 on a hand-crafted POST
    with pytest.raises(ValueError):
        intake.validate_services([{"name": "web", "image": "org/web:latest", "port": 80,
                                   "workload": "deployment"}])

def test_change2_hostname_contract_matches_ok():
    # when the hostname_in_code equals a real service name, no error
    r = intake.ingest(json.dumps({"services": [
        {"name": "web", "image": "org/web:1", "port": 80, "published": True,
         "connects_to": [{"service": "api", "hostname_in_code": "api", "port": 8000}]},
        {"name": "api", "image": "org/api:1", "port": 8000}]}))
    assert r["errors"] == []


def test_ingest_flags_database_missing_password():
    # a postgres service with no password -> asked for up front (Missing), so it can't crash-loop
    r = intake.ingest(json.dumps({"services": [
        {"name": "db", "image": "postgres:16", "port": 5432, "published": False}]}))
    fields = {(m["service"], m["field"]) for m in r["missing"]}
    assert ("db", "secrets.POSTGRES_PASSWORD") in fields, r["missing"]
    # with a password provided, no Missing
    r2 = intake.ingest(json.dumps({"services": [
        {"name": "db", "image": "postgres:16", "port": 5432, "published": False,
         "secrets": {"POSTGRES_PASSWORD": "s3cret"}}]}))
    assert r2["missing"] == []


def test_ingest_service_account_and_rbac():
    r = intake.ingest(json.dumps({"services": [
        {"name": "ctl", "image": "ctl:1", "port": 8080,
         "service_account": {"create": True,
                             "annotations": {"iam.gke.io/gcp-service-account": "x@y.iam"},
                             "rules": [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]}]}}]}))
    sa = r["cfg"]["services"][0]["service_account"]
    assert sa["create"] and sa["rules"] and "iam.gke.io/gcp-service-account" in sa["annotations"]
    assert "RBAC+SA" in r["summary"]
    from tools import manifests
    y = manifests.render({**r["cfg"]["services"][0], "namespace": "default", "stack": "s"})
    assert "kind: ServiceAccount" in y and "kind: Role" in y and "kind: RoleBinding" in y
    assert "serviceAccountName: ctl" in y
    # never a ClusterRole (blast radius stays in-namespace)
    assert "kind: ClusterRole" not in y


def test_ingest_build_spec_rejects_flag_and_traversal_injection():
    r = intake.ingest(json.dumps({"services": [{"name": "api", "port": 80, "build": {
        "git_repo": "-oProxyCommand=touch /tmp/pwn",   # not a URL -> dropped -> Missing
        "git_branch": "--upload-pack=evil",
        "git_ref": "-x",
        "dockerfile": "../../etc/passwd",
        "subdir": "../../.."}}]}))
    b = r["cfg"]["services"][0]["build"]
    assert b["git_repo"] == ""                                    # non-URL repo dropped
    assert b["git_branch"] == "" and b["git_ref"] == ""           # flag-smuggling refs dropped
    assert b["dockerfile"] == "" and b["subdir"] == ""            # traversal dropped
    assert ("api", "build.git_repo") in {(m["service"], m["field"]) for m in r["missing"]}
    with pytest.raises(ValueError):                              # strict gate still rejects
        intake.validate_services(r["cfg"]["services"])


def test_ingest_cronjob_without_schedule_is_missing():
    r = intake.ingest(json.dumps({"services": [{"name": "j", "image": "j:1", "type": "cronjob"}]}))
    assert ("j", "schedule") in {(m["service"], m["field"]) for m in r["missing"]}
    with pytest.raises(ValueError):
        intake.validate_services([{"name": "j", "image": "j:1", "workload": "cronjob"}])


def test_ingest_no_scaling_block_leaves_hpa_off():
    r = intake.ingest(json.dumps({"services": [{"name": "db", "image": "postgres:16", "port": 5432}]}))
    assert r["cfg"]["services"][0]["hpa_enabled"] is False
    assert r["cfg"]["services"][0]["ingress_host"] == ""


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
