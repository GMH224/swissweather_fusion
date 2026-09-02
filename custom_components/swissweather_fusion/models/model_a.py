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
    CONDITION_CLOUDY_HUMIDITY_THRESHOLD,
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

    v0.1.23 fix (L-03, external ICS audit): ema_abs_error MUST be computed
    using previous_bias, before this observation updates the bias — not
    after. The old code computed new_bias first (folding this very
    observation's raw error into it) and only then measured the "debiased"
    residual against that already-updated bias, so the current sample
    partially fit itself before it ever contributed to ema_abs_error. That
    made the error statistic systematically optimistic — worst for new/
    low-sample-count buckets, where alpha is largest — and, since
    ema_weight = 1 / (ema_abs_error + epsilon), a learned source weight
    could end up too large. Evaluating the residual against previous_bias
    first (the bias as it stood *before* this sample) fixes that: the
    sample is judged on how well the *existing* model predicted it, then
    folded into both statistics for next time. This is standard online
    least-squares/EMA practice — predict-then-update, never update-then-
    grade-yourself-against-the-update.
    """
    alpha = EMA_ALPHA_BY_LEAD_TIME[lead_time_bucket]

    raw_error = forecast_value - actual_value

    # Judge this observation against the bias as it stood BEFORE this
    # sample updates it — see the fix note above. On the very first sample
    # for a bucket there is no previous bias to debias against yet, so the
    # raw (undebiased) forecast is the only honest baseline.
    debiased_forecast = forecast_value - previous_bias
    abs_error_before_bias_update = abs(debiased_forecast - actual_value)
    if previous_sample_count == 0:
        new_abs_error = abs_error_before_bias_update
    else:
        new_abs_error = alpha * abs_error_before_bias_update + (1 - alpha) * previous_abs_error

    if previous_sample_count == 0:
        new_bias = raw_error
    else:
        new_bias = alpha * raw_error + (1 - alpha) * previous_bias

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


# v0.1.24 (IND-01): the two helpers below exist to put cold-start and
# learned weights on one comparable, dimensionless scale.
#
# MAX_LEARNED_WEIGHT_RATIO bounds how far a single well-performing source
# can dominate the others within one blend. Without it, a bucket whose
# ema_abs_error happens to approach zero reaches 1/EMA_WEIGHT_EPSILON =
# 100 and effectively becomes the only contributor — which is not a
# statement the data supports, since ema_abs_error is itself an EMA over
# a modest number of samples and can be transiently tiny by luck. 8:1 is
# wide enough to let a genuinely better source lead decisively while
# keeping the others audible.
MAX_LEARNED_WEIGHT_RATIO = 8.0


def _reference_weight(contributions: list[SourceContribution]) -> float:
    """The weight scale for THIS blend, in this measurement's units.

    Returns the median learned weight among trusted contributors, which
    is the natural "neutral" point for the set: a cold-start source is
    neither better nor worse than a typical learned source, which is
    exactly the claim the cold-start guard wants to make.

    When no contributor is trusted yet (the genuine cold-start case,
    every source below MIN_SAMPLES_TO_TRUST_BUCKET), every source gets
    this same value, so the blend degenerates to a plain average of raw
    values — identical to the pre-v0.1.24 behaviour for that case, which
    was always correct.
    """
    trusted = [
        c.ema_weight
        for c in contributions
        if c.sample_count >= MIN_SAMPLES_TO_TRUST_BUCKET
        and c.ema_weight is not None
        and c.ema_weight > 0
    ]
    if not trusted:
        return 1.0
    ordered = sorted(trusted)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _clamp_learned_weight(weight: float | None, reference: float) -> float:
    """Bound a learned weight to a sane multiple of the reference scale.

    Also guards against a None or non-positive weight, which would
    otherwise silently remove a source from the blend or, worse, make
    weight_total zero.
    """
    if weight is None or weight <= 0:
        return reference
    ceiling = reference * MAX_LEARNED_WEIGHT_RATIO
    floor = reference / MAX_LEARNED_WEIGHT_RATIO
    return max(floor, min(ceiling, weight))


def blend(contributions: list[SourceContribution]) -> float | None:
    """Debias each source, then weight-blend the debiased values.

    Debias-then-blend, not blend-then-debias: two sources with opposite
    systematic biases could otherwise appear to "cancel out" in a way that
    looks fine but isn't for the right reason. See plan doc §3.

    Sources with sample_count below MIN_SAMPLES_TO_TRUST_BUCKET contribute
    their raw (uncorrected) value rather than a partially-learned
    correction — this is the cold-start guard. If every contributing
    source is below the trust threshold, the blend still proceeds using
    raw values; there's no untrusted-everything fallback beyond that, by
    design (some answer is better than none).

    **v0.1.24 fix (IND-01)**: the cold-start weight used to be a
    hard-coded 1.0 while a trusted source's weight was
    1 / (ema_abs_error + EMA_WEIGHT_EPSILON). Those two numbers are not
    on the same scale, and the learned one carries the measurement's own
    units, so the blend behaved differently — and wrongly — per
    measurement:

        humidity, trusted source with MAE 5%      -> weight 0.20
        pressure, trusted source with MAE 0.3 hPa -> weight 3.23

    against a cold-start weight of exactly 1.0 in both cases. For
    humidity and precipitation, where absolute errors are numerically
    large, every well-characterised source was therefore weighted BELOW
    every unvalidated one: a source with 200 validated samples was
    outvoted roughly 5:1 by a source with one. Learning made the blend
    worse. There was also no upper bound — EMA_WEIGHT_EPSILON caps the
    raw weight at 100, so one bucket with a near-zero error could
    dominate 100:1.

    Both are fixed by making the weights dimensionless relative to the
    contributing set: see _reference_weight() and
    _clamp_learned_weight(). This changes blend output for existing
    installations, which is why it lands with a schema rebuild rather
    than as a silent in-place behaviour change.

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

    usable = [c for c in contributions if c.raw_value is not None]
    if not usable:
        return None

    reference = _reference_weight(usable)

    weighted_sum = 0.0
    weight_total = 0.0
    for c in usable:
        if c.sample_count < MIN_SAMPLES_TO_TRUST_BUCKET:
            debiased = c.raw_value
            # v0.1.24 fix (IND-01): the cold-start weight is now drawn
            # from the same scale as the learned weights in this blend,
            # instead of the hard-coded 1.0 that made the two
            # incomparable. See _reference_weight below.
            weight = reference
        else:
            debiased = c.raw_value - c.ema_bias
            weight = _clamp_learned_weight(c.ema_weight, reference)
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


def aggregate_daily_forecast(
    hourly_forecast: list[dict[str, Any]], *, local_tz: timezone = timezone.utc
) -> list[dict[str, Any]]:
    """Groups the already-computed hourly blend entries into daily
    high/low temperature and total precipitation — no additional data
    access needed, this is purely a reshaping of what the hourly
    forecast already produced.

    **v0.1.15 fix**: this used to always group by UTC calendar day,
    regardless of the configured local timezone — confirmed by an
    outside code review as a real bug (hours near midnight could land in
    the "wrong" local day). `local_tz` defaults to UTC so any caller not
    yet passing a real timezone gets the exact same behavior as before —
    the caller (coordinator.py) is expected to pass HA's actual
    configured timezone.
    """
    by_day: dict[Any, list[dict[str, Any]]] = {}
    for entry in hourly_forecast:
        local_dt = datetime.fromisoformat(entry["datetime"]).astimezone(local_tz)
        by_day.setdefault(local_dt.date(), []).append(entry)

    results: list[dict[str, Any]] = []
    for day in sorted(by_day):
        entries = by_day[day]
        temps = [e["native_temperature"] for e in entries if e["native_temperature"] is not None]
        precips = [
            e["native_precipitation"] for e in entries if e["native_precipitation"] is not None
        ]
        total_precip = sum(precips) if precips else None
        # v0.1.24 (P2-10): needed by derive_condition's "cloudy" branch.
        humidities = [e.get("humidity") for e in entries if e.get("humidity") is not None]
        results.append(
            {
                "datetime": datetime.combine(day, datetime.min.time(), tzinfo=local_tz).isoformat(),
                "native_temperature": max(temps) if temps else None,
                "native_templow": min(temps) if temps else None,
                "native_precipitation": total_precip,
                # v0.1.24 (P2-10): 0.5 mm passed explicitly — a DAILY
                # total is not the same quantity as an hourly amount and
                # must not silently inherit the hourly site's threshold.
                # `total_precip or 0` preserves this site's own
                # pre-existing None-as-zero behaviour.
                "condition": derive_condition(
                    total_precip or 0,
                    max(temps) if temps else None,
                    max(humidities) if humidities else None,
                    precip_threshold=0.5,
                ),
            }
        )
    return results


# Local-hour boundaries for the day/night split (interpreted against
# whatever local_tz is passed to aggregate_twice_daily_forecast below).
TWICE_DAILY_DAY_START_HOUR = 6
TWICE_DAILY_DAY_END_HOUR = 18


def aggregate_twice_daily_forecast(
    hourly_forecast: list[dict[str, Any]], *, local_tz: timezone = timezone.utc
) -> list[dict[str, Any]]:
    """Splits each day into a daytime period (06:00-18:00 local) and a
    nighttime period (18:00-06:00 local), each with its own representative
    temperature and total precipitation — same source data as the daily
    aggregation above, just grouped differently.

    **v0.1.15 fix**: same timezone bug and fix as aggregate_daily_forecast
    above — `local_tz` defaults to UTC for backward compatibility.

    **v0.1.23 fix (own-review finding, not in the external ICS audit)**:
    the night period's representative temperature now uses min(temps)
    ("overnight low"), not max(temps). It previously used max() for both
    day AND night periods — meaning a night entry reported the warmest
    point of the night (typically right after sunset) rather than the
    overnight low, the opposite of what "night temperature" conventionally
    means and inconsistent with aggregate_daily_forecast's own separate
    native_temperature (high) / native_templow (low) split just above.
    This was silent: the shipped unit tests asserted the max()-for-night
    behavior as if it were correct, so nothing caught it.
    """
    by_period: dict[tuple[Any, bool], list[dict[str, Any]]] = {}
    for entry in hourly_forecast:
        dt = datetime.fromisoformat(entry["datetime"]).astimezone(local_tz)
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
        # v0.1.24 (P2-10): needed by derive_condition's "cloudy" branch.
        humidities = [e.get("humidity") for e in entries if e.get("humidity") is not None]
        period_start_hour = TWICE_DAILY_DAY_START_HOUR if is_daytime else TWICE_DAILY_DAY_END_HOUR
        results.append(
            {
                "datetime": datetime.combine(
                    day, datetime.min.time(), tzinfo=local_tz
                ).replace(hour=period_start_hour).isoformat(),
                "is_daytime": is_daytime,
                "native_temperature": (
                    (max(temps) if is_daytime else min(temps)) if temps else None
                ),
                "native_precipitation": total_precip,
                # v0.1.24 (P2-10): see the daily aggregation above.
                "condition": derive_condition(
                    total_precip or 0,
                    (max(temps) if is_daytime else min(temps)) if temps else None,
                    max(humidities) if humidities else None,
                    precip_threshold=0.5,
                ),
            }
        )
    return results


# ---------------------------------------------------------------------------
# Weather condition mapping (v0.1.24, P2-10)
# ---------------------------------------------------------------------------
def derive_condition(
    precip: float | None,
    temperature: float | None,
    humidity: float | None,
    precip_threshold: float = 0.1,
) -> str | None:
    """Map blended values to a Home Assistant weather condition string.

    **Why this function exists.** Four separate call sites — weather.py's
    current condition, coordinator.py's hourly forecast, and both of this
    module's daily/twice-daily aggregations — independently used

        "rainy" if precip > threshold else "sunny"

    which collapsed snow, cloud, overcast and fog all into "sunny". The
    weather card showed a sun during a snowstorm.

    **Two pre-existing behaviours are deliberately preserved rather than
    unified**, because both were intentional and unifying them would
    silently change one of the call sites:

    1. ``precip_threshold`` defaults to 0.1 (the hourly sites' value) but
       the daily/twice-daily aggregation sites pass 0.5 explicitly. A
       daily total and an hourly amount are not the same quantity and
       should not share a threshold.
    2. None handling. This returns None only when ``precip`` itself is
       None. The daily/twice-daily callers pass ``total_precip or 0``,
       matching their prior None-as-zero behaviour; weather.py and the
       hourly forecast pass raw precip, matching their prior
       explicit-None behaviour.

    **The "cloudy" branch is an honest v0 heuristic.** High relative
    humidity with no significant precipitation often but not always
    means overcast. None of the five providers is queried for cloud
    cover today, so this is a plausible proxy, not a measurement — see
    CONDITION_CLOUDY_HUMIDITY_THRESHOLD in const.py and the caveat in
    DEVELOPER.md.
    """
    if precip is None:
        return None

    if precip > precip_threshold:
        # Temperature unknown resolves to rain rather than snow: rain is
        # far more common across the year at Swiss valley altitudes, and
        # a wrong "snowy" is the more conspicuous error.
        if temperature is not None and temperature <= 0.0:
            return "snowy"
        return "rainy"

    if humidity is not None and humidity >= CONDITION_CLOUDY_HUMIDITY_THRESHOLD:
        return "cloudy"

    return "sunny"
