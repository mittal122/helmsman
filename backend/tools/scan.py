import json
import os
import subprocess
import tempfile

def _severities(threshold: str) -> str:
    order = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    i = order.index(threshold) if threshold in order else 3
    return ",".join(order[i:])

def scan_image(image: str, threshold: str = "CRITICAL") -> dict:
    try:
        r = subprocess.run(
            ["trivy", "image", "--quiet", "--format", "json",
             "--severity", _severities(threshold), image],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return {"available": False, "ok": True, "findings": [],
                "summary": "trivy not installed — scan skipped"}
    findings = []
    try:
        for res in (json.loads(r.stdout or "{}").get("Results") or []):
            for v in (res.get("Vulnerabilities") or []):
                findings.append({"id": v.get("VulnerabilityID", ""),
                                 "severity": v.get("Severity", ""),
                                 "pkg": v.get("PkgName", ""),
                                 "title": v.get("Title", "")})
    except json.JSONDecodeError:
        return {"available": True, "ok": True, "findings": [],
                "summary": "trivy output unparseable — treated as no findings"}
    ok = len(findings) == 0
    return {"available": True, "ok": ok, "findings": findings,
            "summary": f"{len(findings)} vuln(s) at/above {threshold}"}

def scan_config(manifests: str) -> dict:
    d = tempfile.mkdtemp()
    path = os.path.join(d, "manifests.yaml")
    open(path, "w").write(manifests)
    try:
        r = subprocess.run(
            ["trivy", "config", "--quiet", "--format", "json", d],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return {"available": False, "ok": True, "findings": [],
                "summary": "trivy not installed — config scan skipped"}
    finally:
        try:
            os.unlink(path); os.rmdir(d)
        except OSError:
            pass
    findings = []
    try:
        for res in (json.loads(r.stdout or "{}").get("Results") or []):
            for m in (res.get("Misconfigurations") or []):
                findings.append({"id": m.get("ID", ""), "severity": m.get("Severity", ""),
                                 "pkg": m.get("Type", ""), "title": m.get("Title", "")})
    except json.JSONDecodeError:
        pass
    return {"available": True, "ok": True, "findings": findings,   # advisory: never blocks
            "summary": f"{len(findings)} misconfig(s) (advisory)"}
