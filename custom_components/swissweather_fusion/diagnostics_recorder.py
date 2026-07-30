"""Toggleable diagnostic event recorder.

**Why this exists**: repeated debugging cycles across v0.1.1-v0.1.8 all
followed the same shape — a problem shows up, the person screenshots a
few sensor states from their phone, sends them over, and real progress
only happens once an actual HA log file gets uploaded. That's slow and
error-prone (the wrong log window, truncated diagnostic messages, no way
to see a raw API response in full). This closes that loop: when enabled,
every coordinator records structured events here (poll attempts,
successes, failures with exception detail, and — critically — the full,
untruncated raw response body for the specific "parsed successfully but
found zero usable data" cases that have caused most of SRF's debugging
back-and-forth) into a bounded in-memory buffer. `diagnostics.py` exposes
this buffer through Home Assistant's own built-in "Download Diagnostics"
button, so getting real evidence is one click, not a request-and-wait
round trip.

Deliberately **in-memory only, not persisted to the SQLite database** —
survives until next restart/reload, not across one. That's a real
limitation, not hidden: the intended workflow is "enable logging, let the
problem happen, download diagnostics before restarting," not "look back
at what happened last week." Persisting this would mean a new table, a
purge policy, and genuine storage growth for a feature whose whole
purpose is short-lived, active debugging — not worth that cost given the
actual use case.

Default is OFF (see const.py) — this only accumulates data when someone
has explicitly asked to watch closely, not as a standing background cost.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass(frozen=True)
class DiagnosticEvent:
    ts: str
    source: str
    event_type: str  # 'poll_start' | 'poll_success' | 'poll_failure' | 'raw_response'
    detail: str
    extra: dict[str, Any] = field(default_factory=dict)


class DiagnosticsRecorder:
    """One shared instance per config entry, created in __init__.py and
    passed to every coordinator. Cheap to call even when disabled (a
    single boolean check) — coordinators don't need their own "is this
    enabled" branching logic before calling record().
    """

    def __init__(self, *, max_events: int = 1000) -> None:
        self._enabled = False
        self._events: deque[DiagnosticEvent] = deque(maxlen=max_events)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled

    def record(
        self,
        *,
        source: str,
        event_type: str,
        detail: str,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        if not self._enabled:
            return
        self._events.append(
            DiagnosticEvent(
                ts=datetime.now(timezone.utc).isoformat(),
                source=source,
                event_type=event_type,
                detail=detail,
                extra=extra or {},
            )
        )

    def get_events(self) -> list[dict[str, Any]]:
        """Returns plain dicts (not the dataclass) — this is what feeds
        directly into diagnostics.py's JSON-serializable output.
        """
        return [
            {"ts": e.ts, "source": e.source, "event_type": e.event_type,
             "detail": e.detail, "extra": e.extra}
            for e in self._events
        ]

    def clear(self) -> None:
        self._events.clear()
