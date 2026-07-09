import json
import subprocess

_GOOD = {"deployed", "superseded"}

def get_revisions(name: str, namespace: str) -> list[dict]:
    r = subprocess.run(
        ["helm", "history", name, "-n", namespace, "-o", "json"],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return []
    try:
        items = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    return [{"revision": int(i["revision"]), "status": i["status"]} for i in items]

def previous_good_revision(revisions: list[dict]) -> int | None:
    if not revisions:
        return None
    current = max(r["revision"] for r in revisions)
    candidates = [r["revision"] for r in revisions
                  if r["revision"] < current and r["status"] in _GOOD]
    return max(candidates) if candidates else None

def do_rollback(name: str, namespace: str, revision: int) -> None:
    subprocess.run(
        ["helm", "rollback", name, str(revision), "-n", namespace,
         "--wait", "--timeout", "120s"],
        capture_output=True, text=True, check=True,
    )
