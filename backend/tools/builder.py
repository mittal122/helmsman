"""Deploy-from-source: clone a Git repo, build its Docker image, and make it available
to the cluster — so a user ships their app from code, not a pre-built image.

Local clusters (kind/minikube) get the image injected directly into their nodes (no
registry needed — the chart pins a non-'latest' tag, so IfNotPresent uses the loaded
image). Remote clusters push to $REGISTRY.

Security: the repo URL is validated and every command uses argv (no shell → no
injection). Building an arbitrary Dockerfile runs arbitrary code at build time — that's
inherent to 'build from source'; it's the user's own repo, and deploy is operator-gated.
Isolated/sandboxed builds (kaniko) are a multi-tenant hardening item.
"""
import os
import re
import shutil
import subprocess
import tempfile

CLONE_TIMEOUT_S = 180
BUILD_TIMEOUT_S = 900
LOAD_TIMEOUT_S = 300
# https://host/path(.git) or git@host:path(.git) — no spaces, no shell metacharacters
_GIT_URL = re.compile(r"^(https://|git@)[A-Za-z0-9._~:/@%+-]+$")
# branch / commit / tag — safe git ref chars, NO leading '-' (else it's argv flag injection
# into `git fetch`/`git checkout`, which pass the ref as a bare positional argument)
_GIT_REF = re.compile(r"^[A-Za-z0-9._/][A-Za-z0-9._/-]{0,199}$")

# A GitHub/GitLab *browser* URL to a branch/subfolder — e.g.
# https://github.com/owner/repo/tree/main/subdir  (or /blob/, or GitLab /-/tree/).
# It is NOT cloneable; normalize it to (clone_url, branch, subdir).
_TREE = re.compile(
    r"^(https://[^/\s]+/[^/\s]+/[^/\s]+?)(?:\.git)?/(?:-/)?(?:tree|blob)/([^/\s]+)(?:/([^\s]*?))?/?$")

def normalize_repo_url(url: str) -> tuple[str, str, str]:
    """Turn a browser tree/blob URL into (clone_url, branch, subdir). A plain repo URL is
    returned unchanged with empty branch/subdir. Lets a user paste the GitHub page URL of a
    folder and still deploy."""
    m = _TREE.match(url or "")
    if not m:
        return (url or ""), "", ""
    base, ref, sub = m.group(1), m.group(2), (m.group(3) or "")
    return base + ".git", ref, sub.strip("/")

def valid_ref(v: str) -> bool:
    return bool(_GIT_REF.match(v or ""))

def valid_url(url: str) -> bool:
    return bool(_GIT_URL.match(url or ""))

def display_url(url: str) -> str:
    """Strip any `user:token@` credentials before the URL is logged/shown."""
    return re.sub(r"//[^/@]*@", "//", url or "")

def _run(args, cwd=None, timeout=60, env=None):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:600] or "command failed")
    return r.stdout

def current_context() -> str:
    r = subprocess.run(["kubectl", "config", "current-context"],
                       capture_output=True, text=True, timeout=10)
    return r.stdout.strip() if r.returncode == 0 else ""

def build_base() -> str:
    """Where cloned sources land. NOT /tmp: a snap-confined Docker daemon (Ubuntu's default
    `snap install docker`) can't read /tmp or hidden dirs as a build context — the build would
    fail 'path not found'. A non-hidden dir under $HOME is readable by both snap and normal
    Docker. Overridable via HELMSMAN_BUILD_DIR; falls back to the system temp dir if $HOME is
    unusable."""
    base = os.environ.get("HELMSMAN_BUILD_DIR") or os.path.join(os.path.expanduser("~"), "helmsman-build")
    try:
        os.makedirs(base, mode=0o700, exist_ok=True)
        return base
    except OSError:
        return tempfile.gettempdir()

def clone(repo_url: str, branch: str = "", ref: str = "") -> tuple[str, str]:
    """Shallow-clone the repo; return (workdir, short_commit_sha)."""
    # a pasted browser URL (…/tree/<branch>/<subdir>) isn't cloneable — normalize it and
    # pick up its branch so the clone just works.
    repo_url, url_branch, _sub = normalize_repo_url(repo_url)
    if not branch and url_branch:
        branch = url_branch
    if not _GIT_URL.match(repo_url or ""):
        raise ValueError("invalid git repo URL (must be https://… or git@…)")
    # branch/ref become bare positional argv to git — a leading '-' would be read as a flag
    if branch and not valid_ref(branch):
        raise ValueError("invalid branch name")
    if ref and not valid_ref(ref):
        raise ValueError("invalid git ref")
    workdir = tempfile.mkdtemp(prefix="helmsman-src-", dir=build_base())
    args = ["git", "clone", "--depth", "1"]
    if branch:
        args += ["--branch", branch]
    args += ["--", repo_url, workdir]
    _run(args, timeout=CLONE_TIMEOUT_S)
    if ref:
        _run(["git", "-C", workdir, "fetch", "--depth", "1", "origin", ref], timeout=CLONE_TIMEOUT_S)
        _run(["git", "-C", workdir, "checkout", ref], timeout=60)
    sha = _run(["git", "-C", workdir, "rev-parse", "--short", "HEAD"], timeout=30).strip()
    return workdir, sha

def build(workdir: str, image_tag: str, dockerfile: str = "Dockerfile") -> None:
    if ".." in dockerfile or dockerfile.startswith("/"):
        raise ValueError("invalid dockerfile path")
    df = os.path.join(workdir, dockerfile)
    if not os.path.isfile(df):
        raise ValueError(f"Dockerfile not found in repo: {dockerfile}")
    _run(["docker", "build", "-t", image_tag, "-f", df, workdir], timeout=BUILD_TIMEOUT_S)

def make_available(image_tag: str, context: str) -> str:
    """Inject the built image where the cluster can pull it. Returns the method used."""
    # kind/minikube stage the image through `docker save -o <TMPDIR>/images.tar`. A
    # snap-confined Docker daemon can't write /tmp, so point TMPDIR at the (home-based) build
    # base it CAN write — else the load fails 'invalid output path'.
    env = {**os.environ, "TMPDIR": build_base()}
    if context.startswith("kind-"):
        _run(["kind", "load", "docker-image", image_tag, "--name", context[len("kind-"):]],
             timeout=LOAD_TIMEOUT_S, env=env)
        return "kind"
    if context == "minikube":
        _run(["minikube", "image", "load", image_tag], timeout=LOAD_TIMEOUT_S, env=env)
        return "minikube"
    if os.environ.get("REGISTRY"):
        _run(["docker", "push", image_tag], timeout=LOAD_TIMEOUT_S)
        return "registry"
    raise RuntimeError(f"cluster '{context}' isn't local (kind/minikube); set REGISTRY "
                       f"and use a pushable image tag to deploy a source build to it")

def cleanup(workdir: str) -> None:
    # only remove our own clone dirs (helmsman-src-* under the build base or the temp dir)
    if not workdir or not os.path.basename(workdir).startswith("helmsman-src-"):
        return
    allowed = (os.path.realpath(build_base()), os.path.realpath(tempfile.gettempdir()))
    if os.path.realpath(workdir).startswith(allowed):
        shutil.rmtree(workdir, ignore_errors=True)

_SKIP_DIRS = {".git", "node_modules", "vendor", ".venv", "dist", "build", "__pycache__"}

def _is_dockerfile(fname: str) -> bool:
    return fname == "Dockerfile" or fname.startswith("Dockerfile.") or fname.endswith(".Dockerfile")

def list_dockerfiles(workdir: str, max_depth: int = 4, max_files: int = 50) -> list[str]:
    """Find Dockerfiles anywhere in a cloned repo. Returns sorted POSIX-relative paths
    (root 'Dockerfile' first). Bounded by depth/count so a huge monorepo can't blow up."""
    found: list[str] = []
    root = os.path.abspath(workdir)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = dirpath[len(root):].count(os.sep)
        if depth >= max_depth:
            dirnames[:] = []            # stop descending past the cap
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for f in filenames:
            if _is_dockerfile(f):
                found.append(os.path.relpath(os.path.join(dirpath, f), root).replace(os.sep, "/"))
                if len(found) >= max_files:
                    break
        if len(found) >= max_files:
            break
    # root "Dockerfile" first, then alphabetical
    return sorted(found, key=lambda p: (p != "Dockerfile", p))

def image_tag(name: str, sha: str) -> str:
    reg = os.environ.get("REGISTRY", "").rstrip("/")
    base = f"{reg}/{name}" if reg else name
    return f"{base}:src-{sha}"
