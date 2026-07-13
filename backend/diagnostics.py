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
    (lambda s: "dockerfile not found" in s,
     lambda: ("The repo has no Dockerfile at that path",
              "Deploy-from-source builds the Dockerfile in your repo, but none was found at the given path.",
              "Add a Dockerfile at the repo root, or set the correct path in the Dockerfile field (e.g. `docker/Dockerfile`).")),
    (lambda s: "docker daemon" in s or "cannot connect to the docker daemon" in s,
     lambda: ("No running Docker daemon on the build host",
              "Building from source needs Docker on the machine running Helmsman.",
              "Start Docker (`sudo systemctl start docker`, or Docker Desktop), then deploy again — or deploy a pre-built image instead of a git repo.")),
    (lambda s: ("repository" in s and "not found" in s) or "could not read from remote" in s or "authentication failed" in s or ("clone" in s and "fail" in s),
     lambda: ("The git repo couldn't be cloned",
              "The URL/branch is wrong or unreachable, or the repo is private and needs credentials. "
              "Tip: paste the plain repo URL (…/repo.git), not a browser link to a branch/folder — "
              "Helmsman now auto-converts /tree/<branch>/<subdir> links, but a wrong repo still won't clone.",
              "Check the repo URL and branch. For a private repo use an https URL with a token: `https://<token>@host/org/repo.git`.")),
    (lambda s: "isn't local" in s or "set registry" in s,
     lambda: ("A source build can't reach a remote cluster without a registry",
              "For kind/minikube the built image is loaded straight into the cluster; a remote cluster must pull it from a registry.",
              "Set the REGISTRY env var to a registry you can push to, or deploy the source to a local kind/minikube cluster.")),
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
    (lambda s: "createcontainerconfigerror" in s or "createcontainererror" in s or "invalidimagename" in s,
     lambda: ("Kubernetes can't build the container's config (CreateContainerConfigError)",
              "Usually a referenced Secret or ConfigMap (or a key inside it) is missing, or an env/volume points at something that doesn't exist.",
              "Check that every Secret/ConfigMap your env and volumes reference exists in the namespace with the right keys — create the missing one or fix the reference, then deploy again.")),
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

# ---- crash-log catalog: map the REAL container logs of a crash-looping pod to a specific
# cause + fix. This is what makes "CrashLoopBackOff" actionable instead of "check the logs".
# Ordered; first match wins. Predicates run on the lowercased log text.
_CRASH_RULES = [
    (lambda s: "superuser password is not specified" in s or ("postgres_password" in s and ("not specified" in s or "not set" in s)),
     lambda: ("PostgreSQL won't start — no password is set",
              "The postgres image refuses to initialize without POSTGRES_PASSWORD (or POSTGRES_HOST_AUTH_METHOD).",
              "Set POSTGRES_PASSWORD for the database service (it's stored as a Secret). In the guided flow this is a required value — make sure it isn't left blank. For a throwaway local DB you can instead set POSTGRES_HOST_AUTH_METHOD=trust.")),
    (lambda s: "you need to specify one of mysql_root_password" in s or "mariadb_root_password" in s or ("mysql_root_password" in s and "password" in s),
     lambda: ("MySQL/MariaDB won't start — no root password is set",
              "The image needs MYSQL_ROOT_PASSWORD (or MYSQL_ALLOW_EMPTY_PASSWORD / MYSQL_RANDOM_ROOT_PASSWORD) to initialize.",
              "Set MYSQL_ROOT_PASSWORD for the database service (stored as a Secret) and make sure it isn't blank.")),
    (lambda s: "mongo_initdb_root_password" in s or ("mongo" in s and "password" in s and "not" in s),
     lambda: ("MongoDB won't start — no root password is set",
              "The mongo image needs MONGO_INITDB_ROOT_USERNAME and MONGO_INITDB_ROOT_PASSWORD.",
              "Set MONGO_INITDB_ROOT_USERNAME and MONGO_INITDB_ROOT_PASSWORD (Secret) for the database service.")),
    (lambda s: "password authentication failed" in s or "access denied for user" in s or "authentication failed" in s,
     lambda: ("The app can't log in to its database (wrong password)",
              "The password the app uses doesn't match the database's. This happens when the two use different Secret values.",
              "Use the SAME password value for the database's POSTGRES_PASSWORD/MYSQL_PASSWORD and the app's DB password env var — one shared Secret value.")),
    (lambda s: "connection refused" in s or "econnrefused" in s or "could not connect to server" in s or "could not translate host name" in s or "getaddrinfo" in s or "no route to host" in s,
     lambda: ("The app can't reach a service it depends on (e.g. the database)",
              "The app started before its dependency was ready, or it's using the wrong host/port. Start-order alone doesn't guarantee the dependency is READY to accept connections.",
              "Point the app at the dependency by its service name and port (e.g. host `db`, port `5432` — cluster DNS resolves it). Make the app retry the connection on startup instead of exiting; a DB takes a few seconds to accept connections after its pod starts.")),
    (lambda s: "address already in use" in s or "eaddrinuse" in s or "bind: address already in use" in s,
     lambda: ("The app's port is already in use",
              "The container tried to bind a port that's taken, or the port it listens on doesn't match the one declared.",
              "Make the port the app binds match the port you told the platform it listens on, and make sure only one process binds it.")),
    (lambda s: "read-only file system" in s or ("permission denied" in s and ("mkdir" in s or "open" in s or "write" in s)),
     lambda: ("The app can't write to a folder (read-only filesystem)",
              "For security the container's root filesystem is read-only by default; the app is trying to write somewhere that isn't a mounted volume.",
              "Give the app a writable volume for that path (a persistent volume if the data must survive, or a scratch space otherwise). A database service that declares a data volume gets a writable data dir automatically.")),
    (lambda s: "modulenotfounderror" in s or "cannot find module" in s or "no module named" in s or "importerror" in s,
     lambda: ("A code dependency is missing from the image",
              "The app can't import a library — it wasn't installed in the built image.",
              "Add the missing dependency to your requirements/package file and rebuild the image, then redeploy.")),
    (lambda s: "keyerror" in s or ("environment variable" in s and ("not set" in s or "missing" in s or "required" in s or "undefined" in s)),
     lambda: ("A required environment variable is missing",
              "The app read an env var that wasn't provided and crashed on startup.",
              "Add the missing environment variable (as env, or a Secret if it's sensitive) for this service. The crash log above names it.")),
    (lambda s: "exec format error" in s or "exec /" in s and "no such file" in s or "no such file or directory" in s and "exec" in s,
     lambda: ("The container can't run its start command",
              "Either the image is built for a different CPU architecture, or the entrypoint/command points at a file that isn't there.",
              "Rebuild the image for the right architecture, or fix the start command/entrypoint path.")),
]

def diagnose_crash(logs: str) -> dict | None:
    """Match a crash-looping pod's REAL logs to a specific cause. None if no rule matches."""
    low = (logs or "").lower()
    if not low.strip():
        return None
    for pred, build in _CRASH_RULES:
        try:
            if pred(low):
                p, c, f = build()
                return {"problem": p, "cause": c, "fix": f, "checker": "container logs", "raw": ""}
        except Exception:
            continue
    return None


# known stateful images -> the env var(s) that MUST be non-empty or the container crash-loops.
# Used proactively (before deploy) so a database never crash-loops for the #1 reason.
_DB_REQUIRED_ENV = [
    (("postgres", "postgis", "timescale", "pgvector"),
     ("POSTGRES_PASSWORD", "POSTGRES_HOST_AUTH_METHOD"),
     "POSTGRES_PASSWORD (or POSTGRES_HOST_AUTH_METHOD=trust for a local-only DB)"),
    (("mysql", "mariadb", "percona"),
     ("MYSQL_ROOT_PASSWORD", "MYSQL_ALLOW_EMPTY_PASSWORD", "MYSQL_RANDOM_ROOT_PASSWORD"),
     "MYSQL_ROOT_PASSWORD"),
    (("mongo",),
     ("MONGO_INITDB_ROOT_PASSWORD",),
     "MONGO_INITDB_ROOT_USERNAME and MONGO_INITDB_ROOT_PASSWORD"),
]

def db_required_env_missing(image: str, provided: dict) -> str:
    """If `image` is a known database that needs an init password and none of its accepted env
    keys is present with a non-empty value, return a human hint naming what to set. Else ''.
    `provided` is the merged env+secrets dict for the service."""
    img = (image or "").lower()
    have = {k for k, v in (provided or {}).items() if str(v).strip()}
    for names, accepted, hint in _DB_REQUIRED_ENV:
        if any(n in img for n in names) and not (set(accepted) & have):
            return hint
    return ""

def db_password_field(image: str) -> str:
    """The primary password env key for a known database image (else '')."""
    img = (image or "").lower()
    for names, accepted, _hint in _DB_REQUIRED_ENV:
        if any(n in img for n in names):
            return accepted[0]
    return ""

# a stateful image -> the directory it writes its data to. Used to auto-attach the right volume
# (a DB with no volume loses data AND fails the read-only-root policy -> crash-loop).
_DB_DATA_PATH = [
    (("postgres", "postgis", "timescale", "pgvector"), "/var/lib/postgresql/data"),
    (("mariadb", "mysql", "percona"), "/var/lib/mysql"),
    (("mongo",), "/data/db"),
    (("redis",), "/data"),
    (("elasticsearch", "opensearch"), "/usr/share/elasticsearch/data"),
    (("rabbitmq",), "/var/lib/rabbitmq"),
]

def db_data_path(image: str) -> str:
    """The data directory a known stateful image writes to (else '')."""
    img = (image or "").lower()
    for names, path in _DB_DATA_PATH:
        if any(n in img for n in names):
            return path
    return ""


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

def build_fix_prompt(stage: str, context: dict, items: list, issues: list, logs: str = "") -> str:
    """A self-contained, copy-pasteable prompt the user can hand to ANY AI/coding
    assistant so it can fix the issue in their project. Includes the context an
    outside AI can't otherwise know (app, image, exact checker output)."""
    c = context or {}
    name = c.get("name") or "(your app)"
    image = c.get("image") or "(your image)"
    ns = c.get("namespace") or "default"
    L = []
    L.append("You are helping me fix a Kubernetes deployment that was blocked by an "
             "automated check. I deploy a prebuilt container image to Kubernetes using "
             "a Helm chart. Read the details below and tell me the exact changes to make "
             "in my project to fix it.")
    L.append("")
    L.append("## Context")
    L.append(f"- App name: {name}")
    L.append(f"- Container image: {image}")
    L.append(f"- Namespace: {ns}")
    L.append(f"- Stage that failed: {stage}")
    L.append("")
    L.append("## What the automated checks reported (verbatim)")
    for i in (issues or []):
        L.append(f"- {i}")
    if logs and logs.strip():
        L.append("")
        L.append("## Actual container logs (verbatim — this is the real error)")
        L.append("```")
        # last ~40 lines is plenty of signal without flooding the prompt
        L.append("\n".join(logs.strip().splitlines()[-40:]))
        L.append("```")
    L.append("")
    L.append("## Diagnosis")
    for it in items:
        L.append(f"- Problem: {it['problem']}")
        L.append(f"  - Why it's blocked: {it['cause']}")
        L.append(f"  - Suggested fix: {it['fix']}")
    L.append("")
    L.append("## What I need from you")
    L.append("Give me the concrete change to make — the exact image tag/reference, "
             "Dockerfile edit, Helm value, or Kubernetes manifest field — with the "
             "specific commands or file diffs. Be precise and minimal; assume I will "
             "re-run the deploy after applying your change.")
    return "\n".join(L)

def diagnose(stage: str, issues, context: dict = None, logs: str = "") -> dict:
    """issues: a list[str] of checker messages, or a single string.
    context (optional): {name, image, namespace} — used to build the AI fix-prompt.
    logs (optional): a crash-looping pod's real container logs. When given, they're matched to
    a specific crash cause (postgres-needs-password, connection-refused, …) which becomes the
    primary item, and the logs are embedded VERBATIM in the fix-prompt so the developer's AI
    sees the real error instead of a generic template."""
    if isinstance(issues, str):
        issues = [issues]
    issues = [i for i in (issues or []) if i and str(i).strip()]
    items, seen = [], set()
    crash = diagnose_crash(logs)          # specific cause from the real logs, if any
    if crash:
        items.append(crash); seen.add(crash["problem"])
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
        "fix_prompt": build_fix_prompt(stage, context, items, issues, logs),
    }


if __name__ == "__main__":
    g = diagnose("Validate", ["kube-score: [CRITICAL] apex apps/v1/Deployment: (apex) Image with latest tag"],
                 {"name": "apex", "image": "apex", "namespace": "default"})
    assert g["items"][0]["problem"] == "Your image has no pinned version tag", g
    assert "1.4.2" in g["items"][0]["fix"]
    assert "Container image: apex" in g["fix_prompt"] and "verbatim" in g["fix_prompt"]
    assert "Image with latest tag" in g["fix_prompt"]  # includes the raw checker output
    g2 = diagnose("Validate", ["schema: could not find schema for CronWidget"])
    assert "Kind/apiVersion" in g2["items"][0]["problem"], g2
    g3 = diagnose("Validate", ["some brand new checker message"])
    assert g3["items"][0]["checker"] == "validator" and g3["items"][0]["raw"]
    g4 = diagnose("Validate", ["kube-score: cpu request", "kube-score: cpu limit missing"])
    assert len(g4["items"]) == 1  # deduped by problem

    # crash-log diagnosis: the real postgres error -> a specific cause, not "check the logs"
    pg = "2026-07-13 FATAL: database is uninitialized and superuser password is not specified"
    cr = diagnose_crash(pg)
    assert cr and "PostgreSQL won't start" in cr["problem"] and "POSTGRES_PASSWORD" in cr["fix"], cr
    gc = diagnose("Verify", ["CrashLoopBackOff on postgres-xyz"],
                  {"name": "postgres", "image": "postgres:16", "namespace": "default"}, logs=pg)
    assert gc["items"][0]["problem"].startswith("PostgreSQL"), gc["items"][0]
    assert "Actual container logs" in gc["fix_prompt"] and "superuser password" in gc["fix_prompt"]
    assert diagnose_crash("some app: connection refused to db:5432")["problem"].startswith("The app can't reach")
    assert diagnose_crash("") is None and diagnose_crash("just some normal startup log") is None

    # proactive DB guard: a postgres with no password is flagged BEFORE it can crash-loop
    assert db_required_env_missing("postgres:16", {}) and "POSTGRES_PASSWORD" in db_required_env_missing("postgres:16", {})
    assert db_required_env_missing("postgres:16", {"POSTGRES_PASSWORD": "s3cret"}) == ""
    assert db_required_env_missing("postgres:16", {"POSTGRES_PASSWORD": ""})        # empty value = still missing
    assert db_required_env_missing("mysql:8", {}) and db_required_env_missing("nginx:1.27", {}) == ""
    print("diagnostics ok:", g["summary"])
