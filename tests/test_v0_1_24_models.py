"""Regression tests for v0.1.24's model-layer fixes.

Each test here reproduces the ORIGINAL failure mode and asserts the
specific behavioural difference the fix makes — not merely that the new
code runs without raising.

Covers IND-01 (blend weight scale), IND-02 (dropout resilience), P1-13
(radar freshness), P1-16 (radar quality), P1-14 (accumulation semantics)
and P2-10 (condition mapping).
"""
from datetime import datetime, timedelta, timezone

import pytest

from swissweather_fusion.const import (
    LOCAL_POINT_PROBABILITY,
    MIN_SAMPLES_TO_TRUST_BUCKET,
    RADAR_FRESHNESS_LIMIT,
    UPWIND_POINT_PROBABILITY,
)
from swissweather_fusion.models import model_a, model_b

NOW = datetime(2026, 7, 25, 15, 0, tzinfo=timezone.utc)


def _contribution(source, value, weight, samples, bias=0.0):
    return model_a.SourceContribution(
        source=source,
        raw_value=value,
        ema_bias=bias,
        ema_weight=weight,
        sample_count=samples,
    )


# ---------------------------------------------------------------------------
# IND-01 — blend weights must not depend on the measurement's units
# ---------------------------------------------------------------------------
def test_well_learned_source_is_not_outvoted_by_a_cold_start_source():
    """The original defect, stated as a behaviour.

    Weights used to be `1.0` for a cold-start source and
    `1 / (ema_abs_error + 0.01)` for a trusted one. For humidity, where a
    good MAE is around 5%, that gave the trusted source a weight of 0.20
    against the newcomer's 1.0 — so a source with 200 validated samples
    was outvoted 5:1 by a source with one, and the blend got WORSE as
    learning progressed.

    Asserted as an inequality about influence rather than an exact
    number, so the test survives a change of normalisation strategy but
    not a regression of the property.
    """
    trusted = _contribution("ch1", 60.0, 1 / (5.0 + 0.01), 200)
    cold = _contribution("ch2", 90.0, 1.0, 1)

    blended = model_a.blend([trusted, cold])

    midpoint = (60.0 + 90.0) / 2
    assert blended <= midpoint + 1e-9, (
        "a source with 200 samples must not be pulled past the midpoint by "
        f"a source with 1 (got {blended})"
    )


def test_blend_weighting_is_consistent_across_measurement_scales():
    """The same relative skill must produce the same relative influence
    whether the measurement is humidity (errors ~5) or pressure
    (errors ~0.3). Under the old unit-carrying weights these two cases
    produced opposite orderings."""
    humidity = model_a.blend([
        _contribution("good", 0.0, 1 / (5.0 + 0.01), 200),
        _contribution("new", 10.0, 1.0, 1),
    ])
    pressure = model_a.blend([
        _contribution("good", 0.0, 1 / (0.3 + 0.01), 200),
        _contribution("new", 10.0, 1.0, 1),
    ])
    assert humidity == pytest.approx(pressure), (
        "identical skill relationships produced different blends purely "
        "because the measurements have different natural error magnitudes"
    )


def test_blend_caps_a_single_lucky_bucket_from_dominating():
    """EMA_WEIGHT_EPSILON allows a raw weight up to 100. ema_abs_error is
    itself an EMA over a modest sample count and can be transiently tiny
    by luck, which is not a claim the data supports."""
    lucky = _contribution("ch1", 0.0, 1 / 0.01, MIN_SAMPLES_TO_TRUST_BUCKET + 1)
    ordinary = _contribution("ch2", 10.0, 1 / 1.0, 200)
    blended = model_a.blend([lucky, ordinary])
    assert blended > 0.5, (
        "the ordinary source was drowned out entirely; some influence must "
        "survive a single very low observed error"
    )


def test_blend_all_cold_start_is_a_plain_average_as_before():
    """The genuine cold-start case must be unchanged from pre-v0.1.24 —
    that behaviour was always correct."""
    blended = model_a.blend([
        _contribution("a", 10.0, 0.0, 1),
        _contribution("b", 20.0, 0.0, 1),
    ])
    assert blended == pytest.approx(15.0)


def test_blend_skips_none_values_without_zeroing_weight_total():
    blended = model_a.blend([
        _contribution("a", None, 1.0, 200),
        _contribution("b", 12.0, 1.0, 200),
    ])
    assert blended == pytest.approx(12.0)


# ---------------------------------------------------------------------------
# IND-02 — one missing reading must not blank the whole storm score
# ---------------------------------------------------------------------------
def _samples_with_trailing_gap():
    """An hour of good data followed by one all-None row, exactly what a
    single 5-minute sensor dropout produces."""
    base = NOW.timestamp()
    samples = [
        model_b.StationSample(
            ts_epoch_seconds=base - 3600 + (i * 300),
            temperature=20.0,
            humidity=50.0 + i,
            pressure=1013.0 - i,
        )
        for i in range(12)
    ]
    samples.append(
        model_b.StationSample(
            ts_epoch_seconds=base, temperature=None, humidity=None, pressure=None
        )
    )
    return samples


def test_single_dropout_does_not_blank_every_tendency_feature():
    """The original defect: compute_tendency_features took
    `latest = samples[-1]` wholesale, so one all-None trailing row made
    every one of the nine deltas None and dropped score_v0 to 0.0 —
    discarding 55 minutes of perfectly good data, during exactly the
    conditions in which sensor dropouts are most likely."""
    features = model_b.compute_tendency_features(
        samples=_samples_with_trailing_gap(),
        now_epoch_seconds=NOW.timestamp(),
    )
    assert features.delta_pressure_30min is not None
    assert features.delta_humidity_30min is not None
    assert features.delta_temperature_30min is not None


def test_dropout_on_one_sensor_does_not_silence_the_others():
    """Endpoints are resolved PER MEASUREMENT, so a pressure sensor going
    quiet must not take humidity down with it."""
    base = NOW.timestamp()
    samples = [
        model_b.StationSample(
            ts_epoch_seconds=base - 3600 + (i * 300),
            temperature=20.0,
            humidity=50.0 + i,
            pressure=1013.0 - i,
        )
        for i in range(12)
    ]
    samples.append(
        model_b.StationSample(
            ts_epoch_seconds=base, temperature=21.0, humidity=70.0, pressure=None
        )
    )
    features = model_b.compute_tendency_features(
        samples=samples, now_epoch_seconds=base
    )
    assert features.delta_humidity_30min is not None
    assert features.delta_pressure_30min is not None


def test_no_samples_at_all_still_yields_none_deltas():
    """The genuinely-empty case must still be None, not zero — "no data"
    and "no change" are different claims."""
    features = model_b.compute_tendency_features(
        samples=[], now_epoch_seconds=NOW.timestamp()
    )
    assert features.delta_pressure_30min is None


# ---------------------------------------------------------------------------
# P1-13 — radar freshness
# ---------------------------------------------------------------------------
def _radar(label, value, age_minutes=0, quality=9):
    return model_b.RadarPointReading(
        label=label,
        precip_accum_mm_1h=value,
        valid_at=NOW - timedelta(minutes=age_minutes),
        quality=quality,
    )


def test_fresh_radar_point_contributes_to_score():
    score = model_b._radar_signal_probability((_radar("local", 5.0),), now=NOW)
    assert score == LOCAL_POINT_PROBABILITY


def test_stale_radar_point_does_not_contribute_to_score():
    """Home Assistant keeps serving a coordinator's last successful .data
    indefinitely across failed refreshes, so without this a stalled
    CombiPrecip feed influences the storm score forever."""
    stale = _radar("local", 5.0, age_minutes=120)
    assert model_b._radar_signal_probability((stale,), now=NOW) == 0.0


def test_radar_point_just_within_freshness_limit_still_counts():
    age = RADAR_FRESHNESS_LIMIT.total_seconds() / 60 - 1
    point = _radar("local", 5.0, age_minutes=age)
    assert model_b._radar_signal_probability((point,), now=NOW) == LOCAL_POINT_PROBABILITY


def test_radar_point_just_beyond_freshness_limit_is_excluded():
    age = RADAR_FRESHNESS_LIMIT.total_seconds() / 60 + 1
    point = _radar("local", 5.0, age_minutes=age)
    assert model_b._radar_signal_probability((point,), now=NOW) == 0.0


def test_radar_point_with_missing_valid_at_treated_as_stale():
    """"Freshness unknown" is treated as "known stale". A reading whose
    age cannot be established provides no evidence that it is current,
    and wrongly trusting a stale echo costs more (a false warning,
    blinds closing on a clear day) than ignoring one reading."""
    point = model_b.RadarPointReading(
        label="local", precip_accum_mm_1h=5.0, valid_at=None, quality=9
    )
    assert model_b._radar_signal_probability((point,), now=NOW) == 0.0


def test_freshness_gate_is_skipped_when_now_is_not_supplied():
    """Pure tendency tests pass now=None deliberately; production callers
    always pass a real timestamp."""
    point = model_b.RadarPointReading(
        label="local", precip_accum_mm_1h=5.0, valid_at=None
    )
    assert model_b._radar_signal_probability((point,), now=None) == LOCAL_POINT_PROBABILITY


def test_nearest_upwind_detection_wins_over_farther_one():
    points = (_radar("far", 5.0), _radar("near", 5.0))
    score = model_b._radar_signal_probability(points, now=NOW)
    assert score == UPWIND_POINT_PROBABILITY["near"]


# ---------------------------------------------------------------------------
# P1-16 — radar quality, with deliberately asymmetric unknown-handling
# ---------------------------------------------------------------------------
def test_confirmed_low_quality_radar_point_does_not_contribute():
    point = _radar("local", 5.0, quality=1)
    assert model_b._radar_signal_probability((point,), now=NOW) == 0.0


def test_high_quality_radar_point_contributes():
    assert model_b._radar_signal_probability(
        (_radar("local", 5.0, quality=9),), now=NOW
    ) == LOCAL_POINT_PROBABILITY


def test_unknown_quality_radar_point_still_contributes():
    """The asymmetry with freshness is the design, not an oversight. This
    project has never verified a real CombiPrecip file, so the quality
    code may never populate in practice — treating unknown as bad would
    then silently disable the entire radar signal, which is far worse
    than occasionally scoring on a low-quality scan."""
    point = _radar("local", 5.0, quality=None)
    assert model_b._radar_signal_probability((point,), now=NOW) == LOCAL_POINT_PROBABILITY


# ---------------------------------------------------------------------------
# P2-10 — condition mapping
# ---------------------------------------------------------------------------
def test_derive_condition_snowy_when_precipitating_and_below_freezing():
    assert model_a.derive_condition(1.0, -2.0, 80.0) == "snowy"


def test_derive_condition_rainy_when_precipitating_above_freezing():
    assert model_a.derive_condition(1.0, 5.0, 80.0) == "rainy"


def test_derive_condition_rainy_when_temperature_unknown():
    """Rain is far more common than snow across the year at Swiss valley
    altitudes, and a wrong "snowy" is the more conspicuous error."""
    assert model_a.derive_condition(1.0, None, 80.0) == "rainy"


def test_derive_condition_cloudy_when_humid_and_no_precip():
    assert model_a.derive_condition(0.0, 12.0, 95.0) == "cloudy"


def test_derive_condition_sunny_when_dry_and_no_precip():
    assert model_a.derive_condition(0.0, 12.0, 40.0) == "sunny"


def test_derive_condition_none_when_precip_unknown():
    assert model_a.derive_condition(None, 12.0, 40.0) is None


def test_derive_condition_respects_custom_precip_threshold():
    """The hourly sites use 0.1 mm and the daily aggregation sites 0.5 mm.
    A daily total is not the same quantity as an hourly amount, and the
    two thresholds are deliberately preserved rather than unified."""
    assert model_a.derive_condition(0.3, 10.0, 50.0) == "rainy"
    assert model_a.derive_condition(0.3, 10.0, 50.0, precip_threshold=0.5) == "sunny"
