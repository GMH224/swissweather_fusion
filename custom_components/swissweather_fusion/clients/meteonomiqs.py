"""Meteonomiqs (wetter.com Public Weather API v4.0) client.

Much simpler auth than SRF — a flat `x-api-key` header, no OAuth2 dance.
The defining constraint here is the opposite problem: a 1000 calls/year
budget, far tighter than any other source in this project. That budget is
why this is NOT a routinely-polled source — see DEVELOPER.md ("Why
Meteonomiqs needs a daily heartbeat").

Two endpoints are used, both on the plain (non-premium) tier:
  - /nowcast/weather/{lat}/{lon}: 5-minute-resolution radar-derived
    precipitation risk (a 0-9 scale) and amount — an independent
    cross-check for Model B, reserved for the cross-model trigger rather
    than polled continuously like CombiPrecip.
  - /forecast/{lat}/{lon}/hourly: hourly pressure and precipitation
    (sum + probability) forecasts. **Correction**: this originally used
    /forecast2, which turned out to be a paid tier not included in the
    actual API key obtained for this project — /forecast2's CAPE index
    is therefore NOT available; CAPE remains an open question for Model B,
    same as before Meteonomiqs was introduced. See DEVELOPER.md.
"""
from __future__ import annotations

import logging
import math

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://forecast.meteonomiqs.com/v4_0"

# Reference table from Meteonomiqs's own docs ("Nowcast Radar Values") —
# 0 = no precipitation, 9 = extreme / hail possible. Kept here rather than
# re-deriving a probability from scratch, since Meteonomiqs already
# provides a calibrated-by-them risk scale.
RADAR_RISK_SCALE_MAX = 9


def build_nowcast_url(*, latitude: float, longitude: float) -> str:
    return f"{BASE_URL}/nowcast/weather/{latitude}/{longitude}"


def build_auth_headers(api_key: str) -> dict[str, str]:
    return {"x-api-key": api_key, "Accept-Language": "en-US"}


@dataclass(frozen=True)
class NowcastItem:
    from_ts: datetime
    to_ts: datetime
    precip_risk_value: Optional[int]  # 0-9 scale, Meteonomiqs's own
    radar_precip_mmh: Optional[float]


@dataclass(frozen=True)
class ParsedNowcast:
    items: list[NowcastItem]
    fetched_at: datetime


# Meteonomiqs documents precipitationRisk as an integer 0-9 scale.
PRECIP_RISK_MIN = 0
PRECIP_RISK_MAX = 9


def _validated_risk_value(raw: Any) -> Optional[int]:
    """Accept a documented 0-9 risk value; reject anything else.

    **v0.1.27 fix (SWF-P1-003).** This value was previously stored
    straight from the JSON with no type, finiteness or range check, and
    models/model_b.refine_with_meteonomiqs divides it by 9 and averages
    the result into the storm score. A risk value of 99 therefore
    produced a refined score of 5.9 — surfaced by
    StormOnsetProbabilitySensor, which advertises `%`, as **590%**.

    The consequences run past a silly dashboard number: any automation
    thresholding on that sensor fires unconditionally, and the value is
    persisted into storm_predictions, which is the training set for
    Model B v1. Corrupt training data is the expensive kind of wrong,
    because it is not obviously wrong later.

    Out-of-range means the provider is not behaving as documented, so the
    value is discarded rather than clamped: clamping 99 to 9 would invent
    a maximum-risk reading out of a response we do not understand.
    Returning None puts this interval in the same state as one the
    provider simply did not rate, which every caller already handles.

    Numeric strings are accepted because JSON APIs return them
    inconsistently and "7" is unambiguous; bools are refused despite
    being int subclasses, since True would silently become risk 1.
    """
    if raw is None or isinstance(raw, bool):
        return None
    try:
        numeric = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric) or numeric != int(numeric):
        # A fractional value is not on the documented integer scale.
        # int(3.7) would silently become 3, which is a guess about a
        # response we do not understand — the same silent-guess pattern
        # rejected in unit_conversion.py for unrecognised units.
        _LOGGER.warning(
            "Meteonomiqs returned non-integer precipitation risk %r; "
            "ignoring this interval", raw
        )
        return None
    value = int(numeric)
    if not PRECIP_RISK_MIN <= value <= PRECIP_RISK_MAX:
        _LOGGER.warning(
            "Meteonomiqs returned precipitation risk %r, outside its "
            "documented %d-%d scale; ignoring this interval",
            raw,
            PRECIP_RISK_MIN,
            PRECIP_RISK_MAX,
        )
        return None
    return value


def _validated_radar_amount(raw: Any) -> Optional[float]:
    """Finite, non-negative precipitation amount, or None.

    v0.1.27 (SWF-P2-002): parser-level normalisation, so a string or a
    non-finite value cannot reach arithmetic downstream before the shared
    physical-bounds validator ever sees it.
    """
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def parse_nowcast_response(payload: dict[str, Any]) -> ParsedNowcast:
    precip_risk = payload.get("precipitationRisk", {})
    items: list[NowcastItem] = []
    for entry in precip_risk.get("items", []):
        from_str = entry.get("from")
        to_str = entry.get("to")
        if not from_str or not to_str:
            continue
        from_ts = datetime.fromisoformat(from_str.replace("Z", "+00:00"))
        to_ts = datetime.fromisoformat(to_str.replace("Z", "+00:00"))
        precrisk = entry.get("precrisk", {})
        radar = entry.get("radar", {})
        amount = radar.get("amount", {})
        items.append(
            NowcastItem(
                from_ts=from_ts,
                to_ts=to_ts,
                # v0.1.27 fix (SWF-P1-003): validated at the parser
                # boundary rather than trusted. See _validated_risk_value.
                precip_risk_value=_validated_risk_value(precrisk.get("value")),
                radar_precip_mmh=_validated_radar_amount(amount.get("value")),
            )
        )
    return ParsedNowcast(items=items, fetched_at=datetime.now(timezone.utc))


def build_forecast_hourly_url(*, latitude: float, longitude: float) -> str:
    return f"{BASE_URL}/forecast/{latitude}/{longitude}/hourly"


@dataclass(frozen=True)
class HourlyForecastPoint:
    valid_at: datetime
    mean_sea_level_pressure: Optional[float]
    precipitation_sum_mm: Optional[float]
    precipitation_probability: Optional[float]


def parse_hourly_forecast(payload: dict[str, Any]) -> list[HourlyForecastPoint]:
    """Parses /forecast/{lat}/{lon}/hourly — the plain (non-premium) hourly
    endpoint. **Correction**: this project originally built against
    /forecast2, which turned out to be a paid tier not included in the
    actual API key obtained — see DEVELOPER.md ("Why Meteonomiqs needs a
    daily heartbeat") for the full story. Pressure and precipitation are
    both present here too, just structured differently (plain fields
    rather than nested under "parameters", and no CAPE — that field is
    premium-only and is not available on this tier).
    """
    items = payload.get("items", [])
    points: list[HourlyForecastPoint] = []
    for entry in items:
        from_str = entry.get("from")
        if not from_str:
            continue
        prec = entry.get("prec", {})
        points.append(
            HourlyForecastPoint(
                valid_at=datetime.fromisoformat(from_str.replace("Z", "+00:00")),
                mean_sea_level_pressure=entry.get("pressure"),
                precipitation_sum_mm=prec.get("sum"),
                precipitation_probability=prec.get("probability"),
            )
        )
    return points


@dataclass
class AnnualCallBudget:
    """Tracks the 1000-calls/year allowance. Rolls over on calendar year
    boundary, not a rolling 365-day window — simple and matches how this
    kind of vendor quota is normally billed. Both the daily keep-alive
    calls and the event-triggered bonus calls draw from this same pool;
    the keep-alive is unconditional (see needs_keepalive_call below) and
    should never be skipped just because bonus calls used up budget
    elsewhere that day — losing API access entirely from inactivity is a
    worse outcome than a slightly tighter annual budget.
    """

    def __init__(self, annual_budget: int) -> None:
        self._annual_budget = annual_budget
        self._calls_used_this_year = 0
        self._current_year: Optional[int] = None

    def _roll_if_new_year(self, *, today: date) -> None:
        if today.year != self._current_year:
            self._current_year = today.year
            self._calls_used_this_year = 0

    def can_call(self, *, today: date) -> bool:
        self._roll_if_new_year(today=today)
        return self._calls_used_this_year < self._annual_budget

    def record_call(self, *, today: date) -> None:
        self._roll_if_new_year(today=today)
        self._calls_used_this_year += 1

    def try_call(self, *, today: date) -> bool:
        """v0.1.15 fix: combines can_call + record_call into one atomic
        check-and-record, same TOCTOU fix as BonusCallTracker.try_use_bonus_call
        above, for the bonus-call path specifically (the daily keepalive
        path deliberately still calls record_call() unconditionally with
        no check at all — see the class docstring for why that's
        intentional, not an oversight to also "fix" here).
        """
        if not self.can_call(today=today):
            return False
        self.record_call(today=today)
        return True

    @property
    def calls_remaining_this_year(self) -> int:
        return max(0, self._annual_budget - self._calls_used_this_year)

    def to_state(self) -> dict:
        """v0.1.23 fix (L-07): serializes year + calls-used so the
        coordinator can persist it via
        SwissWeatherDB.set_annual_call_budget_state(). Previously this
        state existed only as plain instance attributes, reset to zero on
        every Home Assistant restart/reload — meaning repeated restarts
        could silently bypass the intended annual call protection,
        eventually exceeding the real 1000-calls/year vendor quota without
        the integration's own tracking ever reflecting it."""
        return {
            "year": self._current_year,
            "calls_used": self._calls_used_this_year,
        }

    def load_state(self, state: Optional[dict]) -> None:
        """Inverse of to_state(), applied in place (the coordinator
        constructs the budget first, then loads persisted state into it,
        since annual_budget itself comes from a constant, not from
        storage). A missing/empty state behaves exactly like the old
        always-zero default — this only adds durability, it doesn't change
        first-run behavior."""
        if not state:
            return

        # v0.1.27 fix (SWF-P2-001): validate what comes back from
        # storage. SwissWeatherDB._safe_parse_meta already guarantees the
        # value is well-formed JSON (P2-02), but well-formed is not the
        # same as meaningful: a partially-written or hand-edited row can
        # carry a string year, a negative call count, or a count far
        # above the annual budget. Any of those silently corrupts quota
        # accounting for a whole year — a negative count hands out free
        # calls, an inflated one starves the source.
        #
        # Invalid state is DISCARDED rather than clamped, leaving the
        # in-memory default (year None, zero used). That is the same
        # state as "never persisted", which is already handled correctly
        # everywhere, and it fails safe: the next successful call
        # re-establishes the year and the count starts from a known
        # floor.
        year = state.get("year")
        calls_used = state.get("calls_used", 0)

        if year is not None and (isinstance(year, bool) or not isinstance(year, int)):
            _LOGGER.warning(
                "Discarding persisted annual-budget state: year %r is not an "
                "integer", year
            )
            return
        if isinstance(calls_used, bool) or not isinstance(calls_used, int):
            _LOGGER.warning(
                "Discarding persisted annual-budget state: calls_used %r is "
                "not an integer", calls_used
            )
            return
        if calls_used < 0 or calls_used > self._annual_budget:
            _LOGGER.warning(
                "Discarding persisted annual-budget state: calls_used %d is "
                "outside 0..%d", calls_used, self._annual_budget
            )
            return

        self._current_year = year
        self._calls_used_this_year = calls_used


def needs_keepalive_call(
    *, last_successful_call_date: Optional[date], today: date, max_days_between_calls: int
) -> bool:
    """True if it's been long enough since the last successful call that
    the API key risks revocation from inactivity.

    **v0.1.15 fix**: this used to be the single gate deciding whether the
    coordinator's daily call logic ran at all — meaning the actual API
    calls (both the seasonal forecast branch and the nowcast fallback)
    never fired more than once every max_days_between_calls (~30 days per
    Meteonomiqs's stated policy), contradicting this project's own
    documented intent of a daily call. Confirmed by an outside code
    review against the exact coordinator code. Now used only as a
    warning signal in the coordinator (see coordinator.py) — the actual
    daily-once-per-day logic is the real gate; this function just flags
    if that daily logic has somehow failed to produce a successful call
    for this long, which is worth knowing loudly rather than silently.
    """
    if last_successful_call_date is None:
        return True
    days_since = (today - last_successful_call_date).days
    return days_since >= max_days_between_calls


class MeteonomiqsClient:
    """Requires an aiohttp.ClientSession (HA's shared session)."""

    def __init__(self, session: Any, api_key: str) -> None:
        self._session = session
        self._api_key = api_key

    async def async_fetch_nowcast(
        self, *, latitude: float, longitude: float
    ) -> ParsedNowcast:
        import aiohttp

        url = build_nowcast_url(latitude=latitude, longitude=longitude)
        headers = build_auth_headers(self._api_key)
        # v0.1.14: no explicit timeout existed on either call in this
        # client — same fix as every other client, caught by an outside
        # code review checked directly against the source.
        async with self._session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            resp.raise_for_status()
            payload = await resp.json()
        return parse_nowcast_response(payload)

    async def async_fetch_hourly_forecast(
        self, *, latitude: float, longitude: float
    ) -> list[HourlyForecastPoint]:
        import aiohttp

        url = build_forecast_hourly_url(latitude=latitude, longitude=longitude)
        headers = build_auth_headers(self._api_key)
        async with self._session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            resp.raise_for_status()
            payload = await resp.json()
        return parse_hourly_forecast(payload)
