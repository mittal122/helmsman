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

def test_fix_prompt_is_ai_ready_and_self_contained():
    g = diagnostics.diagnose("Validate",
        ["kube-score: [CRITICAL] (apex) Image with latest tag"],
        {"name": "apex", "image": "apex", "namespace": "prod"})
    fp = g["fix_prompt"]
    # includes the context an outside AI can't otherwise know
    assert "App name: apex" in fp and "Container image: apex" in fp and "Namespace: prod" in fp
    assert "Stage that failed: Validate" in fp
    # includes the verbatim checker output + the diagnosis + a concrete ask
    assert "Image with latest tag" in fp
    assert "Problem:" in fp and "Suggested fix:" in fp and "What I need from you" in fp

def test_fix_prompt_without_context_still_valid():
    g = diagnostics.diagnose("Scan", ["trivy: CVE-2024-1 critical"])
    assert "(your app)" in g["fix_prompt"] and "What I need from you" in g["fix_prompt"]

def test_crash_logs_diagnosis_names_the_real_cause():
    # a postgres crash-loop caused by a missing password -> specific cause, not "check the logs"
    logs = "PostgreSQL Database directory appears to contain a database...\n" \
           "Error: Database is uninitialized and superuser password is not specified."
    cr = diagnostics.diagnose_crash(logs)
    assert cr and "PostgreSQL won't start" in cr["problem"] and "POSTGRES_PASSWORD" in cr["fix"]
    g = diagnostics.diagnose("Verify", ["CrashLoopBackOff on postgres-x"],
                             {"name": "postgres", "image": "postgres:16", "namespace": "default"}, logs=logs)
    assert g["items"][0]["problem"].startswith("PostgreSQL")           # crash cause is primary
    # the real logs go verbatim into the fix-prompt the developer hands to their AI
    assert "Actual container logs" in g["fix_prompt"] and "superuser password is not specified" in g["fix_prompt"]

def test_crash_logs_connection_refused_and_none():
    assert diagnostics.diagnose_crash("Error: connect ECONNREFUSED 10.0.0.1:5432")["problem"].startswith("The app can't reach")
    assert diagnostics.diagnose_crash("permission denied, mkdir '/data'")["problem"].startswith("The app can't write")
    assert diagnostics.diagnose_crash("") is None
    assert diagnostics.diagnose_crash("Listening on :8080, ready") is None   # a healthy log -> no false positive

def test_db_required_env_guard():
    assert "POSTGRES_PASSWORD" in diagnostics.db_required_env_missing("postgres:16", {})
    assert diagnostics.db_required_env_missing("postgres:16", {"POSTGRES_PASSWORD": "x"}) == ""
    assert diagnostics.db_required_env_missing("postgres:16", {"POSTGRES_PASSWORD": ""}) != ""   # empty = missing
    assert diagnostics.db_required_env_missing("nginx:1.27", {}) == ""
    assert diagnostics.db_password_field("mysql:8") == "MYSQL_ROOT_PASSWORD"
    assert diagnostics.db_password_field("nginx") == ""
