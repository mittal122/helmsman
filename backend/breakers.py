class Breaker:
    def __init__(self, max_attempts: int = 2) -> None:
        self.max_attempts = max_attempts
        self._counts: dict[str, int] = {}

    def record(self, key: str) -> None:
        self._counts[key] = self._counts.get(key, 0) + 1

    def tripped(self, key: str) -> bool:
        return self._counts.get(key, 0) >= self.max_attempts

    def reset(self, key: str) -> None:
        self._counts.pop(key, None)
