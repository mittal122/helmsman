# Deny-by-default: only actions explicitly listed here may auto-run in autonomous
# mode. Deletes / namespace / PVC / CRD ops are NOT here and stay human-gated.
SAFE_AUTO_ACTIONS = {"rollback"}


def is_destructive(action: str) -> bool:
    return action not in SAFE_AUTO_ACTIONS
