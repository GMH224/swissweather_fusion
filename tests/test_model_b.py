from swissweather_fusion.models import model_b

BASE_EPOCH = 1_000_000.0


def _storm_like_samples():
    return [
        model_b.StationSample(
            ts_epoch_seconds=BASE_EPOCH + i * 300,
            temperature=20.0 - i * 0.1,
            humidity=50.0 + i * 3.0,
            pressure=1015.0 - i * 0.5,
        )
        for i in range(13)
    ]


def _calm_samples():
    return [
        model_b.StationSample(
            ts_epoch_seconds=BASE_EPOCH + i * 300, temperature=20.0, humidity=50.0, pressure=1015.0
        )
        for i in range(13)
    ]


NOW = BASE_EPOCH + 12 * 300


def test_tendency_features_detect_storm_signature():
    features = model_b.compute_tendency_features(samples=_storm_like_samples(), now_epoch_seconds=NOW)
    assert features.delta_pressure_30min is not None and features.delta_pressure_30min < 0
    assert features.delta_humidity_30min is not None and features.delta_humidity_30min > 0


def test_score_v0_fires_on_storm_signature():
    features = model_b.compute_tendency_features(samples=_storm_like_samples(), now_epoch_seconds=NOW)
    assert model_b.score_v0(features) > 0


def test_score_v0_stays_zero_on_calm_data():
    features = model_b.compute_tendency_features(samples=_calm_samples(), now_epoch_seconds=NOW)
    assert model_b.score_v0(features) == 0.0


def test_insufficient_history_does_not_raise():
    features = model_b.compute_tendency_features(samples=_calm_samples()[-1:], now_epoch_seconds=NOW)
    assert features.delta_pressure_30min is None
    assert model_b.score_v0(features) == 0.0


def test_graduated_score_radar_far_point():
    radar = (
        model_b.RadarPointReading(label="local", precip_rate_mmh=0.0),
        model_b.RadarPointReading(label="near", precip_rate_mmh=0.0),
        model_b.RadarPointReading(label="mid", precip_rate_mmh=0.0),
        model_b.RadarPointReading(label="far", precip_rate_mmh=3.5),
    )
    features = model_b.compute_tendency_features(samples=_calm_samples(), now_epoch_seconds=NOW, radar_points=radar)
    assert model_b.score_v0_graduated(features) == 0.30


def test_graduated_score_near_point_beats_far_point():
    near_radar = (model_b.RadarPointReading(label="near", precip_rate_mmh=2.0),)
    far_radar = (model_b.RadarPointReading(label="far", precip_rate_mmh=2.0),)
    features_near = model_b.compute_tendency_features(
        samples=_calm_samples(), now_epoch_seconds=NOW, radar_points=near_radar
    )
    features_far = model_b.compute_tendency_features(
        samples=_calm_samples(), now_epoch_seconds=NOW, radar_points=far_radar
    )
    assert model_b.score_v0_graduated(features_near) > model_b.score_v0_graduated(features_far)


def test_graduated_score_local_precip_is_highest():
    local_radar = (model_b.RadarPointReading(label="local", precip_rate_mmh=5.0),)
    features = model_b.compute_tendency_features(samples=_calm_samples(), now_epoch_seconds=NOW, radar_points=local_radar)
    assert model_b.score_v0_graduated(features) == 0.90


def test_graduated_score_takes_max_not_sum():
    radar = (model_b.RadarPointReading(label="near", precip_rate_mmh=2.0),)
    features = model_b.compute_tendency_features(samples=_storm_like_samples(), now_epoch_seconds=NOW, radar_points=radar)
    combined = model_b.score_v0_graduated(features)
    assert combined == max(model_b.score_v0(features), 0.75)
    assert combined <= 1.0  # never exceeds 1.0 by summing signals


def test_refine_with_meteonomiqs_pulls_toward_independent_signal():
    up = model_b.refine_with_meteonomiqs(base_probability=0.5, meteonomiqs_risk_value=9)
    down = model_b.refine_with_meteonomiqs(base_probability=0.5, meteonomiqs_risk_value=0)
    unchanged = model_b.refine_with_meteonomiqs(base_probability=0.5, meteonomiqs_risk_value=None)
    assert up > 0.5
    assert down < 0.5
    assert unchanged == 0.5


def test_cross_model_trigger_fires_only_on_upward_crossing():
    fires = model_b.evaluate_cross_model_trigger(previous_probability=0.0, current_probability=0.7, threshold=0.5)
    already_above = model_b.evaluate_cross_model_trigger(previous_probability=0.7, current_probability=0.72, threshold=0.5)
    downward = model_b.evaluate_cross_model_trigger(previous_probability=0.7, current_probability=0.3, threshold=0.5)

    assert fires.should_trigger is True
    assert already_above.should_trigger is False
    assert downward.should_trigger is False
