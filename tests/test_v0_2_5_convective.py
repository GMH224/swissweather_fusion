"""Tests for v0.2.5 — convective parameters, Class D, and deprecations.

The headline change is that Model B has an instability input for the
first time. Until now it was a rain-APPROACH detector: station tendency
plus upwind radar, with nothing describing whether the atmosphere could
actually convect. It could not distinguish steady frontal rain arriving
from the southwest from a thunderstorm developing overhead.

CAPE was recorded in const.py and model_b.py as unavailable ("hoped for
but turned out to require a paid tier") — a conclusion drawn from
Meteonomiqs' /forecast2 and then carried for several releases without
being re-checked against the other providers. Open-Meteo supplies it free
for all three ICON models.
"""
import pytest

from swissweather_fusion import forecast_parameters as fp
from swissweather_fusion.clients import open_meteo
from swissweather_fusion.const import (
    CAPE_MARGINAL_JKG,
    CAPE_MODERATE_JKG,
    CAPE_STRONG_JKG,
    CIN_STRONG_CAP_JKG,
    V0_TRIGGER_PROBABILITY,
)
from swissweather_fusion.models import model_b


def _features(**kwargs):
    """TendencyFeatures with all nine deltas explicitly absent.

    The deltas are required positional fields, so tests that care only
    about instability or radar must state that the tendency half is
    empty rather than relying on defaults.
    """
    deltas = {
        f"delta_{measure}_{window}min": None
        for measure in ("pressure", "humidity", "temperature")
        for window in (10, 30, 60)
    }
    deltas.update(kwargs)
    return model_b.TendencyFeatures(**deltas)


# ---------------------------------------------------------------------------
# SWF-025-001 — convective scoring
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "cape,expected_nonzero",
    [(0, False), (100, False), (400, True), (1500, True), (3000, True)],
)
def test_cape_bands_produce_a_graduated_signal(cape, expected_nonzero):
    result = model_b.convective_probability(cape, 0.0)
    assert (result > 0) is expected_nonzero


def test_signal_increases_with_instability():
    weak = model_b.convective_probability(CAPE_MARGINAL_JKG, 0.0)
    moderate = model_b.convective_probability(CAPE_MODERATE_JKG, 0.0)
    strong = model_b.convective_probability(CAPE_STRONG_JKG, 0.0)
    assert weak < moderate < strong


def test_strong_cap_suppresses_high_cape_entirely():
    """The classic false-alarm case: the energy is there and nothing can
    reach it. A convective signal that ignores the lid would cry wolf on
    every warm afternoon, which is how a warning becomes ignored."""
    assert model_b.convective_probability(3000, CIN_STRONG_CAP_JKG - 50) == 0.0


def test_unknown_inhibition_does_not_suppress():
    """Same asymmetry as radar quality (P1-16): treating unknown as
    capped would silently discard the whole contribution whenever a
    provider omits one variable."""
    assert model_b.convective_probability(3000, None) > 0.0


def test_instability_alone_cannot_fire_the_storm_trigger():
    """CAPE describes POTENTIAL, not an event in progress. It should
    raise concern and support a radar or tendency signal, never trigger
    on a merely unstable afternoon."""
    assert model_b.convective_probability(9999, 0.0) < V0_TRIGGER_PROBABILITY


def test_missing_cape_yields_zero_not_none():
    """Combined with max() against the other signals, so None would force
    every caller to special-case it."""
    assert model_b.convective_probability(None, 0.0) == 0.0


def test_graduated_score_takes_the_max_of_three_independent_signals():
    """max(), not a sum: three lines of evidence for the same event, not
    additive contributions to it. Summing would let three weak signals
    manufacture a strong one."""
    features = _features(cape=3000, convective_inhibition=0.0)
    score = model_b.score_v0_graduated(features)
    assert score == pytest.approx(model_b.convective_probability(3000, 0.0))


def test_convective_signal_does_not_override_a_stronger_radar_signal():
    from datetime import datetime, timezone

    now = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)
    features = _features(
        radar_points=(
            model_b.RadarPointReading(
                label="local", precip_accum_mm_1h=5.0, valid_at=now, quality=9
            ),
        ),
        cape=400,
        convective_inhibition=0.0,
    )
    # Rain already falling here must outrank a marginal instability.
    assert model_b.score_v0_graduated(features, now=now) > 0.8


def test_features_default_to_no_instability():
    """Absent CAPE must leave pre-v0.2.5 behaviour untouched."""
    features = _features()
    assert features.cape is None
    assert model_b.score_v0_graduated(features) == 0.0


# ---------------------------------------------------------------------------
# Acquisition and fusion strategies
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "variable",
    ["cape", "convective_inhibition", "freezing_level_height",
     "snowfall_height", "cloud_base"],
)
def test_convective_variables_are_requested_and_mapped(variable):
    assert variable in open_meteo.HOURLY_VARIABLES
    assert variable in open_meteo._VARIABLE_NAME_MAP.values()
    assert fp.get(variable) is not None


def test_cape_is_fused_with_max_not_mean():
    """Like a wind gust, CAPE is a hazard indicator. Averaging away one
    model's warning is the wrong direction to be wrong in."""
    values = [500.0, 2400.0, 900.0]
    result = fp.get("cape").fuse_values(values)
    assert result == 2400.0
    assert result != pytest.approx(sum(values) / len(values))


def test_inhibition_fuses_toward_the_least_capped_model():
    """Consistent with CAPE: the pessimistic case is a weak cap. CIN is
    negative, so max() is the least inhibited."""
    assert fp.get("convective_inhibition").fuse_values([-200.0, -10.0]) == -10.0


def test_freezing_level_uses_the_mean():
    """A smooth continuous field, unlike the hazard indicators."""
    assert fp.get("freezing_level_height").fuse_values(
        [2000.0, 2200.0]
    ) == pytest.approx(2100.0)


def test_all_new_parameters_reach_the_blend():
    """The reachability guard that caught these when they were first
    registered but not wired."""
    from swissweather_fusion.coordinator import ModelABlendCoordinator

    missing = set(fp.fused_parameters()) - set(ModelABlendCoordinator.MEASUREMENTS)
    assert not missing, f"registered but never fused: {sorted(missing)}"


# ---------------------------------------------------------------------------
# SWF-025-002 — Class D provider confidence
# ---------------------------------------------------------------------------
def test_predictability_is_registered_as_a_fusable_parameter():
    """meteoblue's own hourly confidence score, parsed since v0.1.x into
    ParsedMeteoblueForecast.predictability and discarded ever since — the
    architecture document's Class D gap."""
    assert fp.get("predictability") is not None
    assert fp.get("predictability").maximum == 100


def test_predictability_is_persisted_by_the_meteoblue_coordinator():
    import inspect

    from swissweather_fusion import coordinator as coord

    source = inspect.getsource(coord.MeteoblueCoordinator)
    assert 'parsed.predictability' in source
    assert '"predictability"' in source


# ---------------------------------------------------------------------------
# SWF-025-004 — daily cloud coverage
# ---------------------------------------------------------------------------
def test_daily_forecast_carries_cloud_coverage():
    """It was computed to feed the condition resolver and then discarded,
    so an automation reading daily.cloud_coverage silently got a template
    default rather than an error — the worst way for a field to be
    missing."""
    from datetime import datetime, timedelta, timezone

    from swissweather_fusion.models import model_a

    base = datetime(2026, 9, 3, tzinfo=timezone.utc)
    entries = [
        {
            "datetime": (base + timedelta(hours=h)).isoformat(),
            "native_temperature": 18.0,
            "native_precipitation": 0.0,
            "cloud_coverage": 40.0 + h,
        }
        for h in range(24)
    ]
    days = model_a.aggregate_daily_forecast(entries, local_tz=timezone.utc)
    assert days[0]["cloud_coverage"] == pytest.approx(63.0)


# ---------------------------------------------------------------------------
# SWF-025-005 — deprecated Home Assistant APIs
# ---------------------------------------------------------------------------
def test_no_deprecated_helper_entity_imports():
    """EntityCategory belongs to homeassistant.const and DeviceInfo to
    the device registry helper; the helpers.entity aliases are
    deprecated. Baseline for this project is HA 2026.9, so there is no
    backward-compatibility reason to keep them."""
    import pathlib

    root = pathlib.Path(__file__).parent.parent / "custom_components" / "swissweather_fusion"
    offenders = [
        str(f) for f in root.rglob("*.py")
        if "from homeassistant.helpers.entity import" in f.read_text()
    ]
    assert not offenders, f"deprecated imports in: {offenders}"


def test_platforms_use_the_config_entry_entities_callback():
    """AddEntitiesCallback is superseded by the config-entry-specific
    callback type in current Home Assistant."""
    import pathlib

    root = pathlib.Path(__file__).parent.parent / "custom_components" / "swissweather_fusion"
    for name in ("sensor.py", "binary_sensor.py", "weather.py", "button.py"):
        text = (root / name).read_text()
        assert "AddConfigEntryEntitiesCallback" in text, name


# ---------------------------------------------------------------------------
# UI exposure
# ---------------------------------------------------------------------------
def test_new_parameters_have_their_own_entities():
    """Several are not members of Home Assistant's Forecast contract, so
    no weather card can render them — sensors are the mechanism the
    architecture review identified for exactly this case (AR-01)."""
    import inspect

    from swissweather_fusion import sensor

    setup = inspect.getsource(sensor.async_setup_entry)
    for measurement in (
        "cape", "convective_inhibition", "freezing_level_height",
        "snowfall_height", "cloud_base", "predictability",
    ):
        assert f'"{measurement}"' in setup, f"{measurement} has no entity"
    assert "ConvectiveRiskSensor" in setup


def test_convective_risk_sensor_reports_capped_state():
    """A high-CAPE, strongly-capped atmosphere must read zero and say
    why, rather than looking like a quiet day."""
    from swissweather_fusion.sensor import ConvectiveRiskSensor

    s = object.__new__(ConvectiveRiskSensor)
    s._runtime = {
        "blend_coordinator": type("C", (), {
            "data": {"current": {"cape": 3000, "convective_inhibition": -200}}
        })()
    }
    assert ConvectiveRiskSensor.native_value.fget(s) == 0.0
    attrs = ConvectiveRiskSensor.extra_state_attributes.fget(s)
    assert attrs["capped"] is True
    assert attrs["is_calibrated_probability"] is False


def test_blended_value_sensor_is_blank_before_the_first_blend():
    from swissweather_fusion.sensor import BlendedValueSensor

    s = object.__new__(BlendedValueSensor)
    s._runtime = {}
    s._measurement = "cape"
    assert BlendedValueSensor.native_value.fget(s) is None
