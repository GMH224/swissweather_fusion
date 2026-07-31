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

BASE_URL = "https://my.meteoblue.com/packages/basic-1h_basic-day"


def build_forecast_url(*, latitude: float, longitude: float, api_key: str) -> str:
    return f"{BASE_URL}?lat={latitude}&lon={longitude}&apikey={api_key}"


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
    """True if local_dt's hour matches this month's schedule (minute 0)."""
    return local_dt.hour in scheduled_hours_for_month(local_dt.month) and local_dt.minute == 0


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
    """Tracks the one-bonus-call-per-storm-scenario allowance (const.py:
    METEOBLUE_MAX_BONUS_CALLS_PER_EVENT). Per calendar day, not per event
    duration — a new storm scenario on the same day does not reset this;
    that's a deliberate conservative choice to protect the annual credit
    budget, worth revisiting once real storm clustering is observed
    (see DEVELOPER.md).
    """

    _calls_used_by_date: dict[date, int]

    def __init__(self) -> None:
        self._calls_used_by_date = {}

    def can_use_bonus_call(self, *, today: date) -> bool:
        used = self._calls_used_by_date.get(today, 0)
        return used < METEOBLUE_MAX_BONUS_CALLS_PER_EVENT

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


# Fields confirmed present via a live test call during planning:
# relativehumidity, sealevelpressure, temperature, precipitation, windspeed,
# felttemperature, uvindex, predictability, rainspot (format undocumented).
_FIELD_MAP = {
    "temperature": "temperature",
    "relativehumidity": "humidity",
    "sealevelpressure": "pressure",
    "precipitation": "precip",
    "windspeed": "wind_speed",
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


def parse_forecast_response(payload: dict[str, Any]) -> ParsedMeteoblueForecast:
    metadata = payload.get("metadata", {})
    issued_at_str = metadata.get("modelrun_updatetime_utc")
    issued_at = (
        datetime.fromisoformat(issued_at_str).replace(tzinfo=timezone.utc)
        if issued_at_str
        else datetime.now(timezone.utc)
    )
    grid_elevation_m = metadata.get("height")

    data_1h = payload.get("data_1h", {})
    times = data_1h.get("time", [])

    points: list[MeteoblueForecastPoint] = []
    for mb_key, internal_name in _FIELD_MAP.items():
        values = data_1h.get(mb_key)
        if values is None:
            continue
        for t_str, value in zip(times, values):
            valid_at = datetime.strptime(t_str, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            points.append(
                MeteoblueForecastPoint(variable=internal_name, valid_at=valid_at, value=value)
            )

    return ParsedMeteoblueForecast(
        issued_at=issued_at,
        grid_elevation_m=grid_elevation_m,
        points=points,
        predictability=data_1h.get("predictability"),
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
