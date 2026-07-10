import pytest
import store

@pytest.mark.asyncio
async def test_inmemory_events_roundtrip(monkeypatch):
    monkeypatch.setattr(store, "DATABASE_URL", "")     # force in-memory
    assert await store.init() == "memory"
    await store.append_event({"ts": 1.0, "type": "stage_enter", "stage": "Deploy", "message": "go", "data": {}})
    await store.append_event({"ts": 2.0, "type": "endpoint", "stage": "Verify", "message": "live", "data": {"url": "x"}})
    evs = await store.recent_events(10)
    assert [e["type"] for e in evs] == ["stage_enter", "endpoint"]
    assert evs[-1]["data"]["url"] == "x"

@pytest.mark.asyncio
async def test_inmemory_audit_roundtrip(monkeypatch):
    monkeypatch.setattr(store, "DATABASE_URL", "")
    await store.init()
    await store.append_audit("operator", "delete", "prod/api", True, "helm uninstall")
    a = await store.recent_audit(10)
    assert a[0]["action"] == "delete" and a[0]["target"] == "prod/api" and a[0]["ok"] is True

@pytest.mark.asyncio
async def test_healthy_and_backend_name(monkeypatch):
    monkeypatch.setattr(store, "DATABASE_URL", "")
    await store.init()
    assert store.backend_name() == "memory"
    assert await store.healthy() is True

@pytest.mark.asyncio
async def test_postgres_falls_back_to_memory_on_bad_dsn(monkeypatch):
    # a broken DATABASE_URL must degrade to in-memory, never crash startup
    monkeypatch.setattr(store, "DATABASE_URL", "postgresql://bad:bad@127.0.0.1:1/nope")
    msg = await store.init()
    assert store.backend_name() == "memory" and "in-memory" in msg

@pytest.mark.asyncio
async def test_writes_are_best_effort_never_raise(monkeypatch):
    monkeypatch.setattr(store, "_impl", None)          # no backend
    await store.append_event({"type": "x"})            # must not raise
    await store.append_audit("op", "a", "t")           # must not raise
