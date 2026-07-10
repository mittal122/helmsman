"""Cluster read/action layer — the SRE console engine.

Deterministic kubectl/helm wrappers to browse and manage ANY workload in ANY
namespace (not just Helmsman-deployed ones). Reads the live cluster (the source of
truth), so it's accurate across UI reloads. All names are RFC1123-validated before
they reach argv (no flag/command injection); every call has a wall-clock timeout so
an unreachable cluster fails fast instead of hanging.
"""
import json
import re
import subprocess

TIMEOUT_S = 20
_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")   # RFC1123 (ns/workload)

def _valid(*names: str) -> None:
    for n in names:
        if not _NAME.match(n or ""):
            raise ValueError(f"invalid kubernetes name: {n!r}")

def _kubectl(*args: str, timeout: int = TIMEOUT_S):
    r = subprocess.run(["kubectl", *args, "--request-timeout=12s"],
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr

def _json(*args: str):
    rc, out, err = _kubectl(*args, "-o", "json")
    if rc != 0:
        raise RuntimeError((err or out).strip().splitlines()[0] if (err or out).strip() else "kubectl error")
    return json.loads(out or "{}")

def _sel(labels: dict) -> str:
    return ",".join(f"{k}={v}" for k, v in (labels or {}).items())

def _subset(small: dict, big: dict) -> bool:
    return bool(small) and all(big.get(k) == v for k, v in small.items())

# ---------- reads ----------
def list_namespaces() -> list[dict]:
    obj = _json("get", "ns")
    out = []
    for i in obj.get("items", []):
        m, s = i.get("metadata", {}), i.get("status", {})
        out.append({"name": m.get("name"), "status": s.get("phase", "Active"),
                    "created": m.get("creationTimestamp", "")})
    return sorted(out, key=lambda x: x["name"])

def list_workloads(namespace: str) -> list[dict]:
    _valid(namespace)
    obj = _json("get", "deploy", "-n", namespace)
    out = []
    for d in obj.get("items", []):
        m, spec, st = d.get("metadata", {}), d.get("spec", {}), d.get("status", {})
        conts = spec.get("template", {}).get("spec", {}).get("containers", [])
        out.append({
            "name": m.get("name"),
            "namespace": namespace,
            "desired": int(spec.get("replicas", 0)),
            "ready": int(st.get("readyReplicas", 0)),
            "available": int(st.get("availableReplicas", 0)),
            "image": conts[0].get("image", "") if conts else "",
            "managed": (m.get("labels", {}) or {}).get("helmsman.dev/managed-by") == "helmsman",
            "created": m.get("creationTimestamp", ""),
        })
    return sorted(out, key=lambda x: x["name"])

def get_summary(namespace: str, name: str) -> dict:
    """Full topology of a deployment: its pods, the services routing to it, HPA, PDB,
    and referenced ConfigMaps/Secrets — with the relationships made explicit."""
    _valid(namespace, name)
    dep = _json("get", "deploy", name, "-n", namespace)
    spec = dep.get("spec", {})
    sel = spec.get("selector", {}).get("matchLabels", {}) or {}
    tmpl_labels = spec.get("template", {}).get("metadata", {}).get("labels", {}) or {}
    st = dep.get("status", {})
    conts = spec.get("template", {}).get("spec", {}).get("containers", [])

    # pods via the deployment's selector
    pods = []
    try:
        pobj = _json("get", "pods", "-n", namespace, "-l", _sel(sel)) if sel else {"items": []}
    except RuntimeError:
        pobj = {"items": []}
    for p in pobj.get("items", []):
        pm, pst = p.get("metadata", {}), p.get("status", {})
        css = pst.get("containerStatuses") or []
        restarts = sum(int(c.get("restartCount", 0)) for c in css)
        ready = sum(1 for c in css if c.get("ready"))
        reason = ""
        for c in css:
            w = (c.get("state") or {}).get("waiting") or {}
            if w.get("reason"):
                reason = w["reason"]; break
        pods.append({"name": pm.get("name"), "phase": pst.get("phase", ""),
                     "ready": f"{ready}/{len(css)}" if css else "0/0",
                     "restarts": restarts, "node": p.get("spec", {}).get("nodeName", ""),
                     "reason": reason, "podIP": pst.get("podIP", "")})

    # services whose selector routes to these pods
    services = []
    try:
        for s in _json("get", "svc", "-n", namespace).get("items", []):
            ssel = s.get("spec", {}).get("selector") or {}
            if _subset(ssel, tmpl_labels) or _subset(ssel, sel):
                sp = s.get("spec", {})
                services.append({"name": s.get("metadata", {}).get("name"),
                                 "type": sp.get("type", "ClusterIP"),
                                 "clusterIP": sp.get("clusterIP", ""),
                                 "ports": [f"{p.get('port')}→{p.get('targetPort')}/{p.get('protocol','TCP')}"
                                           for p in sp.get("ports", [])],
                                 "selector": ssel})
    except RuntimeError:
        pass

    # HPA targeting this deployment
    hpas = []
    try:
        for h in _json("get", "hpa", "-n", namespace).get("items", []):
            ref = h.get("spec", {}).get("scaleTargetRef", {})
            if ref.get("kind") == "Deployment" and ref.get("name") == name:
                hs, hst = h.get("spec", {}), h.get("status", {})
                hpas.append({"name": h.get("metadata", {}).get("name"),
                             "min": hs.get("minReplicas"), "max": hs.get("maxReplicas"),
                             "current": hst.get("currentReplicas"),
                             "targetCPU": hs.get("targetCPUUtilizationPercentage"),
                             "currentCPU": hst.get("currentCPUUtilizationPercentage")})
    except RuntimeError:
        pass

    # PDBs whose selector matches
    pdbs = []
    try:
        for pd in _json("get", "pdb", "-n", namespace).get("items", []):
            psel = pd.get("spec", {}).get("selector", {}).get("matchLabels", {}) or {}
            if _subset(psel, tmpl_labels):
                pds = pd.get("spec", {})
                pdbs.append({"name": pd.get("metadata", {}).get("name"),
                             "minAvailable": pds.get("minAvailable"),
                             "maxUnavailable": pds.get("maxUnavailable")})
    except RuntimeError:
        pass

    # ConfigMaps/Secrets referenced by the pod spec
    refs = {"configMaps": set(), "secrets": set()}
    for c in conts:
        for ef in c.get("envFrom", []) or []:
            if ef.get("configMapRef"): refs["configMaps"].add(ef["configMapRef"].get("name"))
            if ef.get("secretRef"): refs["secrets"].add(ef["secretRef"].get("name"))
        for e in c.get("env", []) or []:
            vf = (e.get("valueFrom") or {})
            if vf.get("configMapKeyRef"): refs["configMaps"].add(vf["configMapKeyRef"].get("name"))
            if vf.get("secretKeyRef"): refs["secrets"].add(vf["secretKeyRef"].get("name"))
    for v in spec.get("template", {}).get("spec", {}).get("volumes", []) or []:
        if v.get("configMap"): refs["configMaps"].add(v["configMap"].get("name"))
        if v.get("secret"): refs["secrets"].add(v["secret"].get("secretName"))

    return {
        "name": name, "namespace": namespace,
        "desired": int(spec.get("replicas", 0)),
        "ready": int(st.get("readyReplicas", 0)),
        "available": int(st.get("availableReplicas", 0)),
        "updated": int(st.get("updatedReplicas", 0)),
        "image": conts[0].get("image", "") if conts else "",
        "images": [c.get("image", "") for c in conts],
        "selector": sel,
        "strategy": spec.get("strategy", {}).get("type", ""),
        "managed": (dep.get("metadata", {}).get("labels", {}) or {}).get("helmsman.dev/managed-by") == "helmsman",
        "pods": pods,
        "services": services,
        "hpa": hpas,
        "pdb": pdbs,
        "configMaps": sorted(x for x in refs["configMaps"] if x),
        "secrets": sorted(x for x in refs["secrets"] if x),
    }

def get_logs(namespace: str, name: str, tail: int = 200) -> str:
    _valid(namespace, name)
    dep = _json("get", "deploy", name, "-n", namespace)
    sel = dep.get("spec", {}).get("selector", {}).get("matchLabels", {}) or {}
    if not sel:
        return ""
    rc, out, err = _kubectl("logs", "-n", namespace, "-l", _sel(sel),
                            "--tail", str(int(tail)), "--all-containers", "--prefix",
                            "--max-log-requests", "10")
    return out if rc == 0 else (err or "").strip()

# ---------- actions (mutating — token-gated at the API) ----------
def scale(namespace: str, name: str, replicas: int) -> dict:
    _valid(namespace, name)
    if not isinstance(replicas, int) or replicas < 0 or replicas > 100:
        raise ValueError("replicas must be 0..100")
    rc, out, err = _kubectl("scale", "deploy", name, "-n", namespace, f"--replicas={replicas}")
    if rc != 0:
        raise RuntimeError((err or out).strip())
    return {"ok": True, "replicas": replicas}

def stop(namespace: str, name: str) -> dict:
    return scale(namespace, name, 0)   # stop = scale to zero (reversible, keeps config)

def restart(namespace: str, name: str) -> dict:
    _valid(namespace, name)
    rc, out, err = _kubectl("rollout", "restart", "deploy", name, "-n", namespace)
    if rc != 0:
        raise RuntimeError((err or out).strip())
    return {"ok": True}

def set_autoscale(namespace: str, name: str, min_r: int, max_r: int, cpu: int) -> dict:
    _valid(namespace, name)
    if not (1 <= min_r <= max_r <= 100) or not (1 <= cpu <= 100):
        raise ValueError("need 1<=min<=max<=100 and 1<=cpu<=100")
    # replace any existing HPA so min/max/cpu are updated idempotently
    _kubectl("delete", "hpa", name, "-n", namespace, "--ignore-not-found")
    rc, out, err = _kubectl("autoscale", "deploy", name, "-n", namespace,
                            f"--min={min_r}", f"--max={max_r}", f"--cpu-percent={cpu}")
    if rc != 0:
        raise RuntimeError((err or out).strip())
    return {"ok": True, "min": min_r, "max": max_r, "targetCPU": cpu}

def disable_autoscale(namespace: str, name: str) -> dict:
    _valid(namespace, name)
    _kubectl("delete", "hpa", name, "-n", namespace, "--ignore-not-found")
    return {"ok": True}

def _helm_release_exists(namespace: str, name: str) -> bool:
    r = subprocess.run(["helm", "status", name, "-n", namespace],
                       capture_output=True, text=True, timeout=TIMEOUT_S)
    return r.returncode == 0

def delete_app(namespace: str, name: str) -> dict:
    """Delete a workload. Prefers `helm uninstall` for a clean, complete removal of a
    Helm-managed release; otherwise deletes the Deployment + the Services routing to
    it + its HPA/PDB (bounded — never a blanket label-delete that could hit siblings)."""
    _valid(namespace, name)
    if _helm_release_exists(namespace, name):
        r = subprocess.run(["helm", "uninstall", name, "-n", namespace, "--wait", "--timeout", "120s"],
                           capture_output=True, text=True, timeout=150)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip())
        return {"ok": True, "method": "helm uninstall"}
    # non-helm: gather then delete only what belongs to this deployment
    summary = get_summary(namespace, name)
    deleted = []
    _kubectl("delete", "deploy", name, "-n", namespace, "--ignore-not-found"); deleted.append(f"deploy/{name}")
    for s in summary["services"]:
        _kubectl("delete", "svc", s["name"], "-n", namespace, "--ignore-not-found"); deleted.append(f"svc/{s['name']}")
    for h in summary["hpa"]:
        _kubectl("delete", "hpa", h["name"], "-n", namespace, "--ignore-not-found"); deleted.append(f"hpa/{h['name']}")
    for pd in summary["pdb"]:
        _kubectl("delete", "pdb", pd["name"], "-n", namespace, "--ignore-not-found"); deleted.append(f"pdb/{pd['name']}")
    return {"ok": True, "method": "kubectl delete", "deleted": deleted}
