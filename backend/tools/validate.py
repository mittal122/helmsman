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

    # kube-score gates on genuinely-bad config, but we ignore checks that are
    # deliberate decisions for this platform, not defects:
    #   pod-networkpolicy .......... default-deny NetworkPolicy is a later hardening phase
    #   ...ephemeral-storage ....... ephemeral-storage limits are out of scope here
    #   ...image-pull-policy ....... we pin image tags; Always is not required
    #   pod-probes-identical ....... identical liveness/readiness probe is acceptable
    #   ...security-context-user-group-id .. runAsNonRoot is enforced; explicit high UID is extra polish
    #   deployment-has-poddisruptionbudget . PDB is rendered only for replicas>1 by design
    _KS_IGNORE = [
        "pod-networkpolicy",
        "container-ephemeral-storage-request-and-limit",
        "container-image-pull-policy",
        "pod-probes-identical",
        "container-security-context-user-group-id",
        "deployment-has-poddisruptionbudget",
    ]
    ks_cmd = ["kube-score", "score", "--output-format", "ci"]
    for t in _KS_IGNORE:
        ks_cmd += ["--ignore-test", t]
    ks_cmd.append("-")
    ks = subprocess.run(ks_cmd, input=manifests, capture_output=True, text=True)
    criticals = [ln for ln in ks.stdout.splitlines() if "[CRITICAL]" in ln]
    if criticals:
        issues.append("kube-score: " + "; ".join(criticals))
    if ks.returncode not in (0, 1) and not criticals:
        issues.append("kube-score: exec error: " + ks.stderr.strip())

    return (len(issues) == 0, issues)
