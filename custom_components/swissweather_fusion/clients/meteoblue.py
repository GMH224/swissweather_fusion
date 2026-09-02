"""meteoblue Free Weather API client.

Confirmed during planning via a live test call: 8,000 credits per call,
flat regardless of package requested (basic-1h alone costs the same as
basic-1h_basic-day combined) — so both packages are always requested, the
daily rollup is free once you're paying for the hourly one anyway.

Polling is seasonal, not a fixed interval (see const.py and DEVELOPER.md):
  - Mar-Oct: 12:00/16:00/20:00 local time — climatology-driven, matches
    when Swiss storm formation actually peaks (~17:00 CEST).
  - Nov-Feb: 06:00/12:00/18:00 local time — commute-relevant checkpoints
    for ice/snow risk, not climatology; winter risk develops gradually and
    is already covered hours ahead by the routine CH1/CH2/D2 blend.
Both schedules are 3 calls/day — this is a timing refinement, not a
credit-budget change (~8.76M/year either way, against the 10M/year cap).

One bonus call per storm scenario is allowed via the cross-model trigger
(Model B), overriding the scheduled times — see model_b.py and
DEVELOPER.md. That allowance is tracked here per calendar day so it can't
silently exceed the credit budget during an unusually active stretch.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional

from ..const import (
    METEOBLUE_MAX_BONUS_CALLS_PER_EVENT,
    METEOBLUE_SUMMER_HOURS_LOCAL,
    METEOBLUE_SUMMER_MONTHS,
    METEOBLUE_WINTER_HOURS_LOCAL,
)
from ..fingerprint import fingerprint_points

BASE_URL = "https://my.meteoblue.com/packages/basic-1h_basic-day"


def build_forecast_url(*, latitude: float, longitude: float, api_key: str) -> str:
    # v0.1.23 fix (own-review finding, not in the external ICS audit):
    # explicit &tz=UTC. Per meteoblue's own API documentation (data
    # packages and images doc, §3.6-3.7): if the &tz= parameter is
    # omitted, the API looks up a timezone from the request coordinates
    # and returns human-readable timestamps (the "Y-M-D hh:mm" style this
    # client parses from data_1h.time) in THAT LOCAL ZONE, not UTC — only
    # the separate numeric-epoch time formats are UTC "by definition".
    # This client's parse_forecast_response() was tagging every parsed
    # timestamp with tzinfo=timezone.utc regardless, silently mislabeling
    # what was actually Europe/Zurich local time as UTC for a Swiss
    # deployment. meteoblue's own docs recommend exactly this fix
    # verbatim: "For autonomous systems we recommend to use UTC" for the
    # tz parameter. Matches the explicit &timezone=UTC Open-Meteo's client
    # already sends for the identical reason (see open_meteo.py).
    return f"{BASE_URL}?lat={latitude}&lon={longitude}&apikey={api_key}&tz=UTC"


def scheduled_hours_for_month(month: int) -> tuple[int, ...]:
    """Which local hours to poll at, given the calendar month.

    Deliberately a different, unrelated concept from Model A's 4-way
    meteorological `season` bucket (DJF/MAM/JJA/SON) — this is a 2-way
    Mar-Oct/Nov-Feb split used only for scheduling meteoblue's polls. Named
    distinctly (this function, not "season") to avoid the two being
    conflated in code, per the explicit warning in the plan doc.
    """
    if month in METEOBLUE_SUMMER_MONTHS:
        return METEOBLUE_SUMMER_HOURS_LOCAL
    return METEOBLUE_WINTER_HOURS_LOCAL


def is_scheduled_poll_time(*, local_dt: datetime) -> bool:
    """True if local_dt's hour matches this month's schedule.

    **v0.1.19 fix**: this used to also require `local_dt.minute == 0`, on
    the assumption the coordinator would be checked exactly on the hour.
    In reality the coordinator (see MeteoblueCoordinator) checks every 5
    minutes via `async_track_time_interval`, which is a fixed interval
    *relative to whenever the coordinator was created* (HA startup or
    integration reload), not wall-clock aligned. Unless that moment
    happened to fall on a multiple of 5 minutes that also lands exactly on
    :00, the checks would land on minutes like :17/:22/:27/... forever and
    `minute == 0` would never be true — so scheduled meteoblue calls could
    silently never fire on most restarts. Confirmed via code trace, no
    live reproduction needed: the bug is in the gate condition itself.
    Now a window check instead: true for the whole scheduled hour, with
    `should_fire_scheduled_call` below (via `last_scheduled_call_hour`)
    responsible for making sure that only fires once per (date, hour)
    rather than on every 5-minute check within the hour.
    """
    return local_dt.hour in scheduled_hours_for_month(local_dt.month)


def should_fire_scheduled_call(
    *, local_dt: datetime, last_scheduled_call_hour: Optional[datetime]
) -> bool:
    """True if local_dt is a scheduled slot AND a call hasn't already
    fired for this same (date, hour) — extracted from the coordinator's
    update loop specifically so this logic (including DST edge cases) is
    directly unit-testable without needing Home Assistant installed. Pure
    behavior-preserving extraction, not a change — same checks the
    coordinator used to do inline.

    Deliberately tolerant of the two DST edge cases rather than needing to
    handle them with perfect precision: during a "spring forward" gap, the
    skipped local hour simply never occurs, so no scheduled slot silently
    gets skipped in a way that matters. During a "fall back" repeat, if a
    scheduled hour happens to repeat, this compares only (date, hour), so
    a call that already fired in the first occurrence of that hour will
    correctly NOT fire again in the second occurrence — a missed second
    attempt is an acceptable outcome (matches the wider project's
    "gaps are fine, corruption/crashes are not" tolerance for this), not a
    bug to engineer around further.
    """
    if not is_scheduled_poll_time(local_dt=local_dt):
        return False
    if (
        last_scheduled_call_hour is not None
        and last_scheduled_call_hour.hour == local_dt.hour
        and last_scheduled_call_hour.date() == local_dt.date()
    ):
        return False
    return True


@dataclass
class BonusCallTracker:
    """Tracks a per-day bonus-call allowance from the cross-model trigger.
    Per calendar day, not per event duration — a new storm scenario on the
    same day does not reset this; that's a deliberate conservative choice
    to protect the annual credit budget, worth revisiting once real storm
    clustering is observed (see DEVELOPER.md).

    **v0.1.17 fix**: `max_calls_per_day` used to be hardcoded to
    `METEOBLUE_MAX_BONUS_CALLS_PER_EVENT`, meaning this class could only
    ever be used for meteoblue. Confirmed in production: Meteonomiqs's own
    bonus-call path had no equivalent per-day cap at all — only the
    overall annual budget check — so if the cross-model trigger fired
    repeatedly (a separate, still-being-investigated question), meteoblue
    was protected and Meteonomiqs wasn't, burning through its 1000-
    calls/year budget in days instead of months. Now parameterized so the
    same tracker (and the same tested logic) protects both.
    """

    _calls_used_by_date: dict[date, int]

    def __init__(self, max_calls_per_day: int = METEOBLUE_MAX_BONUS_CALLS_PER_EVENT) -> None:
        self._calls_used_by_date = {}
        self._max_calls_per_day = max_calls_per_day

    def can_use_bonus_call(self, *, today: date) -> bool:
        used = self._calls_used_by_date.get(today, 0)
        return used < self._max_calls_per_day

    def record_bonus_call_used(self, *, today: date) -> None:
        self._calls_used_by_date[today] = self._calls_used_by_date.get(today, 0) + 1

    def try_use_bonus_call(self, *, today: date) -> bool:
        """v0.1.15 fix: combines can_use_bonus_call + record_bonus_call_used
        into one atomic check-and-record, closing a TOCTOU race an
        independent review flagged — the two separate calls (with an
        await for the actual HTTP request in between, in the caller) left
        a window where two concurrent triggers could both pass the check
        before either recorded usage, allowing more bonus calls than the
        allowance intends. Low practical likelihood given the coordinator
        calling this already has its own overlap protection, but a cheap,
        correct fix either way. Prefer this over the two separate methods
        above (kept for compatibility, not removed).
        """
        if not self.can_use_bonus_call(today=today):
            return False
        self.record_bonus_call_used(today=today)
        return True

    def prune_old_dates(self, *, keep_since: date) -> None:
        self._calls_used_by_date = {
            d: c for d, c in self._calls_used_by_date.items() if d >= keep_since
        }

    def to_state(self) -> dict:
        """v0.1.23 fix (L-08): serializes this tracker's usage-by-date map
        so the coordinator can persist it via
        SwissWeatherDB.set_bonus_call_tracker_state(). Previously this
        state existed only as a plain instance attribute, reset to empty
        on every Home Assistant restart/reload — meaning a restart could
        forget same-day bonus usage already spent, letting the daily
        allowance be exceeded across a reload."""
        return {d.isoformat(): c for d, c in self._calls_used_by_date.items()}

    @classmethod
    def from_state(
        cls, state: Optional[dict], *, max_calls_per_day: int = METEOBLUE_MAX_BONUS_CALLS_PER_EVENT
    ) -> "BonusCallTracker":
        """Inverse of to_state(). A missing/empty state (e.g. first-ever
        start, or nothing persisted yet) behaves exactly like the old
        always-empty default — this only adds durability, it doesn't
        change first-run behavior."""
        tracker = cls(max_calls_per_day=max_calls_per_day)
        if state:
            tracker._calls_used_by_date = {
                date.fromisoformat(d): c for d, c in state.items()
            }
        return tracker


# Fields confirmed present via a live test call during planning:
# relativehumidity, sealevelpressure, temperature, precipitation, windspeed,
# felttemperature, uvindex, predictability, rainspot (format undocumented).
# v0.2.2 (SWF-021-015): meteoblue's own hourly response carries several
# of the parameters v0.2.0 added, and they were being discarded. These
# are fields the existing request already returns — no additional API
# call and no extra credit against the 8,000-per-call budget.
_FIELD_MAP = {
    "temperature": "temperature",
    "relativehumidity": "humidity",
    "sealevelpressure": "pressure",
    "precipitation": "precip",
    "windspeed": "wind_speed",
    # v0.2.2 (SWF-021-015): already present in the same response.
    "snowfraction": "srf_snowfraction",
    "precipitation_probability": "precip_probability",
    "windgust": "wind_gust_speed",
    "winddirection": "wind_bearing",
    "dewpointtemperature": "dew_point",
    "felttemperature": "apparent_temperature",
    "totalcloudcover": "cloud_coverage",
    "uvindex": "uv_index",
}


@dataclass(frozen=True)
class MeteoblueForecastPoint:
    variable: str
    valid_at: datetime
    value: Optional[float]


@dataclass(frozen=True)
class ParsedMeteoblueForecast:
    issued_at: datetime
    grid_elevation_m: Optional[float]
    points: list[MeteoblueForecastPoint]
    predictability: Optional[list[float]]  # confidence score, hourly — bonus field
    # v0.1.23 fix (L-05): content fingerprint of `points` (see
    # fingerprint.py), letting the coordinator recognize and skip an
    # unchanged upstream model run instead of unconditionally inserting a
    # fresh set of forecast_snapshots rows on every scheduled/bonus poll —
    # meteoblue previously had no dedup mechanism at all, unlike
    # Open-Meteo (L-06, fixed alongside this).
    run_fingerprint: Optional[str] = None
    # v0.1.24 fix (P1-24): names of any value arrays whose length did not
    # match the "time" axis. Mirrors ParsedForecast.array_length_mismatches
    # in open_meteo.py, added there by the v0.1.19 fix. Empty tuple is the
    # normal case.
    array_length_mismatches: tuple[str, ...] = ()


def _parse_utc(value: str, naive_format: str | None = None) -> datetime:
    """Parse a provider timestamp to an aware UTC datetime.

    **v0.1.24 fix (P1-25)**: both this client and open_meteo.py used

        datetime.fromisoformat(s).replace(tzinfo=timezone.utc)

    which is correct ONLY for naive input. On already-aware input,
    .replace() overwrites the tzinfo label without converting the
    underlying instant, so "14:00+02:00" silently becomes "14:00+00:00"
    — the same wall-clock reading, two hours off the actual moment.

    Not believed to be actively wrong today, since both clients now
    explicitly request UTC output (&tz=UTC / &timezone=UTC) and
    meteoblue's hourly times use a naive "%Y-%m-%d %H:%M" format that
    cannot carry an offset. But a provider adding an offset to its
    output would corrupt every stored valid_at with no error, and the
    correct form costs one branch.
    """
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        if naive_format is None:
            raise
        parsed = datetime.strptime(value, naive_format)
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc)
    return parsed.replace(tzinfo=timezone.utc)


def parse_forecast_response(payload: dict[str, Any]) -> ParsedMeteoblueForecast:
    metadata = payload.get("metadata", {})
    issued_at_str = metadata.get("modelrun_updatetime_utc")
    # v0.1.24 fix (P1-10): whether the provider gave us a REAL model-run
    # identifier, as opposed to us falling back to "now", determines
    # whether the fingerprint below can use run identity at all.
    has_real_run_identity = bool(issued_at_str)
    issued_at = (
        _parse_utc(issued_at_str) if issued_at_str else datetime.now(timezone.utc)
    )
    grid_elevation_m = metadata.get("height")

    data_1h = payload.get("data_1h", {})
    times = data_1h.get("time", [])

    points: list[MeteoblueForecastPoint] = []
    mismatches: list[str] = []
    for mb_key, internal_name in _FIELD_MAP.items():
        values = data_1h.get(mb_key)
        if values is None:
            continue
        # v0.1.24 fix (P1-24): zip() silently truncates to the shorter
        # sequence, so a response whose value array did not match the
        # "time" axis lost the tail with no trace at all. Open-Meteo has
        # tracked this since the v0.1.19 fix; meteoblue had no
        # equivalent. Recorded rather than raised — a partial forecast is
        # still worth storing, but the caller needs to know it happened.
        if len(values) != len(times):
            mismatches.append(
                f"{mb_key}: {len(values)} values vs {len(times)} times"
            )
        for t_str, value in zip(times, values):
            valid_at = _parse_utc(t_str, naive_format="%Y-%m-%d %H:%M")
            points.append(
                MeteoblueForecastPoint(variable=internal_name, valid_at=valid_at, value=value)
            )

    # v0.1.24 fix (P1-10): fingerprint_points() hashes only the sorted
    # (variable, valid_at, value) tuples. That is robust to metadata
    # noise, which is why v0.1.23 chose it — but it also means a
    # genuinely NEW model run that happens to produce identical values to
    # the previous one (entirely plausible during a stable weather
    # pattern) collides with it and is discarded as a duplicate, silently
    # losing a real, independent training sample.
    #
    # When the provider gave us a real run identifier, that becomes the
    # PRIMARY discriminator and the content hash is layered on as a
    # secondary integrity check. When it did not, issued_at is just
    # datetime.now() — which changes on every single call and would
    # defeat deduplication entirely if embedded — so we fall back to
    # content-hash-only, exactly as before.
    content_hash = fingerprint_points(points)
    run_fingerprint = (
        f"{issued_at.isoformat()}|{content_hash}"
        if has_real_run_identity
        else content_hash
    )

    return ParsedMeteoblueForecast(
        issued_at=issued_at,
        grid_elevation_m=grid_elevation_m,
        points=points,
        predictability=data_1h.get("predictability"),
        run_fingerprint=run_fingerprint,
        array_length_mismatches=tuple(mismatches),
    )


class MeteoblueClient:
    """Requires an aiohttp.ClientSession (HA's shared session)."""

    def __init__(self, session: Any, api_key: str) -> None:
        self._session = session
        self._api_key = api_key

    async def async_fetch_forecast(
        self, *, latitude: float, longitude: float
    ) -> ParsedMeteoblueForecast:
        import aiohttp

        url = build_forecast_url(latitude=latitude, longitude=longitude, api_key=self._api_key)
        # v0.1.14: no explicit timeout existed here before — same fix as
        # every other client, caught by an outside code review checked
        # directly against the source.
        async with self._session.get(
            url, timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            resp.raise_for_status()
            payload = await resp.json()
        return parse_forecast_response(payload)
