"""Per-source health tracking: success/failure state, error classification
(auth vs. data), and consecutive-failure counts.

Pure logic, no Home Assistant imports, so it's directly unit-testable —
see tests/test_health.py. Each coordinator in coordinator.py owns one
SourceHealth instance and updates it via record_success/record_error
around its actual fetch call; sensor.py reads the result.

Auth vs. data matters because the right response differs: a data error
(timeout, malformed response) should just retry on the normal schedule
(handled by graceful degradation elsewhere); an auth error (expired key,
401/403) won't fix itself by retrying and should point the user at
re-entering credentials instead.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# HTTP status codes that mean "the credential itself is the problem", not
# "something transient went wrong". Kept as a simple set rather than
# depending on aiohttp's exception types directly, so this module has zero
# framework dependencies.
AUTH_ERROR_STATUS_CODES = frozenset({401, 403})


def classify_exception(err: BaseException) -> str:
    """Returns 'auth' or 'data'.

    Works off a `.status` attribute if present (this is how
    aiohttp.ClientResponseError exposes the HTTP status code — checked by
    attribute rather than importing aiohttp, so this stays a pure,
    dependency-free classifier). Anything without a recognizable auth
    status code is treated as a data error, which is the safer default:
    misclassifying a real auth problem as a data error costs you one
    unnecessary retry cycle before someone notices via consecutive
    failures; misclassifying a data problem as an auth problem falsely
    points the user at re-entering credentials that were never wrong.
    """
    status = getattr(err, "status", None)
    if status in AUTH_ERROR_STATUS_CODES:
        return "auth"
    return "data"


@dataclass
class SourceHealth:
    """Mutable per-source state, updated in place by the owning
    coordinator. One instance per source, not shared.
    """

    last_success_time: Optional[datetime] = None
    last_poll_duration_ms: Optional[float] = None
    last_data_error: Optional[str] = None
    last_data_error_time: Optional[datetime] = None
    last_auth_error: Optional[str] = None
    last_auth_error_time: Optional[datetime] = None
    consecutive_failures: int = 0
    # v0.2.1 (SWF-P2-008): when this health record was created, i.e. when
    # the integration started tracking the source. Used to distinguish
    # "has not run yet" from "is not working" for sources that poll far
    # less often than every cycle.
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def record_success(self, *, duration_ms: float) -> None:
        self.last_success_time = datetime.now(timezone.utc)
        self.last_poll_duration_ms = duration_ms
        self.consecutive_failures = 0

    def record_error(self, err: BaseException, *, duration_ms: Optional[float] = None) -> str:
        """Classifies and records the error, returns the classification
        ('auth' or 'data') so the caller can decide on cooldown/reauth
        behavior without re-deriving it.
        """
        kind = classify_exception(err)
        now = datetime.now(timezone.utc)
        if kind == "auth":
            self.last_auth_error = str(err)
            self.last_auth_error_time = now
        else:
            self.last_data_error = str(err)
            self.last_data_error_time = now
        self.consecutive_failures += 1
        if duration_ms is not None:
            self.last_poll_duration_ms = duration_ms
        return kind
