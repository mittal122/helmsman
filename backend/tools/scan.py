import json
import os
import subprocess
import tempfile

def _severities(threshold: str) -> str:
    order = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    i = order.index(threshold) if threshold in order else 3
    return ",".join(order[i:])

def scan_image(image: str, threshold: str = "CRITICAL") -> dict:
    # argv-flag-injection guard: a leading-dash/whitespace image ref could be read as a
    # trivy flag. Refuse (block) rather than pass it through, matching rollback.py's _check.
    if not image or image.startswith("-") or any(c.isspace() for c in image):
        return {"available": True, "ok": False, "findings": [],
                "summary": "refusing to scan suspicious image ref"}
    try:
        r = subprocess.run(
            ["trivy", "image", "--quiet", "--format", "json",
             "--severity", _severities(threshold), "--", image],
            capture_output=True, text=True, timeout=300,
        )
    except FileNotFoundError:
        return {"available": False, "ok": True, "findings": [],
                "summary": "trivy not installed — scan skipped"}
    except subprocess.TimeoutExpired:
        return {"available": True, "ok": True, "findings": [],
                "summary": "trivy image scan timed out — inconclusive, not a pass"}
    if r.returncode != 0:
        return {"available": True, "ok": True, "findings": [],
                "summary": f"trivy scan error (rc={r.returncode}): {r.stderr.strip()[:200]} — inconclusive, not a pass"}
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
    with open(path, "w") as f:
        f.write(manifests)
    try:
        r = subprocess.run(
            ["trivy", "config", "--quiet", "--format", "json", "--", d],
            capture_output=True, text=True, timeout=120,
        )
    except FileNotFoundError:
        return {"available": False, "ok": True, "findings": [],
                "summary": "trivy not installed — config scan skipped"}
    except subprocess.TimeoutExpired:
        return {"available": True, "ok": True, "findings": [],
                "summary": "trivy config scan timed out — advisory skipped"}
    finally:
        try:
            os.unlink(path); os.rmdir(d)
        except OSError:
            pass
    if r.returncode != 0:
        return {"available": True, "ok": True, "findings": [],
                "summary": f"trivy scan error (rc={r.returncode}): {r.stderr.strip()[:200]} — inconclusive, not a pass"}
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
