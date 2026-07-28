from datetime import datetime, timedelta, timezone

from swissweather_fusion.models import model_a


def test_derive_season():
    assert model_a.derive_season(datetime(2026, 1, 15)) == "DJF"
    assert model_a.derive_season(datetime(2026, 4, 15)) == "MAM"
    assert model_a.derive_season(datetime(2026, 7, 15)) == "JJA"
    assert model_a.derive_season(datetime(2026, 11, 15)) == "SON"


def test_derive_lead_time_bucket():
    issued = datetime(2026, 7, 25, 6, 0, tzinfo=timezone.utc)
    assert model_a.derive_lead_time_bucket(issued, issued + timedelta(hours=3)) == "short"
    assert model_a.derive_lead_time_bucket(issued, issued + timedelta(hours=48)) == "medium"
    assert model_a.derive_lead_time_bucket(issued, issued + timedelta(hours=100)) == "long"


def test_update_bucket_ema_cold_start():
    result = model_a.update_bucket_ema(
        previous_bias=0.0, previous_abs_error=0.0, previous_sample_count=0,
        forecast_value=20.0, actual_value=18.0, lead_time_bucket="short",
    )
    assert result.sample_count == 1
    assert result.ema_bias == 2.0  # forecast - actual, no prior to blend with


def test_update_bucket_ema_moves_toward_new_observation():
    first = model_a.update_bucket_ema(
        previous_bias=0.0, previous_abs_error=0.0, previous_sample_count=0,
        forecast_value=20.0, actual_value=18.0, lead_time_bucket="short",
    )
    second = model_a.update_bucket_ema(
        previous_bias=first.ema_bias, previous_abs_error=first.ema_abs_error,
        previous_sample_count=first.sample_count,
        forecast_value=20.0, actual_value=20.0, lead_time_bucket="short",
    )
    assert second.sample_count == 2
    # EMA, not a full replace: moves toward the new (zero) error but doesn't jump there
    assert 0.0 < second.ema_bias < first.ema_bias


def test_blend_debiases_before_weighting():
    contributions = [
        model_a.SourceContribution(source="ch1", raw_value=20.0, ema_bias=2.0, ema_weight=5.0, sample_count=10),
        model_a.SourceContribution(source="ch2", raw_value=22.0, ema_bias=0.0, ema_weight=1.0, sample_count=2),
    ]
    blended = model_a.blend(contributions)
    # ch2 is below MIN_SAMPLES_TO_TRUST_BUCKET -> raw value, neutral weight 1.0
    expected = (18.0 * 5.0 + 22.0 * 1.0) / (5.0 + 1.0)
    assert abs(blended - expected) < 1e-9


def test_blend_empty_returns_none():
    assert model_a.blend([]) is None


def test_lapse_rate_precorrection():
    corrected = model_a.apply_lapse_rate_precorrection(
        raw_temperature=20.0, source_grid_elevation_m=600.0, actual_elevation_m=400.0
    )
    # grid 200m higher than actual -> source reads colder than reality -> add back
    assert abs(corrected - 21.3) < 1e-9
