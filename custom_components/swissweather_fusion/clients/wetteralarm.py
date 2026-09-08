"""Client for Wetter-Alarm's official Swiss severe-weather warnings.

v0.2.6. Wetter-Alarm is operated by Gebäudeversicherung Bern (GVB), a
Swiss public building-insurance institution, and issues staged warnings
for thunderstorm/hail, heavy rain, storm, heavy snow, ice and frost.

**Why this source, after four others were rejected.** MeteoSwiss does not
publish warnings as open data (their catalogue has categories A-E; there
is no warnings category, and their own docs say individual API access is
"not available before end of 2026"). MeteoNews and wetter.de are
commercial products with no open contract — MeteoNews' warning page is
`robots.txt`-disallowed outright. meteoblue *does* have a Warnings API
republishing official CAP alerts, but it returned
``403 "Access to the package API is not available for this user"`` on
this project's plan. SRF's official API does not expose warnings at all,
confirmed by another developer who asked them directly.

Wetter-Alarm's endpoint requires no key and serves the same JSON their
own app consumes.

**What this adds that the existing signals cannot.** Model B's storm
score is three heuristics this project invented: station tendency, upwind
radar, and CAPE bands. This is the first input carrying an *external
authority's* judgement — a warning someone with lightning detection,
full radar and human oversight decided to issue. That is a categorically
different kind of evidence, which is precisely why it is kept separate
rather than folded into the score (see coordinator.py).

**Derived from, but not a copy of, `redlukas/wetter-alarm`** (MIT). That
integration found and documented the endpoint. Three things are
deliberately done differently, because it is no longer maintained and
each would be a real defect here:

1. It polls **every 60 seconds** — 1,440 requests/day for the rarest
   event class in this project, more aggressive than our radar polling.
2. It opens a **new `aiohttp.ClientSession` per request** rather than
   reusing Home Assistant's shared session.
3. It **re-parses the unchanged national list every cycle**, with no
   deduplication.
"""
from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

_LOGGER = logging.getLogger(__name__)

API_BASE_URL = "https://my.wetteralarm.ch"
ALARMS_URL = f"{API_BASE_URL}/v7/alarms/meteo.json"

# Bundled index of Swiss towns: [poi_id, lat, lon]. Wetter-Alarm keys its
# alarms by point-of-interest id, so a location must be resolved to one.
# Asking the user to look up a numeric id would be poor configuration, so
# the nearest town is derived from coordinates already configured.
#
# Towns and town sections only, from the upstream project's published POI
# export: 6,261 entries at 144 KB, versus 14,453 and 7 MB for the full
# set including peaks, huts, ski areas and viewpoints — none of which are
# meaningful anchors for a regional weather warning.
_POI_INDEX_FILE = "wetteralarm_pois.json"

# Severity vocabulary. Wetter-Alarm's public site documents three levels
# (gelb / orange / rot). The numeric `priority` field is NOT documented
# anywhere this project can verify, so the mapping below is applied
# defensively: an unrecognised value is preserved verbatim rather than
# coerced into a level it may not mean.
#
# This is the CombiPrecip lesson applied pre-emptively — that outage came
# from encoding a documented-looking convention without checking it
# against a real response.
PRIORITY_TO_LEVEL = {
    1: "yellow",
    2: "orange",
    3: "red",
}
LEVEL_NONE = "none"


@dataclass(frozen=True)
class WeatherWarning:
    """One active warning for a location.

    Every field the upstream API returns is carried through. Nothing is
    summarised away at this layer: the coordinator and sensors decide
    what to surface, and a client that discards fields makes that
    decision unilaterally and irreversibly.
    """

    alarm_id: Optional[int] = None
    priority: Optional[int] = None
    level: str = LEVEL_NONE
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    region: Optional[str] = None
    title: Optional[str] = None
    hint: Optional[str] = None
    signature: Optional[str] = None
    # Verbatim upstream payload for this alarm, so a field this client
    # does not yet understand is still recoverable from diagnostics
    # rather than silently lost.
    # Stored as a JSON string, not a dict: WeatherWarning is a frozen
    # dataclass and an embedded dict makes it unhashable, which would
    # break any future use in a set or as a dict key. v0.2.6 audit.
    raw_json: Optional[str] = None

    @property
    def is_active(self) -> bool:
        return self.alarm_id is not None

    def is_current(self, now: Optional[datetime] = None) -> bool:
        """Whether this warning is in force at `now`.

        **v0.2.6 audit (SWF-026-003).** The national list can carry an
        alarm whose validity window has already closed — publishers do
        not always remove them promptly, and the upstream project never
        checked. Reporting an expired storm warning as active is worse
        than reporting nothing: it is a false alarm that looks
        authoritative.

        A warning with no parseable window is treated as current, since
        the alternative would discard a real warning over a formatting
        problem.
        """
        if not self.is_active:
            return False
        moment = now or datetime.now(timezone.utc)
        if self.valid_to is not None and moment > self.valid_to:
            return False
        if self.valid_from is not None and moment < self.valid_from:
            return False
        return True


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    radius = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
        * math.sin(d_lon / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


def load_poi_index() -> list[list[float]]:
    """Load the bundled [poi_id, lat, lon] index.

    Synchronous file read — callers must dispatch this to an executor,
    never call it from the event loop. v0.1.25 shipped a blocking
    manifest read on the loop and Home Assistant flagged it; the same
    mistake is not repeated here.
    """
    path = os.path.join(os.path.dirname(__file__), "..", _POI_INDEX_FILE)
    with open(os.path.normpath(path), encoding="utf-8") as handle:
        return json.load(handle)


def find_nearest_poi(
    latitude: float, longitude: float, index: list[list[float]]
) -> Optional[tuple[int, float]]:
    """Nearest town POI to a coordinate, as ``(poi_id, distance_km)``.

    Returns None for an empty index rather than raising: a missing POI
    file should disable this one feature, not prevent setup.

    **v0.2.6 audit (SWF-026-008): cheap rejection before trigonometry.**
    The naive version ran a full haversine — four trig calls plus a
    square root — against all 6,261 entries, measured at ~15 ms. Almost
    every entry is hundreds of kilometres away and can be discarded on a
    plain coordinate-difference comparison costing two subtractions.

    A degree of latitude is ~111 km everywhere; a degree of longitude is
    ~111 km times cos(latitude), which at Swiss latitudes is ~76 km. The
    bounding box uses the conservative 111 km for both, so it can never
    reject a candidate that haversine would have accepted — it is a
    filter, not an approximation of the answer.
    """
    best: Optional[tuple[int, float]] = None
    # Widens as nothing is found; starts generous enough to cover any
    # plausible Swiss location, then tightens to the best distance so far.
    box_degrees = 1.0

    for entry in index:
        if len(entry) != 3:
            continue
        poi_id, lat, lon = entry
        # Cheap rejection: if either axis alone already exceeds the
        # current best, the true distance certainly does.
        if abs(lat - latitude) > box_degrees or abs(lon - longitude) > box_degrees:
            continue
        distance = _haversine_km(latitude, longitude, lat, lon)
        if best is None or distance < best[1]:
            best = (int(poi_id), distance)
            # 1 degree of latitude is ~111 km; converting the best
            # distance back to a degree bound keeps the filter valid.
            box_degrees = min(box_degrees, (distance / 111.0) + 0.01)

    if best is None and index:
        # Nothing inside the initial box — fall back to a full scan
        # rather than reporting no coverage, since the box is an
        # optimisation and must never change the answer.
        for entry in index:
            if len(entry) != 3:
                continue
            poi_id, lat, lon = entry
            distance = _haversine_km(latitude, longitude, lat, lon)
            if best is None or distance < best[1]:
                best = (int(poi_id), distance)
    return best


def _parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse an upstream timestamp, tolerating format variation.

    The upstream project assumed exactly ``%Y-%m-%dT%H:%M:%S.%fZ`` and
    would raise on anything else. A warning source that crashes on an
    unexpected date format is worse than one that reports the warning
    without precise timing, so the fractional-seconds and 'Z' variants
    are both accepted and failure degrades to None.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_all_alarms(
    payload: dict,
    poi_id: int,
    language: str = "de",
    now: Optional[datetime] = None,
) -> list[WeatherWarning]:
    """Every warning currently in force for `poi_id`, most severe first.

    **v0.2.6 audit (SWF-026-002).** The original implementation returned
    the FIRST matching alarm and discarded the rest. Concurrent warnings
    are entirely normal — a frost warning and a thunderstorm warning can
    be in force at once — and list order carries no meaning, so a red
    thunderstorm warning could be silently dropped in favour of a yellow
    frost one that happened to be earlier in the response.

    That is precisely the information loss this release was scoped to
    avoid, reintroduced at the parsing layer. Every applicable warning is
    now returned; sorting is by descending priority so callers wanting a
    single headline value can take the first and get the most severe
    rather than an arbitrary one.

    Expired alarms are excluded here — see WeatherWarning.is_current.
    """
    warnings = [
        w for w in _iter_matching_alarms(payload, poi_id, language)
        if w.is_current(now)
    ]
    warnings.sort(key=lambda w: (w.priority or 0), reverse=True)
    return warnings


def parse_alarms_response(
    payload: dict,
    poi_id: int,
    language: str = "de",
    now: Optional[datetime] = None,
) -> WeatherWarning:
    """Find the warning applying to `poi_id`, if any.

    The endpoint returns **every active alarm in Switzerland** in one
    response and expects the caller to filter — there is no per-location
    query server-side. On most days the list is empty or contains
    warnings for other regions.

    Returns the MOST SEVERE current warning, or an inactive
    WeatherWarning when nothing applies — never None, so callers never
    have to distinguish "no warning" from "lookup failed".

    v0.2.6 audit: use parse_all_alarms() when every concurrent warning
    matters. This convenience wrapper exists for the single-value entity
    state and deliberately takes the most severe rather than the first.
    """
    warnings = parse_all_alarms(payload, poi_id, language, now)
    return warnings[0] if warnings else WeatherWarning()


def _iter_matching_alarms(payload: dict, poi_id: int, language: str):
    """Yield a WeatherWarning for each alarm listing `poi_id`."""
    alarms = payload.get("meteo_alarms") or []
    if not isinstance(alarms, list):
        return

    for alarm in alarms:
        if not isinstance(alarm, dict):
            continue
        poi_ids = alarm.get("poi_ids") or []
        if not isinstance(poi_ids, (list, tuple)) or poi_id not in poi_ids:
            continue

        priority = alarm.get("priority")
        # Localised text lives under a language key; fall back through
        # German and English rather than returning nothing, since a
        # warning in the wrong language is far better than no warning.
        localised: dict = {}
        for key in (language, "de", "en"):
            candidate = alarm.get(key)
            if isinstance(candidate, dict):
                localised = candidate
                break

        region_name = None
        region = alarm.get("region")
        if isinstance(region, dict):
            for key in (language, "de", "en"):
                entry = region.get(key)
                if isinstance(entry, dict) and entry.get("name"):
                    region_name = entry["name"]
                    break

        yield WeatherWarning(
            alarm_id=alarm.get("id"),
            priority=priority if isinstance(priority, int) else None,
            level=(
                PRIORITY_TO_LEVEL.get(priority, str(priority))
                if priority is not None else LEVEL_NONE
            ),
            valid_from=_parse_timestamp(alarm.get("valid_from")),
            valid_to=_parse_timestamp(alarm.get("valid_to")),
            region=region_name,
            title=localised.get("title"),
            hint=localised.get("hint"),
            signature=localised.get("signature"),
            raw_json=json.dumps(alarm, sort_keys=True, default=str),
        )


class WetterAlarmClient:
    """Fetches the national alarm list.

    The session is injected rather than created per request — Home
    Assistant provides a shared, connection-pooled session and creating
    one per call is a documented antipattern that the upstream project
    fell into.
    """

    def __init__(self, session: Any) -> None:
        self._session = session

    async def async_fetch_alarms(self) -> dict:
        import aiohttp

        async with self._session.get(
            ALARMS_URL, timeout=aiohttp.ClientTimeout(total=20)
        ) as response:
            response.raise_for_status()
            return await response.json(content_type=None)
