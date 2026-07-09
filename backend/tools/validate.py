import subprocess

def validate(manifests: str, namespace: str) -> tuple[bool, list[str]]:
    issues: list[str] = []

    kc = subprocess.run(
        ["kubeconform", "-strict", "-summary", "-"],
        input=manifests, capture_output=True, text=True,
    )
    if kc.returncode != 0:
        issues.append("schema: " + (kc.stdout + kc.stderr).strip())

    dr = subprocess.run(
        ["kubectl", "apply", "--dry-run=server", "-n", namespace, "-f", "-"],
        input=manifests, capture_output=True, text=True,
    )
    if dr.returncode != 0:
        issues.append("dry-run: " + dr.stderr.strip())

    return (len(issues) == 0, issues)
