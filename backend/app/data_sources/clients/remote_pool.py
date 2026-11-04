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
        # Waiters sleep on this until an entry is released, created or removed,
        # instead of polling the lock.
        self.changed = threading.Condition(self.lock)
        self.stopped = threading.Event()
        self.reaper = None

    def _start(self):
        if self.reaper is None:
            self.reaper = threading.Thread(target=self._reap, daemon=True, name="remote-session-reaper")
            self.reaper.start()

    def _reap(self):
        while not self.stopped.wait(5):
            self.expire()

    def _evict(self, key):
        """Remove ``key`` under the lock; the caller closes the resource after releasing it."""
        entry = self.entries.pop(key)
        self.changed.notify_all()
        return entry["resource"]

    def expire(self):
        victims = []
        with self.lock:
            for key, entry in list(self.entries.items()):
                if not entry["busy"] and self.clock() - entry["used"] >= entry["idle"]:
                    victims.append(self._evict(key))
        for resource in victims:
            resource.close()

    @contextlib.contextmanager
    def lease(self, scope, fingerprint, factory, idle=120):
        # Serialize per scope; independent users/connections have independent locks.
        # A busy entry is never replaced underneath a running command. Transports
        # are built and closed outside the pool lock so one slow handshake or
        # teardown never stalls other scopes.
        deadline = self.clock() + 120
        stale = None
        with self.lock:
            while True:
                entry = self.entries.get(scope)
                if entry is None or not entry["busy"]:
                    break
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise TimeoutError("Remote connection is busy; command was not sent")
                self.changed.wait(min(remaining, 5))
            self._start()
            if entry is not None and (
                entry["fingerprint"] != fingerprint
                or getattr(entry["resource"], "closed", False)
                or (hasattr(entry["resource"], "healthy") and not entry["resource"].healthy())
            ):
                stale = self._evict(scope)
                entry = None
            if entry is None:
                if len(self.entries) >= self.capacity:
                    candidates = [(e["used"], k) for k, e in self.entries.items() if not e["busy"]]
                    if not candidates:
                        raise RuntimeError("Remote session capacity reached; try again later")
                    _, victim = min(candidates)
                    stale = self._evict(victim)
                # Reserve the slot while the transport is built outside the lock.
                entry = {
                    "resource": None,
                    "fingerprint": fingerprint,
                    "busy": True,
                    "used": self.clock(),
                    "idle": idle,
                }
                self.entries[scope] = entry
            entry["busy"] = True
        if stale is not None:
            stale.close()
        if entry["resource"] is None:
            try:
                entry["resource"] = factory()
            except BaseException:
                with self.lock:
                    if self.entries.get(scope) is entry:
                        self._evict(scope)
                raise
        try:
            yield entry["resource"]
        except BaseException:
            with self.lock:
                if self.entries.get(scope) is entry:
                    self._evict(scope)
            entry["resource"].close()
            raise
        finally:
            with self.lock:
                entry["busy"] = False
                entry["used"] = self.clock()
                self.changed.notify_all()

    def close(self):
        self.stopped.set()
        with self.lock:
            resources = [entry["resource"] for entry in self.entries.values() if entry["resource"] is not None]
            self.entries.clear()
            self.changed.notify_all()
        for resource in resources:
            resource.close()


POOL = RemotePool()
atexit.register(POOL.close)
