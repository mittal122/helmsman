class Monitors:
    def __init__(self) -> None:
        self._stopped: set[str] = set()

    def start(self, key: str) -> None:
        self._stopped.discard(key)

    def stop(self, key: str) -> None:
        self._stopped.add(key)

    def is_stopped(self, key: str) -> bool:
        return key in self._stopped
