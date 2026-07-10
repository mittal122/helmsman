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

def valid_ref(v: str) -> bool:
    return bool(_GIT_REF.match(v or ""))

def valid_url(url: str) -> bool:
    return bool(_GIT_URL.match(url or ""))

def display_url(url: str) -> str:
    """Strip any `user:token@` credentials before the URL is logged/shown."""
    return re.sub(r"//[^/@]*@", "//", url or "")

def _run(args, cwd=None, timeout=60):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:600] or "command failed")
    return r.stdout

def current_context() -> str:
    r = subprocess.run(["kubectl", "config", "current-context"],
                       capture_output=True, text=True, timeout=10)
    return r.stdout.strip() if r.returncode == 0 else ""

def clone(repo_url: str, branch: str = "", ref: str = "") -> tuple[str, str]:
    """Shallow-clone the repo; return (workdir, short_commit_sha)."""
    if not _GIT_URL.match(repo_url or ""):
        raise ValueError("invalid git repo URL (must be https://… or git@…)")
    # branch/ref become bare positional argv to git — a leading '-' would be read as a flag
    if branch and not valid_ref(branch):
        raise ValueError("invalid branch name")
    if ref and not valid_ref(ref):
        raise ValueError("invalid git ref")
    workdir = tempfile.mkdtemp(prefix="helmsman-src-")
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
    if context.startswith("kind-"):
        _run(["kind", "load", "docker-image", image_tag, "--name", context[len("kind-"):]],
             timeout=LOAD_TIMEOUT_S)
        return "kind"
    if context == "minikube":
        _run(["minikube", "image", "load", image_tag], timeout=LOAD_TIMEOUT_S)
        return "minikube"
    if os.environ.get("REGISTRY"):
        _run(["docker", "push", image_tag], timeout=LOAD_TIMEOUT_S)
        return "registry"
    raise RuntimeError(f"cluster '{context}' isn't local (kind/minikube); set REGISTRY "
                       f"and use a pushable image tag to deploy a source build to it")

def cleanup(workdir: str) -> None:
    if workdir and workdir.startswith(tempfile.gettempdir()):
        shutil.rmtree(workdir, ignore_errors=True)

def image_tag(name: str, sha: str) -> str:
    reg = os.environ.get("REGISTRY", "").rstrip("/")
    base = f"{reg}/{name}" if reg else name
    return f"{base}:src-{sha}"
