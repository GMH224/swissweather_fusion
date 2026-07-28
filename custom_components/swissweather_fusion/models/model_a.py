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
from datetime import datetime, timezone

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
    raw_value: float
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

    Returns None only if contributions is empty — the caller (coordinator)
    is expected to fall back to any single available raw forecast in that
    case, not treat None as an error.
    """
    if not contributions:
        return None

    weighted_sum = 0.0
    weight_total = 0.0
    for c in contributions:
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
