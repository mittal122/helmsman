import diagnostics

def test_latest_tag_guidance():
    g = diagnostics.diagnose("Validate",
        ["kube-score: [CRITICAL] apex apps/v1/Deployment: (apex) Image with latest tag"])
    it = g["items"][0]
    assert it["problem"] == "Your image has no pinned version tag"
    assert "1.4.2" in it["fix"] and it["checker"] == "kube-score"
    assert g["auto_fixable"] is False

def test_schema_guidance():
    g = diagnostics.diagnose("Validate", ["schema: could not find schema for CronWidget"])
    assert "Kind/apiVersion" in g["items"][0]["problem"]
    assert g["items"][0]["checker"] == "kubeconform"

def test_crashloop_and_internal_rules():
    assert "CrashLoopBackOff" in diagnostics.diagnose("Verify", ["CrashLoopBackOff on pod x"])["items"][0]["problem"]
    assert "internal error" in diagnostics.diagnose("Deploy", ["internal error: boom"])["items"][0]["problem"].lower()

def test_unknown_issue_still_actionable():
    g = diagnostics.diagnose("Validate", ["some brand new checker message"])
    it = g["items"][0]
    assert it["raw"] == "some brand new checker message"
    assert it["fix"]  # always a fix line

def test_dedup_by_problem():
    g = diagnostics.diagnose("Validate", ["kube-score: cpu request", "kube-score: cpu limit missing"])
    assert len(g["items"]) == 1

def test_accepts_single_string():
    g = diagnostics.diagnose("Validate", "kube-score: Image with latest tag")
    assert g["items"][0]["problem"] == "Your image has no pinned version tag"
