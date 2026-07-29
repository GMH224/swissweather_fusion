"""Model A: smooth bias/blend correction (Model Output Statistics).

This is deliberately NOT machine learning in the gradient-boosting sense —
it's a bucketed exponential moving average, which is the right tool for a
slowly-drifting, low-data, streaming problem. See DEVELOPER.md for the full
"why EMA, not trees" reasoning.

Pure functions only in this module — no I/O, no Home Assistant imports, no
database access. The coordinator is responsible for reading/writing
bucket_stats via storage/db.py and calling these functions with plain
values. This separation is what makes the module trivially unit-testable
(see tests/test_model_a.py) without mocking a database or an event loop.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional
from datetime import datetime, timedelta, timezone

from ..const import (
    EMA_ALPHA_BY_LEAD_TIME,
    EMA_WEIGHT_EPSILON,
    LAPSE_RATE_C_PER_1000M,
    LEAD_TIME_LONG,
    LEAD_TIME_MEDIUM,
    LEAD_TIME_MEDIUM_MAX_HOURS,
    LEAD_TIME_SHORT,
    LEAD_TIME_SHORT_MAX_HOURS,
    MIN_SAMPLES_TO_TRUST_BUCKET,
    SEASON_DJF,
    SEASON_JJA,
    SEASON_MAM,
    SEASON_SON,
)


def derive_season(dt: datetime) -> str:
    """Meteorological seasons (DJF/MAM/JJA/SON), not astronomical or monthly.

    Pinned down explicitly per plan doc §3 — this was used everywhere in
    the design but never formally defined until the final audit pass.
    """
    month = dt.month
    if month in (12, 1, 2):
        return SEASON_DJF
    if month in (3, 4, 5):
        return SEASON_MAM
    if month in (6, 7, 8):
        return SEASON_JJA
    return SEASON_SON


def derive_lead_time_bucket(issued_at: datetime, valid_at: datetime) -> str:
    """Classify a forecast's lead time into short/medium/long.

    This is the fix for the bug found mid-project: without this dimension,
    a source's highly-accurate near-term forecast and its much noisier
    far-out forecast for the same hour-of-day were blended into one shared
    bias estimate, diluting exactly the short-range accuracy that matters
    most. See DEVELOPER.md ("The lead-time bucket bug") for the full story.
    """
    lead_hours = (valid_at - issued_at).total_seconds() / 3600.0
    if lead_hours < LEAD_TIME_SHORT_MAX_HOURS:
        return LEAD_TIME_SHORT
    if lead_hours < LEAD_TIME_MEDIUM_MAX_HOURS:
        return LEAD_TIME_MEDIUM
    return LEAD_TIME_LONG


@dataclass(frozen=True)
class EmaUpdateResult:
    """Result of folding one new observation into a bucket's running stats."""

    ema_bias: float
    ema_abs_error: float
    ema_weight: float
    sample_count: int


def update_bucket_ema(
    *,
    previous_bias: float,
    previous_abs_error: float,
    previous_sample_count: int,
    forecast_value: float,
    actual_value: float,
    lead_time_bucket: str,
) -> EmaUpdateResult:
    """Fold one new (forecast, actual) pair into a bucket's EMA statistics.

    Two separate quantities are tracked, deliberately:
      - ema_bias: the systematic offset (forecast - actual, signed) — this
        is what gets subtracted to debias a raw forecast.
      - ema_abs_error: the mean absolute error AFTER debiasing — this is
        what the blend weight is actually derived from. Bias and weight
        are different statistics: a source can be unbiased but noisy, or
        biased but very predictable. Deriving weight from bias alone would
        conflate the two. See DEVELOPER.md ("Two EMA bugs, not one").

    alpha (responsiveness) varies by lead_time_bucket: short buckets adapt
    fast since recent regime changes matter there; long buckets smooth
    heavily since their data is sparser and noisier per bucket.
    """
    alpha = EMA_ALPHA_BY_LEAD_TIME[lead_time_bucket]

    raw_error = forecast_value - actual_value
    if previous_sample_count == 0:
        new_bias = raw_error
    else:
        new_bias = alpha * raw_error + (1 - alpha) * previous_bias

    # Absolute error of the *debiased* forecast against actual — this is
    # what should drive the weight, not the raw error.
    debiased_forecast = forecast_value - new_bias
    abs_error_after_debias = abs(debiased_forecast - actual_value)
    if previous_sample_count == 0:
        new_abs_error = abs_error_after_debias
    else:
        new_abs_error = alpha * abs_error_after_debias + (1 - alpha) * previous_abs_error

    new_weight = 1.0 / (new_abs_error + EMA_WEIGHT_EPSILON)

    return EmaUpdateResult(
        ema_bias=new_bias,
        ema_abs_error=new_abs_error,
        ema_weight=new_weight,
        sample_count=previous_sample_count + 1,
    )


@dataclass(frozen=True)
class SourceContribution:
    source: str
    raw_value: Optional[float]
    ema_bias: float
    ema_weight: float
    sample_count: int


def blend(contributions: list[SourceContribution]) -> float | None:
    """Debias each source, then weight-blend the debiased values.

    Debias-then-blend, not blend-then-debias: two sources with opposite
    systematic biases could otherwise appear to "cancel out" in a way that
    looks fine but isn't for the right reason. See plan doc §3.

    Sources with sample_count below MIN_SAMPLES_TO_TRUST_BUCKET contribute
    their raw (uncorrected) value with a neutral weight of 1.0, rather than
    a partially-learned correction — this is the cold-start guard. If every
    contributing source is below the trust threshold, the blend still
    proceeds using raw values; there's no untrusted-everything fallback
    beyond that, by design (some answer is better than none).

    **v0.1.7 fix**: a source can legitimately return `null` for a given
    hour/measurement (any of Open-Meteo/SRF/meteoblue can do this), which
    gets stored in forecast_snapshots as None — this crashed in production
    with 'unsupported operand type(s) for *: NoneType and float' on every
    single blend cycle since deployment, which is why the weather entity
    stayed continuously Unavailable rather than intermittently. A
    contribution with raw_value=None is now treated the same as "this
    source has nothing to say for this hour" and skipped, same as if it
    had never been included at all.

    Returns None only if there are no *usable* contributions (empty list,
    or every contribution had a None raw_value) — the caller (coordinator)
    is expected to fall back to any single available raw forecast in that
    case, not treat None as an error.
    """
    if not contributions:
        return None

    weighted_sum = 0.0
    weight_total = 0.0
    for c in contributions:
        if c.raw_value is None:
            continue
        if c.sample_count < MIN_SAMPLES_TO_TRUST_BUCKET:
            debiased = c.raw_value
            weight = 1.0
        else:
            debiased = c.raw_value - c.ema_bias
            weight = c.ema_weight
        weighted_sum += debiased * weight
        weight_total += weight

    if weight_total == 0:
        return None
    return weighted_sum / weight_total


def apply_lapse_rate_precorrection(
    *,
    raw_temperature: float,
    source_grid_elevation_m: float,
    actual_elevation_m: float,
) -> float:
    """Optional physics-based head start before the EMA even starts learning.

    If a source's grid-cell elevation differs meaningfully from the
    configured actual elevation, this applies the standard environmental
    lapse rate directly, rather than making the EMA discover the same gap
    empirically over weeks. Purely optional — omit this call entirely if no
    precise elevation is configured; the EMA will still converge to the
    same correction eventually on its own, just more slowly.
    """
    elevation_diff_m = source_grid_elevation_m - actual_elevation_m
    correction = (elevation_diff_m / 1000.0) * LAPSE_RATE_C_PER_1000M
    # If the source's grid point is higher than the actual point, the
    # source will read too cold relative to reality, so we adjust upward
    # (and vice versa) by adding the correction back.
    return raw_temperature + correction


def utcnow() -> datetime:
    """Single source of truth for "now" in tests and production alike."""
    return datetime.now(timezone.utc)


# How close a station reading needs to be to a forecast's valid_at time to
# count as "the actual outcome" for reconciliation purposes — half the
# hourly granularity forecasts are made at, so the nearest reading is
# meaningfully tied to that specific hour rather than a neighboring one.
RECONCILIATION_TOLERANCE_MINUTES = 30


def find_nearest_observation(
    *,
    target: datetime,
    candidates: list[tuple[datetime, Optional[float]]],
    tolerance_minutes: int = RECONCILIATION_TOLERANCE_MINUTES,
) -> Optional[float]:
    """Given a forecast's valid_at time and a list of (timestamp, value)
    station readings, returns the value of the nearest one within
    tolerance, or None if nothing qualifies (no readings, all outside the
    tolerance window, or the nearest one's value is itself None).

    Pure function — the actual DB fetch of candidates happens once per
    reconciliation batch in the coordinator, not per forecast row, so this
    only does the in-memory nearest-match logic.
    """
    best_value: Optional[float] = None
    best_diff_seconds: Optional[float] = None
    for ts, value in candidates:
        if value is None:
            continue
        diff_seconds = abs((ts - target).total_seconds())
        if diff_seconds > tolerance_minutes * 60:
            continue
        if best_diff_seconds is None or diff_seconds < best_diff_seconds:
            best_value = value
            best_diff_seconds = diff_seconds
    return best_value


def aggregate_daily_forecast(hourly_forecast: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Groups the already-computed hourly blend entries into daily
    high/low temperature and total precipitation — no additional data
    access needed, this is purely a reshaping of what the hourly
    forecast already produced.

    **Known simplification**: groups by UTC calendar day, not the
    configured local timezone. Hours near midnight can land in the
    "wrong" local day as a result. Correcting this needs the HA-configured
    timezone threaded through, which is straightforward to add later but
    was not the priority for the first real version of this feature.
    """
    by_day: dict[Any, list[dict[str, Any]]] = {}
    for entry in hourly_forecast:
        day = datetime.fromisoformat(entry["datetime"]).date()
        by_day.setdefault(day, []).append(entry)

    results: list[dict[str, Any]] = []
    for day in sorted(by_day):
        entries = by_day[day]
        temps = [e["native_temperature"] for e in entries if e["native_temperature"] is not None]
        precips = [
            e["native_precipitation"] for e in entries if e["native_precipitation"] is not None
        ]
        total_precip = sum(precips) if precips else None
        results.append(
            {
                "datetime": datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).isoformat(),
                "native_temperature": max(temps) if temps else None,
                "native_templow": min(temps) if temps else None,
                "native_precipitation": total_precip,
                "condition": "rainy" if (total_precip or 0) > 0.5 else "sunny",
            }
        )
    return results


# UTC-hour boundaries for the day/night split — same "not localized yet"
# simplification as aggregate_daily_forecast above.
TWICE_DAILY_DAY_START_HOUR = 6
TWICE_DAILY_DAY_END_HOUR = 18


def aggregate_twice_daily_forecast(hourly_forecast: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Splits each day into a daytime period (06:00-18:00 UTC) and a
    nighttime period (18:00-06:00 UTC), each with its own representative
    temperature and total precipitation — same source data as the daily
    aggregation above, just grouped differently.
    """
    by_period: dict[tuple[Any, bool], list[dict[str, Any]]] = {}
    for entry in hourly_forecast:
        dt = datetime.fromisoformat(entry["datetime"])
        is_daytime = TWICE_DAILY_DAY_START_HOUR <= dt.hour < TWICE_DAILY_DAY_END_HOUR
        if is_daytime:
            period_day = dt.date()
        elif dt.hour >= TWICE_DAILY_DAY_END_HOUR:
            period_day = dt.date()  # night period starting this evening
        else:
            # Early-morning hours (00:00-05:59) are the tail end of the
            # *previous* day's overnight period, not a new one starting
            # at midnight — this branch was a no-op bug initially (both
            # sides of the condition returned dt.date()), caught before
            # ever shipping.
            period_day = dt.date() - timedelta(days=1)
        by_period.setdefault((period_day, is_daytime), []).append(entry)

    results: list[dict[str, Any]] = []
    for (day, is_daytime), entries in sorted(by_period.items(), key=lambda kv: (kv[0][0], not kv[0][1])):
        temps = [e["native_temperature"] for e in entries if e["native_temperature"] is not None]
        precips = [
            e["native_precipitation"] for e in entries if e["native_precipitation"] is not None
        ]
        total_precip = sum(precips) if precips else None
        period_start_hour = TWICE_DAILY_DAY_START_HOUR if is_daytime else TWICE_DAILY_DAY_END_HOUR
        results.append(
            {
                "datetime": datetime.combine(
                    day, datetime.min.time(), tzinfo=timezone.utc
                ).replace(hour=period_start_hour).isoformat(),
                "is_daytime": is_daytime,
                "native_temperature": max(temps) if temps else None,
                "native_precipitation": total_precip,
                "condition": "rainy" if (total_precip or 0) > 0.5 else "sunny",
            }
        )
    return results
