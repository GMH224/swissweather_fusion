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

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional

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
                precip_risk_value=precrisk.get("value"),
                radar_precip_mmh=amount.get("value"),
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
