import shutil
import pytest
from tools import manifests

def test_build_values_maps_config():
    cfg = {"name": "orders", "image": "orders:1.0", "port": 3000, "replicas": 4}
    v = manifests.build_values(cfg)
    assert v["name"] == "orders"
    assert v["image"] == "orders:1.0"
    assert v["port"] == 3000
    assert v["replicas"] == 4

def test_build_values_defaults():
    v = manifests.build_values({"name": "x", "image": "x:1"})
    assert v["replicas"] == 2
    assert v["port"] == 8080

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_render_produces_deployment_and_service():
    out = manifests.render({"name": "demo", "image": "nginx:1.27", "port": 8080})
    assert "kind: Deployment" in out
    assert "kind: Service" in out
    assert "demo" in out

def test_build_values_env_secrets_pdb():
    v = manifests.build_values({
        "name": "x", "image": "x:1", "replicas": 3,
        "env": {"A": "1"}, "secrets": {"S": "y"},
    })
    assert v["env"] == {"A": "1"}
    assert v["secrets"] == {"S": "y"}
    assert v["pdb"]["enabled"] is True          # replicas > 1
    assert v["pdb"]["minAvailable"] == 1

def test_build_values_ingress_hpa_flags():
    v = manifests.build_values({
        "name": "x", "image": "x:1", "replicas": 1,
        "ingress_host": "demo.local",
        "hpa_enabled": True, "hpa_min": 2, "hpa_max": 6, "hpa_cpu": 70,
    })
    assert v["ingress"] == {"enabled": True, "host": "demo.local"}
    assert v["hpa"]["enabled"] is True and v["hpa"]["maxReplicas"] == 6
    assert v["pdb"]["enabled"] is False         # single replica

def test_build_values_compose_extras_absent_by_default():
    # single-service cfg must NOT emit the compose extras (keeps renders byte-identical)
    v = manifests.build_values({"name": "x", "image": "x:1"})
    for k in ("command", "args", "extraPorts", "runAsUser", "volumes", "probe", "resources", "stack"):
        assert k not in v

def test_build_values_compose_extras_passthrough():
    v = manifests.build_values({
        "name": "db", "image": "postgres:16", "port": 5432,
        "command": ["postgres"], "args": ["-c", "x"], "extra_ports": [9187],
        "run_as_user": 999, "volumes": [{"name": "d", "mountPath": "/data", "size": "2Gi"}],
        "probe": {"type": "tcp"}, "stack": "shop",
        "resources": {"limits": {"cpu": "500m", "memory": "512Mi"}},
    })
    assert v["command"] == ["postgres"] and v["args"] == ["-c", "x"]
    assert v["extraPorts"] == [9187] and v["runAsUser"] == 999
    assert v["writableRoot"] is True             # volumes present -> writable root
    assert v["probe"] == {"type": "tcp"} and v["stack"] == "shop"
    assert v["resources"]["limits"] == {"cpu": "500m", "memory": "512Mi"}
    assert v["resources"]["requests"] == {"cpu": "50m", "memory": "64Mi"}   # defaulted

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_render_tcp_probe_and_pvc_for_stateful_service():
    out = manifests.render({
        "name": "db", "image": "postgres:16", "port": 5432,
        "probe": {"type": "tcp"}, "run_as_user": 999,
        "volumes": [{"name": "pgdata", "mountPath": "/var/lib/postgresql/data", "size": "1Gi"}],
    })
    assert "tcpSocket:" in out and "httpGet:" not in out    # DB not killed by an HTTP probe
    assert "kind: PersistentVolumeClaim" in out and "db-pgdata" in out
    assert "readOnlyRootFilesystem: false" in out           # writable root for the data dir
    assert "runAsUser: 999" in out

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_render_exec_probe_from_healthcheck():
    out = manifests.render({"name": "db", "image": "postgres:16", "port": 5432,
                            "probe": {"type": "exec", "command": ["pg_isready", "-U", "postgres"]}})
    assert "exec:" in out and "pg_isready" in out
