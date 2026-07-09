import asyncio
import pytest
from events import Event, EventBus

@pytest.mark.asyncio
async def test_subscriber_receives_published_event():
    bus = EventBus()
    q = bus.subscribe()
    await bus.publish(Event(type="stage_enter", stage="Deploy", message="deploying"))
    got = await asyncio.wait_for(q.get(), timeout=1)
    assert got.type == "stage_enter"
    assert got.stage == "Deploy"
    assert got.message == "deploying"

@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery():
    bus = EventBus()
    q = bus.subscribe()
    bus.unsubscribe(q)
    await bus.publish(Event(type="x", stage="s", message="m"))
    assert q.empty()
