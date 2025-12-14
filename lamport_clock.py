# lamport_clock.py

import threading

class LamportClock:
    def __init__(self, initial: int = 0):
        self._time = initial
        self._lock = threading.Lock()

    @property
    def time(self) -> int:
        with self._lock:
            return self._time

    def tick(self) -> int:
        with self._lock:
            self._time += 1
            return self._time

    def update_on_send(self) -> int:
        return self.tick()

    def update_on_recv(self, other_ts: int) -> int:
        with self._lock:
            self._time = max(self._time, int(other_ts)) + 1
            return self._time
