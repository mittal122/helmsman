import json
import subprocess
from tools import rollback

def test_previous_good_revision_picks_highest_prior_good():
    revs = [{"revision": 1, "status": "superseded"},
            {"revision": 2, "status": "deployed"}]
    assert rollback.previous_good_revision(revs) == 1

def test_previous_good_revision_skips_failed():
    revs = [{"revision": 1, "status": "superseded"},
            {"revision": 2, "status": "failed"},
            {"revision": 3, "status": "deployed"}]
    assert rollback.previous_good_revision(revs) == 1  # 2 is failed, skip it

def test_previous_good_revision_none_when_only_one():
    assert rollback.previous_good_revision([{"revision": 1, "status": "deployed"}]) is None

def test_previous_good_revision_none_when_empty():
    assert rollback.previous_good_revision([]) is None

def test_get_revisions_parses_history(monkeypatch):
    class _R:
        returncode = 0
        stdout = json.dumps([{"revision": 1, "status": "superseded"},
                             {"revision": 2, "status": "deployed"}])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
    assert rollback.get_revisions("demo", "default") == [
        {"revision": 1, "status": "superseded"},
        {"revision": 2, "status": "deployed"}]

def test_get_revisions_empty_on_error(monkeypatch):
    class _R: returncode = 1; stdout = ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
    assert rollback.get_revisions("demo", "default") == []
