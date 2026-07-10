import json
import subprocess
import pytest
from tools import cluster

def _run(rc=0, stdout="", stderr=""):
    class _R: pass
    r = _R(); r.returncode = rc; r.stdout = stdout; r.stderr = stderr
    return r

def test_invalid_names_rejected():
    for bad in ("../evil", "-x", "UP", "a b", "a/b"):
        with pytest.raises(ValueError):
            cluster.list_workloads(bad)

def test_list_workloads_parses(monkeypatch):
    payload = {"items": [{
        "metadata": {"name": "api", "labels": {"helmsman.dev/managed-by": "helmsman"},
                     "creationTimestamp": "t"},
        "spec": {"replicas": 3, "template": {"spec": {"containers": [{"image": "api:1.2"}]}}},
        "status": {"readyReplicas": 2, "availableReplicas": 2}}]}
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _run(stdout=json.dumps(payload)))
    w = cluster.list_workloads("prod")[0]
    assert w == {"name": "api", "namespace": "prod", "desired": 3, "ready": 2,
                 "available": 2, "image": "api:1.2", "managed": True, "created": "t"}

def test_get_summary_maps_relationships(monkeypatch):
    dep = {"metadata": {"name": "api", "labels": {}},
           "spec": {"replicas": 2,
                    "selector": {"matchLabels": {"app.kubernetes.io/name": "api"}},
                    "template": {"metadata": {"labels": {"app.kubernetes.io/name": "api"}},
                                 "spec": {"containers": [{"image": "api:1", "envFrom": [
                                     {"secretRef": {"name": "api-secret"}}]}]}},
                    "strategy": {"type": "RollingUpdate"}},
           "status": {"readyReplicas": 2, "availableReplicas": 2}}
    pods = {"items": [{"metadata": {"name": "api-abc"}, "spec": {"nodeName": "n1"},
                       "status": {"phase": "Running", "podIP": "10.0.0.1",
                                  "containerStatuses": [{"ready": True, "restartCount": 1}]}}]}
    svcs = {"items": [{"metadata": {"name": "api"}, "spec": {"type": "ClusterIP", "clusterIP": "10.1",
             "selector": {"app.kubernetes.io/name": "api"}, "ports": [{"port": 80, "targetPort": 8080}]}},
            {"metadata": {"name": "other"}, "spec": {"selector": {"app.kubernetes.io/name": "zzz"}, "ports": []}}]}
    hpa = {"items": [{"metadata": {"name": "api"}, "spec": {"scaleTargetRef": {"kind": "Deployment", "name": "api"},
             "minReplicas": 2, "maxReplicas": 5, "targetCPUUtilizationPercentage": 80},
             "status": {"currentReplicas": 2, "currentCPUUtilizationPercentage": 12}}]}
    def fake(cmd, **k):
        a = " ".join(cmd)
        if "get deploy api" in a: return _run(stdout=json.dumps(dep))
        if "get pods" in a: return _run(stdout=json.dumps(pods))
        if "get svc" in a: return _run(stdout=json.dumps(svcs))
        if "get hpa" in a: return _run(stdout=json.dumps(hpa))
        if "get pdb" in a: return _run(stdout=json.dumps({"items": []}))
        return _run(stdout="{}")
    monkeypatch.setattr(subprocess, "run", fake)
    s = cluster.get_summary("prod", "api")
    assert s["ready"] == 2 and s["desired"] == 2
    assert [p["name"] for p in s["pods"]] == ["api-abc"] and s["pods"][0]["restarts"] == 1
    assert [x["name"] for x in s["services"]] == ["api"]   # only the matching svc
    assert s["hpa"][0]["max"] == 5 and s["secrets"] == ["api-secret"]

def test_scale_validates_and_runs(monkeypatch):
    cap = {}
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: (cap.__setitem__("cmd", cmd), _run())[1])
    assert cluster.scale("prod", "api", 5) == {"ok": True, "replicas": 5}
    assert "--replicas=5" in cap["cmd"] and "api" in cap["cmd"]
    with pytest.raises(ValueError):
        cluster.scale("prod", "api", 999)

def test_stop_is_scale_zero(monkeypatch):
    cap = {}
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: (cap.__setitem__("cmd", cmd), _run())[1])
    assert cluster.stop("prod", "api")["replicas"] == 0
    assert "--replicas=0" in cap["cmd"]

def test_autoscale_validates(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: _run())
    assert cluster.set_autoscale("prod", "api", 2, 6, 70)["max"] == 6
    with pytest.raises(ValueError):
        cluster.set_autoscale("prod", "api", 6, 2, 70)   # min>max

def test_delete_uses_helm_when_release_exists(monkeypatch):
    calls = []
    def fake(cmd, **k):
        calls.append(cmd)
        if cmd[:2] == ["helm", "status"]: return _run(rc=0)
        if cmd[:2] == ["helm", "uninstall"]: return _run(rc=0, stdout="uninstalled")
        return _run()
    monkeypatch.setattr(subprocess, "run", fake)
    r = cluster.delete_app("prod", "api")
    assert r["method"] == "helm uninstall"
    assert any(c[:2] == ["helm", "uninstall"] for c in calls)
