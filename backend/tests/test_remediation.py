import remediation


def test_rollback_is_safe():
    assert remediation.is_destructive("rollback") is False


def test_delete_is_destructive():
    assert remediation.is_destructive("delete-namespace") is True
    assert remediation.is_destructive("uninstall") is True
    assert remediation.is_destructive("anything-not-allowlisted") is True
