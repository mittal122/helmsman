import asyncio

class Approvals:
    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future] = {}

    def create(self, key: str) -> asyncio.Future:
        fut = asyncio.get_event_loop().create_future()
        self._pending[key] = fut
        return fut

    def resolve(self, key: str, approved: bool) -> bool:
        fut = self._pending.pop(key, None)
        if fut and not fut.done():
            fut.set_result(approved)
            return True
        return False
