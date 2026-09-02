"""Deterministic content fingerprinting, shared across forecast clients.

v0.1.23 fix (audit findings L-04, L-05, L-06): three separate defects
turned out to be the same missing piece — none of Open-Meteo, Meteoblue,
or SRF had a durable way to recognize "this is the same upstream forecast
run I already stored" across a Home Assistant restart:

  - L-06: Open-Meteo's dedup fingerprint existed but lived only in a
    coordinator instance dict, reset to empty on every restart.
  - L-05: Meteoblue had no dedup fingerprint at all — every poll (scheduled
    or bonus) was inserted unconditionally.
  - L-04: SRF's forecast_snapshots rows carry no run identity of any kind,
    so repeated polls of an unchanged upstream forecast create repeated
    training rows.

Rather than inventing three different provider-specific identity schemes
(each provider's raw response shape has already proven to be a moving
target — see DEVELOPER.md's SRF history), every coordinator now fingerprints
its own already-*parsed* points instead of raw response bytes. This is
simpler, uniform across sources, and more robust: it's based on what the
data actually says (variable, valid_at, value) rather than incidental
response metadata (headers, field ordering, an unrelated timestamp) that
could change without the forecast itself changing.

The fingerprint is persisted via SwissWeatherDB.get/set_provider_run_fingerprint
(schema_meta-backed), not just held in memory, which is what makes the fix
survive a restart — see each coordinator's use of it in coordinator.py.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Protocol


class _HasVariableValidAtValue(Protocol):
    variable: str
    value: Any

    @property
    def valid_at(self) -> Any: ...


def compute_content_fingerprint(data: Any) -> str:
    """Generic deterministic hash of any JSON-serializable structure.
    Sorted keys and str-coerced fallback values make this stable across
    dict key ordering and datetime/other non-JSON-native types.
    """
    serialized = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def fingerprint_points(points: Iterable[_HasVariableValidAtValue]) -> str:
    """Content fingerprint for a list of parsed forecast points.

    Works uniformly for any point type with .variable, .valid_at, and
    .value attributes (ForecastPoint, MeteoblueForecastPoint,
    SrfForecastPoint all satisfy this). Points are sorted before hashing
    so the fingerprint doesn't depend on the order a provider happened to
    return them in — only on the actual (variable, time, value) content.
    """
    normalized = sorted(
        (p.variable, p.valid_at.isoformat(), p.value) for p in points
    )
    return compute_content_fingerprint(normalized)
