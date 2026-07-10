import json
import re
import subprocess

_GOOD = {"deployed", "superseded"}
_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?\Z")

def _check(name: str, namespace: str) -> None:
    # argv-flag-injection guard: a leading-dash name/namespace would be read as a helm
    # flag. Validate here at the tool (choke point) so every caller — manual /rollback
    # endpoint and coordinator auto-remediate — is covered.
    if not _NAME_RE.match(name or "") or not _NAME_RE.match(namespace or ""):
        raise ValueError("invalid name/namespace (must be RFC1123, no leading dash)")

def get_revisions(name: str, namespace: str) -> list[dict]:
    _check(name, namespace)
    # timeout: this runs on the auto-remediate path, which fires exactly when the cluster
    # is likely unhealthy/unreachable — it must not hang forever. Empty => caller escalates.
    try:
        r = subprocess.run(
            ["helm", "history", name, "-n", namespace, "-o", "json"],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return []
    if r.returncode != 0 or not r.stdout.strip():
        return []
    try:
        items = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    return [{"revision": int(i["revision"]), "status": i["status"]} for i in items]

def previous_good_revision(revisions: list[dict]) -> int | None:
    # ponytail: "good" = helm status deployed/superseded. install() omits --wait, so a
    # broken upgrade still lands as deployed->superseded. Correct for the common case
    # (one bad revision after a good one), but with 2+ consecutive bad revisions this can
    # target a broken one. Bounded: do_rollback --wait times out on a bad target -> the
    # coordinator escalates (no false "recovered"), and the breaker caps retries. Real fix
    # (track coordinator Verify-passed revision) is Phase 5 state-persistence work; adding
    # --wait to install would instead break auto-rollback (install would raise before the
    # Verify-timeout path that triggers remediate).
    if not revisions:
        return None
    current = max(r["revision"] for r in revisions)
    candidates = [r["revision"] for r in revisions
                  if r["revision"] < current and r["status"] in _GOOD]
    return max(candidates) if candidates else None

def do_rollback(name: str, namespace: str, revision: int) -> None:
    _check(name, namespace)
    # subprocess timeout (>helm's --timeout 120s) hard-kills helm if it wedges before
    # honoring its own timeout (e.g. blocked on the initial API dial).
    subprocess.run(
        ["helm", "rollback", name, str(revision), "-n", namespace,
         "--wait", "--timeout", "120s"],
        capture_output=True, text=True, check=True, timeout=150,
    )
