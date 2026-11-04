"""Bounded, process-local remote sessions. Authorization is never cached here."""

import atexit
import contextlib
import threading
import time


class RemotePool:
    def __init__(self, capacity=32, clock=time.monotonic):
        self.capacity, self.clock = capacity, clock
        self.entries = {}
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.reaper = None

    def _start(self):
        if self.reaper is None:
            self.reaper = threading.Thread(target=self._reap, daemon=True, name="remote-session-reaper")
            self.reaper.start()

    def _reap(self):
        while not self.stopped.wait(5):
            self.expire()

    def expire(self):
        with self.lock:
            for key, entry in list(self.entries.items()):
                if not entry["busy"] and self.clock() - entry["used"] >= entry["idle"]:
                    self.entries.pop(key)
                    entry["resource"].close()

    @contextlib.contextmanager
    def lease(self, scope, fingerprint, factory, idle=120):
        # Serialize per scope; independent users/connections have independent locks.
        # A busy entry is never replaced underneath a running command.
        deadline = self.clock() + 120
        while True:
            with self.lock:
                self._start()
                self.expire()
                entry = self.entries.get(scope)
                if entry is None or not entry["busy"]:
                    if entry is not None and (
                        entry["fingerprint"] != fingerprint
                        or getattr(entry["resource"], "closed", False)
                        or (hasattr(entry["resource"], "healthy") and not entry["resource"].healthy())
                    ):
                        entry["resource"].close()
                        del self.entries[scope]
                        entry = None
                    if entry is None:
                        if len(self.entries) >= self.capacity:
                            candidates = [(e["used"], k) for k, e in self.entries.items() if not e["busy"]]
                            if not candidates:
                                raise RuntimeError("Remote session capacity reached; try again later")
                            _, victim = min(candidates)
                            self.entries.pop(victim)["resource"].close()
                        entry = {
                            "resource": factory(),
                            "fingerprint": fingerprint,
                            "busy": False,
                            "used": self.clock(),
                            "idle": idle,
                        }
                        self.entries[scope] = entry
                    entry["busy"] = True
                    break
            if self.clock() >= deadline:
                raise TimeoutError("Remote connection is busy; command was not sent")
            self.stopped.wait(0.02)
        try:
            yield entry["resource"]
        except BaseException:
            with self.lock:
                if self.entries.get(scope) is entry:
                    self.entries.pop(scope)
                entry["resource"].close()
            raise
        finally:
            with self.lock:
                entry["busy"] = False
                entry["used"] = self.clock()

    def close(self):
        self.stopped.set()
        with self.lock:
            for entry in self.entries.values():
                entry["resource"].close()
            self.entries.clear()


POOL = RemotePool()
atexit.register(POOL.close)
