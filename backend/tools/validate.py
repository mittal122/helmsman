import subprocess

def validate(manifests: str, namespace: str) -> tuple[bool, list[str]]:
    issues: list[str] = []

    kc = subprocess.run(
        ["kubeconform", "-strict", "-summary", "-"],
        input=manifests, capture_output=True, text=True,
    )
    if kc.returncode != 0:
        issues.append("schema: " + (kc.stdout + kc.stderr).strip())

    # ponytail: don't pin -n; target namespace may not exist yet (created at deploy via helm --create-namespace)
    dr = subprocess.run(
        ["kubectl", "apply", "--dry-run=server", "-f", "-"],
        input=manifests, capture_output=True, text=True,
    )
    if dr.returncode != 0:
        issues.append("dry-run: " + dr.stderr.strip())

    ks = subprocess.run(
        ["kube-score", "score", "--output-format", "ci", "-"],
        input=manifests, capture_output=True, text=True,
    )
    criticals = [ln for ln in ks.stdout.splitlines() if "[CRITICAL]" in ln]
    if criticals:
        issues.append("kube-score: " + "; ".join(criticals))

    return (len(issues) == 0, issues)
