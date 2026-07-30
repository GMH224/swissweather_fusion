"""Redaction for diagnostic content, applied BEFORE anything enters the
DiagnosticsRecorder's buffer — not as a filter at export time.

Why redaction has to be more than "hide the config credentials": a real
captured SRF response included `alarm_region_name`, `district`, and a
`geolocation_names` entry with `name`/`province` — the *third-party API's
own response body* embeds identifying location data, not just this
project's own configuration. Since diagnostics content is meant to be
shared (with Claude, in a GitHub issue, wherever), this needs to catch
location data wherever it appears in arbitrary nested JSON from sources
this project doesn't fully control the shape of — not just redact a
known, fixed set of top-level config keys the way HA's own
`async_redact_data` helper does.

Two complementary passes:
  1. Key-name redaction: recursively blank the *value* of any key whose
     name matches a known-sensitive pattern (credentials, and location/
     identity fields observed in real responses), regardless of nesting.
  2. Coordinate-value redaction: a plain-text substitution of the exact
     configured latitude/longitude in likely string formats, since
     coordinates can appear embedded in a value under an innocuous key
     name too (SRF's own "geolocationId" is literally "lat,lon" as a
     string — key-name redaction alone wouldn't catch that).

Deliberately over-inclusive rather than precise: a key list broad enough
to occasionally redact something harmless is a much smaller cost than
missing a genuinely identifying field from a third-party API whose exact
shape isn't fully known in advance.
"""
from __future__ import annotations

import re
from typing import Any

REDACTED_MARKER = "[REDACTED]"

# Substrings matched case-insensitively against key names. Deliberately
# broad — see module docstring for why over-redaction is the safer
# failure mode here.
SENSITIVE_KEY_SUBSTRINGS: tuple[str, ...] = (
    # Credentials
    "key", "secret", "token", "password", "auth",
    # Coordinates and elevation
    "lat", "lon", "elevation", "altitude", "height",
    # Location/identity fields confirmed present in a real captured SRF
    # response (geolocation metadata, place names)
    "district", "region_name", "region_id", "default_name", "station_id",
    "city", "province", "municipality", "description_short",
    "description_long", "location_id", "geolocation", "name", "place",
    "address", "canton",
)


def redact_sensitive_keys(data: Any) -> Any:
    """Recursively walks a dict/list structure, replacing the value of
    any key matching SENSITIVE_KEY_SUBSTRINGS with a fixed marker.
    Non-dict/list values pass through unchanged; the recursion bottoms
    out naturally on plain values (str/int/float/bool/None).
    """
    if isinstance(data, dict):
        result: dict[str, Any] = {}
        for key, value in data.items():
            if any(s in key.lower() for s in SENSITIVE_KEY_SUBSTRINGS):
                result[key] = REDACTED_MARKER
            else:
                result[key] = redact_sensitive_keys(value)
        return result
    if isinstance(data, list):
        return [redact_sensitive_keys(item) for item in data]
    return data


def _coordinate_string_variants(value: float) -> list[str]:
    """A handful of likely string representations of one coordinate
    value, so the text substitution catches reasonable formatting
    variants without needing to parse every possible representation.
    """
    variants = {str(value), f"{value:.4f}", f"{value:.2f}"}
    return list(variants)


def redact_coordinate_strings(text: str, *, latitude: float, longitude: float) -> str:
    """Plain-text substitution of the exact configured coordinates in
    likely formats — catches cases like SRF's geolocationId, which is
    literally the string "46.9480,7.4474" stored under an innocuous key
    name ("id") that key-based redaction alone wouldn't flag.
    """
    for value in _coordinate_string_variants(latitude):
        text = re.sub(re.escape(value), "[LAT_REDACTED]", text)
    for value in _coordinate_string_variants(longitude):
        text = re.sub(re.escape(value), "[LON_REDACTED]", text)
    return text


def redact_diagnostic_payload(
    payload: Any, *, latitude: float, longitude: float
) -> Any:
    """The combined pass used everywhere diagnostic content is recorded:
    key-based redaction first (structural, catches most of it), then a
    text-level coordinate sweep on the JSON-serialized result (catches
    coordinates embedded in an otherwise-innocuous key). Returns a value
    of the same shape as the input (dict/list structure preserved) so
    it's still inspectable, not an opaque blob.
    """
    import json

    key_redacted = redact_sensitive_keys(payload)
    try:
        serialized = json.dumps(key_redacted, default=str)
    except (TypeError, ValueError):
        # If it's not cleanly JSON-serializable, fall back to repr() —
        # still redacted structurally above, just less pretty when
        # displayed.
        serialized = repr(key_redacted)
    coordinate_redacted = redact_coordinate_strings(
        serialized, latitude=latitude, longitude=longitude
    )
    try:
        return json.loads(coordinate_redacted)
    except (TypeError, ValueError):
        return coordinate_redacted
