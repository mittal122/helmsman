import asyncio
import pytest
from approvals import Approvals

@pytest.mark.asyncio
async def test_resolve_completes_future():
    a = Approvals()
    fut = a.create("d1")
    assert a.resolve("d1", True) is True
    assert await fut is True

@pytest.mark.asyncio
async def test_resolve_unknown_key_returns_false():
    a = Approvals()
    assert a.resolve("nope", True) is False
