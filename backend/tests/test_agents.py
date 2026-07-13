import agents.onboarding as onboarding
import agents.config_advisor as config_advisor
import agents.error_resolver as error_resolver

def _spy(monkeypatch, module):
    calls = {}
    def fake(prompt_file, placeholders, schema):
        calls.update(prompt_file=prompt_file, placeholders=placeholders, schema=schema)
        return {"ok": True}
    monkeypatch.setattr(module.base, "call_agent", fake)
    return calls

def test_onboarding_maps_fields(monkeypatch):
    calls = _spy(monkeypatch, onboarding)
    onboarding.generate({"app_description": "a node app", "language_framework": "node"})
    assert calls["prompt_file"] == "onboarding.md"
    assert calls["placeholders"]["app_description"] == "a node app"

def test_config_advisor_maps_fields(monkeypatch):
    calls = _spy(monkeypatch, config_advisor)
    config_advisor.advise({"name": "orders", "image": "orders:1", "port": 3000})
    assert calls["prompt_file"] == "config-advisor.md"
    assert calls["placeholders"]["app_name"] == "orders"
    assert calls["placeholders"]["detected_port"] == 3000

def test_error_resolver_maps_fields(monkeypatch):
    calls = _spy(monkeypatch, error_resolver)
    error_resolver.resolve({"failure_type": "ImagePullBackOff", "recent_logs": "boom"})
    assert calls["prompt_file"] == "error-resolution.md"
    assert calls["placeholders"]["failure_type"] == "ImagePullBackOff"
    assert calls["placeholders"]["recent_logs"] == "boom"

import agents.stack_reviewer as stack_reviewer

def test_stack_reviewer_summary_never_leaks_secret_values():
    services = [
        {"name": "backend", "image": "org/api:1", "workload": "deployment", "port": 8000,
         "published": True, "env": {"DB_HOST": "db"}, "secrets": {"DB_PASSWORD": "s3cret-value"}},
        {"name": "db", "image": "postgres:16", "workload": "deployment", "port": 5432,
         "secrets": {"POSTGRES_PASSWORD": "another-secret"}},
    ]
    s = stack_reviewer.summarize(services)
    # secret KEY names are included; secret VALUES never are (redaction invariant)
    assert "DB_PASSWORD" in s and "POSTGRES_PASSWORD" in s
    assert "s3cret-value" not in s and "another-secret" not in s
    assert "name=backend" in s and "image=postgres:16" in s

def test_stack_reviewer_review_maps_fields(monkeypatch):
    calls = _spy(monkeypatch, stack_reviewer)
    stack_reviewer.review([{"name": "web", "image": "w:1", "port": 80, "secrets": {"K": "v"}}])
    assert calls["prompt_file"] == "stack-review.md"
    assert "name=web" in calls["placeholders"]["stack"] and "v" not in calls["placeholders"]["stack"]
