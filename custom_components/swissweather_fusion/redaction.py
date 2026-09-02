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
from typing import Any, Iterable

REDACTED_MARKER = "[REDACTED]"

# Substrings matched case-insensitively against key names. Deliberately
# broad — see module docstring for why over-redaction is the safer
# failure mode here.
SENSITIVE_KEY_SUBSTRINGS: tuple[str, ...] = (
    # Credentials
    "key", "secret", "token", "password", "auth",
    # v0.1.24 fix (P1-05): station entity IDs. The three
    # station_*_entity config values hold real Home Assistant entity IDs
    # such as sensor.bedroom_temperature — not credentials, but they
    # describe the layout and room names of someone's home, and appeared
    # verbatim in a file intended to be shared for troubleshooting.
    # Verified safe: no other config key in const.py contains "entity".
    "entity",
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


# v0.1.19 fix: this used to be exactly 3 hardcoded formats
# (str(value)/.4f/.2f), so any payload or error string carrying the
# configured coordinates at a different precision (3, 5, 6+ decimals) or
# via a totally different textual representation slipped through
# untouched — a real diagnostics-leak gap, since diagnostics are meant to
# be shared outside the system. Widened to every decimal precision from
# MIN through MAX_COORDINATE_DECIMALS, which comfortably covers what any
# sane API/JSON serializer would emit for a lat/lon float (beyond ~6
# decimal places is sub-meter precision, well past anything a real
# provider response uses).
#
# MIN is 2, not 0 — a 0- or 1-decimal rendering of a coordinate (e.g.
# "7" or "7.4") is short enough to plausibly collide with an unrelated,
# genuinely-innocuous single number elsewhere in a weather payload (a
# temperature, a percentage, an hour). That was caught directly by a
# test during development: the 0-decimal variant of a real longitude
# matched the leading "7" of an unrelated "7.44740001"-shaped value.
# Two decimals is specific enough to keep doing its job (it was already
# one of the 3 original formats) without that false-positive risk.
MIN_COORDINATE_DECIMALS = 2
MAX_COORDINATE_DECIMALS = 8


def _coordinate_string_variants(value: float) -> list[str]:
    """Likely string representations of one coordinate value, so the text
    substitution catches reasonable formatting variants without needing
    to parse every possible representation.

    Ordered longest-first (most decimal places, or the longest of the
    "natural" str()/repr() forms) so the substitution pass below can
    always try the most specific/precise match before a shorter one that
    might otherwise be a strict prefix of it — matching the shorter form
    first could leave a dangling, no-longer-parseable remainder (e.g.
    matching "46.9" inside "46.9480" and leaving "480" behind).
    """
    variants = {str(value), repr(value)}
    for decimals in range(MIN_COORDINATE_DECIMALS, MAX_COORDINATE_DECIMALS + 1):
        variants.add(f"{value:.{decimals}f}")
    return sorted(variants, key=len, reverse=True)


def redact_coordinate_strings(text: str, *, latitude: float, longitude: float) -> str:
    """Plain-text substitution of the exact configured coordinates in
    likely formats — catches cases like SRF's geolocationId, which is
    literally the string "46.9480,7.4474" stored under an innocuous key
    name ("id") that key-based redaction alone wouldn't flag.

    Each candidate substring is only replaced when it isn't itself
    directly adjacent to another digit OR a decimal point on either
    side — so redacting "46.94" doesn't also eat into an unrelated,
    longer number like "146.9480", and matching "7.4474" doesn't clip
    the front off a longer, unrelated "7.44740001" (adjacency to the
    trailing "." + more digits matters just as much as adjacency to a
    bare digit). Longest-variant-first ordering (see
    _coordinate_string_variants) means the most precise match is always
    attempted before a shorter one that could be its prefix.
    """
    for value in _coordinate_string_variants(latitude):
        pattern = r"(?<![\d.])" + re.escape(value) + r"(?![\d.])"
        text = re.sub(pattern, "[LAT_REDACTED]", text)
    for value in _coordinate_string_variants(longitude):
        pattern = r"(?<![\d.])" + re.escape(value) + r"(?![\d.])"
        text = re.sub(pattern, "[LON_REDACTED]", text)
    return text


def redact_secret_values(text: str, *, secrets: Iterable[str]) -> str:
    """Scrubs any exact occurrence of a known credential value out of
    free-form text — e.g. an exception message that embedded a full
    request URL with an API key as a query parameter.

    **v0.1.20**: added after finding that Open-Meteo's own client builds
    its request URL as `url += f"&apikey={api_key}"` (see
    clients/open_meteo.py) — a plain HTTP error from that request would
    put the real API key directly into `str(exception)`. Combined with
    diagnostics_events' `detail` field never actually being redacted
    despite diagnostics.py's own docstring claiming otherwise (found in
    the same investigation), this was a real, live credential-leak path
    for anyone who downloaded diagnostics while an Open-Meteo poll was
    failing with diagnostic logging enabled — not a hypothetical.

    Unlike redact_coordinate_strings, there's no false-positive/formatting
    ambiguity to worry about here: a configured secret is either present
    verbatim or it isn't, so a straightforward literal substring
    replacement is both safe and sufficient. Falsy secrets (None/empty
    string, e.g. an unconfigured optional API key) are skipped — an
    empty-string "secret" would otherwise match (and corrupt) everywhere.
    """
    for secret in secrets:
        if not secret:
            continue
        text = text.replace(secret, "[SECRET_REDACTED]")
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
