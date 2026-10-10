"""In-process event bus feeding SSE, the audit trail and diagnostics."""
from __future__ import annotations

import json
import queue
import threading
from collections import deque
from dataclasses import dataclass, field

from .state import now_ms


@dataclass(frozen=True)
class Event:
    seq: int
    ts_ms: int
    message_id: str   # LeakDetected | StateChange | CommunicationLost | ...
    severity: str     # OK | Warning | Critical
    origin: str       # e.g. /redfish/v1/LeakDetectors/leak-b
    message: str
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        import time

        return {
            "sequence": self.seq,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(self.ts_ms / 1000))
            + f".{self.ts_ms % 1000:03d}Z",
            "timestamp_ms": self.ts_ms,
            "messageId": self.message_id,
            "severity": self.severity,
            "originOfCondition": self.origin,
            "message": self.message,
            "data": self.data,
        }


class EventBus:
    """Fan-out event bus with drop-oldest subscriber queues and a recent ring."""

    def __init__(self, recent_len: int = 500, on_publish=None) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._recent: deque[Event] = deque(maxlen=recent_len)
        self._subscribers: list[queue.Queue] = []
        self._on_publish = on_publish  # optional hook (e.g. metrics counter)

    def publish(self, message_id: str, severity: str, origin: str = "", message: str = "", **data) -> Event:
        with self._lock:
            self._seq += 1
            ev = Event(
                seq=self._seq,
                ts_ms=now_ms(),
                message_id=message_id,
                severity=severity,
                origin=origin,
                message=message,
                data=data,
            )
            self._recent.append(ev)
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(ev)
            except queue.Full:
                try:
                    q.get_nowait()  # drop oldest, keep newest
                    q.put_nowait(ev)
                except (queue.Empty, queue.Full):
                    pass
        if self._on_publish is not None:
            try:
                self._on_publish(ev)
            except Exception:  # pragma: no cover
                pass
        return ev

    def subscribe(self, maxsize: int = 1000) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=maxsize)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def recent(self, n: int = 100) -> list[Event]:
        with self._lock:
            return list(self._recent)[-n:]

    @staticmethod
    def sse_format(ev: Event) -> str:
        payload = json.dumps(ev.to_dict(), separators=(",", ":"))
        return f"event: {ev.message_id}\nid: {ev.seq}\ndata: {payload}\n\n"
