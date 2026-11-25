"""Nonblocking, single-flight AI health snapshots; no credential or body logging."""
import threading
import time
from datetime import datetime, timezone

class BackgroundHealthProbe:
    def __init__(self, probe, interval=30, clock=time.monotonic):
        self._probe, self._interval, self._clock = probe, interval, clock
        self._lock = threading.Lock()
        self._running = False
        self._started = float("-inf")
        self._result = {"reachable": None, "checked_at": None, "duration_ms": None}

    def snapshot(self):
        start = False
        with self._lock:
            if not self._running and self._clock() - self._started >= self._interval:
                self._running = True
                self._started = self._clock()
                start = True
            result = dict(self._result)
            result["state"] = "checking" if self._running else ("ready" if result["reachable"] else "unavailable")
        if start:
            try:
                threading.Thread(target=self._check, name="quillbox-ai-health", daemon=True).start()
            except Exception:
                with self._lock:
                    self._running = False
                    self._result = {"reachable": False, "checked_at": None, "duration_ms": None}
                result.update(reachable=False, state="unavailable")
        return result

    def _check(self):
        began = self._clock()
        try:
            reachable = bool(self._probe())
        except Exception:
            reachable = False
        result = {"reachable": reachable,
                  "checked_at": datetime.now(timezone.utc).isoformat(),
                  "duration_ms": round(max(0, self._clock() - began) * 1000)}
        with self._lock:
            self._result = result
            self._running = False
