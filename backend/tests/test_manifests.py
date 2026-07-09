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
