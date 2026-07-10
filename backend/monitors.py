class Monitors:
    def __init__(self) -> None:
        self._stopped: set[str] = set()
        self._active: set[str] = set()

    def start(self, key: str) -> None:
        self._stopped.discard(key)
        self._active.add(key)

    def stop(self, key: str) -> None:
        self._stopped.add(key)
        self._active.discard(key)

    def stop_all(self) -> None:
        # single-deploy-by-design: a new deploy halts any prior deploy's monitor loop
        # so stale health events (e.g. an old app's pods) stop polluting the UI.
        for k in list(self._active):
            self._stopped.add(k)
        self._active.clear()

    def is_stopped(self, key: str) -> bool:
        return key in self._stopped
