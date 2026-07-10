"""Deterministic failure diagnostics — turn a raw checker/error string into plain
"what's wrong + why + how to fix it" guidance.

This is the self-healing "guide" rung: when the agent can't auto-fix a break (a
user-input or policy problem, e.g. an unpinned image tag), it must still tell the
user exactly what to change. Deterministic + keyed on known patterns, so it works
with no API key and never executes untrusted text (injection-safe). The LLM
error-resolver may enrich this, but this catalog is the guaranteed floor.
"""

def _checker(issue: str) -> str:
    low = issue.lower()
    if low.startswith("kube-score") or "kube-score" in low:
        return "kube-score"
    if low.startswith("schema") or "kubeconform" in low:
        return "kubeconform"
    if low.startswith("dry-run") or "dry run" in low:
        return "kubectl dry-run"
    if "trivy" in low or "cve-" in low or "vuln" in low:
        return "trivy"
    return "validator"

# ordered (predicate, builder) rules; first match wins per issue.
_RULES = [
    (lambda s: "latest tag" in s or "image with latest" in s or "pinned" in s and "tag" in s,
     lambda: ("Your image has no pinned version tag",
              "Kubernetes treats an untagged image as ':latest'. That isn't reproducible and breaks rollbacks, so it's blocked as a CRITICAL policy violation.",
              "Put a specific version in the Image field, e.g. `myimage:1.4.2` — or an immutable digest like `myimage@sha256:...`. Then deploy again.")),
    (lambda s: "cpu" in s and ("limit" in s or "request" in s),
     lambda: ("CPU request/limit is missing",
              "kube-score wants an explicit CPU request AND limit so the scheduler can place the pod and cap a noisy neighbour.",
              "The platform sets CPU defaults (50m/500m). If you changed the chart, restore both a `resources.requests.cpu` and `resources.limits.cpu`.")),
    (lambda s: "memory" in s and ("limit" in s or "request" in s),
     lambda: ("Memory request/limit is missing",
              "kube-score wants an explicit memory request AND limit to prevent OOM and over-scheduling.",
              "The platform sets memory defaults (64Mi/256Mi). If you changed the chart, restore both `resources.requests.memory` and `resources.limits.memory`.")),
    (lambda s: "readonlyrootfilesystem" in s or "read only root" in s or "read-only root" in s,
     lambda: ("Container root filesystem isn't read-only",
              "A writable root filesystem is a hardening risk; kube-score flags it.",
              "The platform sets readOnlyRootFilesystem by default. If you overrode securityContext, set it back to true and mount an emptyDir for any writable paths your app needs.")),
    (lambda s: "runasnonroot" in s or "run as non-root" in s or "running as root" in s,
     lambda: ("Container may run as root",
              "Running as root inside the container is a privilege-escalation risk.",
              "The platform sets runAsNonRoot by default. Make sure your image has a non-root USER, or restore the securityContext the platform generates.")),
    (lambda s: "networkpolicy" in s,
     lambda: ("No NetworkPolicy is attached",
              "kube-score prefers a default-deny NetworkPolicy. The platform defers this to a later hardening phase, so it's usually ignored — if you see it gating, a policy is required in your cluster.",
              "Add a NetworkPolicy for this app, or (if this is expected) it's a known deferred item — safe to proceed once other issues are fixed.")),
    (lambda s: "could not find schema" in s or "schema" in s and ("find" in s or "unknown" in s or "invalid" in s),
     lambda: ("A manifest uses a Kind/apiVersion the validator doesn't recognise",
              "kubeconform couldn't find a schema for a resource — usually a CRD that isn't registered, or a typo in `kind`/`apiVersion`.",
              "Check the resource's `kind` and `apiVersion` for typos. If it's a Custom Resource, its CRD must be installed in the cluster first.")),
    (lambda s: "connection refused" in s or "was refused" in s or "unreachable" in s or "timed out" in s or "no such host" in s,
     lambda: ("The cluster API isn't reachable",
              "kubectl couldn't talk to the cluster — it's down, the context is wrong, or the kubeconfig is stale.",
              "Check `kubectl config current-context` points at a running cluster (e.g. `kind-helmsman`), start it if needed, then deploy again.")),
    (lambda s: "already exists" in s or "field is immutable" in s or "immutable" in s or "conflict" in s,
     lambda: ("A conflicting resource already exists in the cluster",
              "An existing resource blocks this apply — usually an immutable field (like a Service selector) or a name clash.",
              "Delete or rename the existing resource, or change this app's name/namespace, then deploy again.")),
    (lambda s: "forbidden" in s or "cannot create" in s or "is not allowed" in s or "rbac" in s,
     lambda: ("Your kubeconfig user lacks permission",
              "The cluster's RBAC denies creating these resources in this namespace.",
              "Use a context/service-account with rights to create Deployments/Services/etc in the target namespace.")),
    (lambda s: "crashloopbackoff" in s or "crash" in s,
     lambda: ("The container starts then crashes (CrashLoopBackOff)",
              "The process exits with an error right after start — usually a bad command, a missing env var/secret, or the app failing on boot.",
              "Check the container logs for the real error, fix the start command or the missing config/secret, and redeploy. A wrong port or missing dependency is the common cause.")),
    (lambda s: "imagepullbackoff" in s or "errimagepull" in s or "pull" in s and "image" in s,
     lambda: ("Kubernetes can't pull the image (ImagePullBackOff)",
              "The image name/tag is wrong, the image isn't pushed, or the registry needs credentials.",
              "Verify the exact image and tag exist in the registry (`docker pull` it yourself). For a private registry, add an imagePullSecret.")),
    (lambda s: "oomkilled" in s or "out of memory" in s,
     lambda: ("The container was killed for using too much memory (OOMKilled)",
              "The app exceeded its memory limit.",
              "Raise the memory request/limit for this app, or reduce the app's memory use, then redeploy.")),
    (lambda s: "internal error" in s or "unexpected error" in s or "traceback" in s,
     lambda: ("The agent hit an unexpected internal error",
              "This looks like a platform/code problem, not your configuration — the pipeline threw where it shouldn't.",
              "This one is on the code side: check the backend logs for the traceback (the raw error is below). Retry the deploy; if it repeats, it's a bug worth reporting.")),
    (lambda s: "cve-" in s or "vuln" in s or "trivy" in s and "critical" in s,
     lambda: ("The image has known vulnerabilities at or above the gate",
              "trivy found CRITICAL/HIGH CVEs in the image, so it's blocked before reaching the cluster.",
              "Rebuild on a patched base image or bump to a newer image tag that fixes the CVEs, then deploy again.")),
]

def _one(issue: str) -> dict:
    low = issue.lower()
    for pred, build in _RULES:
        try:
            if pred(low):
                p, c, f = build()
                return {"problem": p, "cause": c, "fix": f,
                        "checker": _checker(issue), "raw": issue}
        except Exception:
            continue
    # fallback: no known rule — still be useful, echo the raw message.
    return {"problem": "A pre-deploy check blocked the deployment",
            "cause": "A policy or schema check failed. The checker's exact message is shown under 'raw'.",
            "fix": "Adjust your image or config to satisfy the check, then deploy again.",
            "checker": _checker(issue), "raw": issue}

def diagnose(stage: str, issues) -> dict:
    """issues: a list[str] of checker messages, or a single string."""
    if isinstance(issues, str):
        issues = [issues]
    issues = [i for i in (issues or []) if i and str(i).strip()]
    items, seen = [], set()
    for i in issues:
        it = _one(str(i))
        key = it["problem"]
        if key not in seen:
            seen.add(key)
            items.append(it)
    if not items:
        items = [_one("unknown error")]
    n = len(items)
    return {
        "stage": stage,
        "summary": f"{n} issue{'s' if n != 1 else ''} to fix before I can deploy — I can't safely auto-fix {'these' if n != 1 else 'this'} for you.",
        "items": items,
        "auto_fixable": False,
    }


if __name__ == "__main__":
    g = diagnose("Validate", ["kube-score: [CRITICAL] apex apps/v1/Deployment: (apex) Image with latest tag"])
    assert g["items"][0]["problem"] == "Your image has no pinned version tag", g
    assert "1.4.2" in g["items"][0]["fix"]
    g2 = diagnose("Validate", ["schema: could not find schema for CronWidget"])
    assert "Kind/apiVersion" in g2["items"][0]["problem"], g2
    g3 = diagnose("Validate", ["some brand new checker message"])
    assert g3["items"][0]["checker"] == "validator" and g3["items"][0]["raw"]
    g4 = diagnose("Validate", ["kube-score: cpu request", "kube-score: cpu limit missing"])
    assert len(g4["items"]) == 1  # deduped by problem
    print("diagnostics ok:", g["summary"])
