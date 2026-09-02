"""Tests for v0.2.0 — Model A forecast expansion (Stage 1a).

Covers the parameter registry, per-parameter fusion strategies, the
condition resolver, and the expanded provider acquisition.

**These tests are written to fail if the strategy is replaced by a mean.**
That is the standard §11 of the architecture review sets, and it matters
here more than anywhere: a test asserting only "a number came out" would
pass for every strategy, including the wrong one. Each fusion test states
what the arithmetic mean would have produced and asserts the result
differs from it.
"""
import pytest

from swissweather_fusion import forecast_parameters as fp
from swissweather_fusion.clients import open_meteo, srf
from swissweather_fusion.models import model_a


# ---------------------------------------------------------------------------
# Registry structure
# ---------------------------------------------------------------------------
def test_learned_set_is_exactly_what_the_station_can_reconcile():
    """Class A is defined by the existence of local ground truth, not by
    convenience. The station measures temperature, humidity and pressure;
    nothing else may claim a learned bias."""
    assert set(fp.learned_parameters()) == {"temperature", "humidity", "pressure"}


def test_no_class_b_parameter_claims_to_be_learned():
    for name in fp.fused_parameters():
        assert fp.get(name).parameter_class is fp.ParameterClass.FUSED


def test_every_registered_parameter_declares_a_fusion_strategy():
    for name, parameter in fp.PARAMETERS.items():
        assert callable(parameter.fuse), f"{name} has no fusion strategy"


def test_every_registered_parameter_has_physical_bounds():
    """A parameter without bounds silently accepts a provider's garbage."""
    for name, parameter in fp.PARAMETERS.items():
        assert parameter.minimum is not None, f"{name} has no lower bound"
        assert parameter.maximum is not None, f"{name} has no upper bound"


# ---------------------------------------------------------------------------
# Fusion strategies — each asserts it is NOT the mean
# ---------------------------------------------------------------------------
def test_precipitation_uses_median_not_mean():
    """The zero-inflation problem. Two models say dry, one says 8 mm.
    The mean invents 2.67 mm of drizzle that no model forecast; the
    median reports the consensus."""
    values = [0.0, 0.0, 8.0]
    mean = sum(values) / len(values)
    result = fp.get("precip").fuse_values(values)
    assert result == 0.0
    assert result != pytest.approx(mean)


def test_precipitation_still_agrees_with_the_mean_when_models_agree():
    """The median must not cost anything in the ordinary case."""
    result = fp.get("precip").fuse_values([6.0, 8.0, 10.0])
    assert result == pytest.approx(8.0)


def test_wind_gusts_use_max_not_mean():
    """A gust forecast is already a peak. The mean of several peaks is
    not a peak, and it understates the hazard."""
    values = [12.0, 25.0, 18.0]
    mean = sum(values) / len(values)
    result = fp.get("wind_gust_speed").fuse_values(values)
    assert result == 25.0
    assert result > mean


def test_visibility_uses_min_not_mean():
    """The worst case is the operationally relevant one."""
    assert fp.get("visibility").fuse_values([20000.0, 2000.0]) == 2000.0


def test_snowfall_requires_two_sources():
    """A lone model forecasting snow against several forecasting rain is
    exactly when a confident answer is least warranted."""
    assert fp.get("snowfall").fuse_values([10.0]) is None
    assert fp.get("snowfall").fuse_values([8.0, 10.0]) == pytest.approx(9.0)


def test_precip_probability_does_use_the_mean():
    """Averaging is defensible here — it is a genuine probability. This
    test exists so the per-parameter choice is visible as a choice."""
    assert fp.get("precip_probability").fuse_values([20.0, 40.0]) == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# Wind bearing — circular, not linear
# ---------------------------------------------------------------------------
def test_wind_bearing_circular_mean_across_north():
    """The arithmetic mean of 350 and 10 is 180 — due south when both
    sources say due north. This is the single most wrong answer linear
    averaging can produce, and it is the common case (northerly wind)."""
    assert fp.fuse_wind_bearing([350.0, 10.0]) == pytest.approx(0.0)


def test_wind_bearing_ordinary_case():
    assert fp.fuse_wind_bearing([270.0, 290.0]) == pytest.approx(280.0)


def test_wind_bearing_returns_none_when_directions_cancel():
    """Opposing directions have no meaningful mean. Inventing one would
    be worse than admitting there isn't one."""
    assert fp.fuse_wind_bearing([0.0, 180.0]) is None


def test_wind_bearing_ignores_unusable_values():
    assert fp.fuse_wind_bearing([None, "x", 90.0]) == pytest.approx(90.0)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), None, "abc"])
def test_unusable_values_are_dropped_before_fusion(bad):
    assert fp.get("temperature").fuse_values([bad, 20.0]) == pytest.approx(20.0)


def test_out_of_bounds_values_are_dropped_not_clamped():
    """Clamping 999 °C to 60 °C would invent a heatwave from a parse
    error. Dropping it lets the remaining sources answer."""
    assert fp.get("temperature").fuse_values([999.0, 20.0]) == pytest.approx(20.0)


def test_all_values_unusable_yields_none():
    assert fp.get("temperature").fuse_values([float("nan"), None]) is None


# ---------------------------------------------------------------------------
# Condition resolver — stated evidence beats inference
# ---------------------------------------------------------------------------
def test_explicit_snowfall_overrides_the_temperature_guess():
    """The headline case for "do not infer what the model states".

    derive_condition() decides snow from `temperature <= 0`. A model
    reporting 2 cm of snowfall at +3 °C — wet snow, entirely real — was
    previously reported as rain. Stated snowfall now settles it.
    """
    assert model_a.resolve_condition(
        snowfall=2.0, precip=2.0, temperature=3.0
    ) == "snowy"
    # The old inference, for contrast.
    assert model_a.derive_condition(2.0, 3.0, 90.0) == "rainy"


@pytest.mark.parametrize(
    "code,expected",
    [
        (0, "sunny"), (2, "partlycloudy"), (3, "cloudy"),
        (45, "fog"), (65, "pouring"), (71, "snowy"),
        (95, "lightning"), (96, "lightning-rainy"),
    ],
)
def test_provider_weather_code_wins_and_unlocks_new_conditions(code, expected):
    """fog, lightning and pouring cannot be derived from precipitation
    and temperature at all. They are only available because the provider
    states them — which is the whole argument for the expansion."""
    assert model_a.resolve_condition(weather_code=code) == expected


def test_unrecognised_weather_code_falls_through_rather_than_guessing():
    assert model_a.condition_from_weather_code(1234) is None
    # ...and the resolver then uses the next-best evidence.
    assert model_a.resolve_condition(weather_code=1234, precip=5.0, temperature=10.0) == "rainy"


def test_measured_cloud_cover_replaces_the_humidity_proxy():
    """DEVELOPER.md labels the humidity-implies-cloudy rule
    "plausible but unvalidated". Measured cover is neither."""
    assert model_a.resolve_condition(precip=0.0, cloud_coverage=90.0) == "cloudy"
    assert model_a.resolve_condition(precip=0.0, cloud_coverage=50.0) == "partlycloudy"
    assert model_a.resolve_condition(precip=0.0, cloud_coverage=5.0) == "sunny"


def test_clear_night_substitution_survives_every_resolution_path():
    """v0.1.28's SWF-P2-005 must not regress through the new paths."""
    assert model_a.resolve_condition(weather_code=0, is_daytime=False) == "clear-night"
    assert model_a.resolve_condition(
        precip=0.0, cloud_coverage=5.0, is_daytime=False
    ) == "clear-night"
    assert model_a.resolve_condition(
        precip=0.0, humidity=40.0, is_daytime=False
    ) == "clear-night"


def test_resolver_falls_back_to_the_v0_inference_when_nothing_better_exists():
    """Sources that provide none of the new fields must keep working
    exactly as before."""
    assert model_a.resolve_condition(precip=0.0, humidity=95.0) == "cloudy"
    assert model_a.resolve_condition(precip=None) is None


# ---------------------------------------------------------------------------
# Provider acquisition
# ---------------------------------------------------------------------------
def test_open_meteo_requests_the_expanded_variable_set():
    """These are all free-tier hourly variables on the same request, so
    the expansion costs no additional API calls or quota."""
    for expected in (
        "snowfall", "weather_code", "precipitation_probability",
        "wind_gusts_10m", "dew_point_2m", "cloud_cover", "visibility",
    ):
        assert expected in open_meteo.HOURLY_VARIABLES


def test_open_meteo_maps_new_variables_to_the_common_vocabulary():
    """Provider names must be translated into the names the registry
    defines, or fusion silently finds nothing."""
    parsed = open_meteo.parse_forecast_response(
        {"hourly": {
            "time": ["2026-09-02T12:00"],
            "snowfall": [1.5],
            "wind_gusts_10m": [22.0],
            "weather_code": [95],
        }}
    )
    names = {p.variable for p in parsed.points}
    assert {"snowfall", "wind_gust_speed", "weather_code"} <= names


def test_every_open_meteo_mapped_name_exists_in_the_registry():
    """Guards the seam between acquisition and fusion: a mapped name with
    no registry entry would be stored and never fused."""
    from swissweather_fusion.clients.open_meteo import _VARIABLE_NAME_MAP

    unregistered = {
        name for name in _VARIABLE_NAME_MAP.values()
        if fp.get(name) is None and name != "weather_code"
    }
    assert not unregistered, f"mapped but unregistered: {sorted(unregistered)}"


def test_srf_fresh_snow_stays_namespaced_because_its_unit_differs():
    """FRESHSNOW_MM is millimetres; the common `snowfall` parameter is
    centimetres. Promoting it would silently mix units by a factor of
    ten — a rename is not a unit conversion."""
    assert srf._HOURLY_SIMPLE_FIELD_MAP["FRESHSNOW_MM"] == "srf_freshsnow"


def test_srf_promoted_fields_use_the_common_vocabulary():
    promoted = {
        srf._HOURLY_SIMPLE_FIELD_MAP[key]
        for key in ("DEWPOINT_C", "TTTFEEL_C", "PROBPCP_PERCENT", "DD_DEG")
    }
    assert promoted == {
        "dew_point", "apparent_temperature", "precip_probability", "wind_bearing"
    }
    for name in promoted:
        assert fp.get(name) is not None, f"{name} promoted but not registered"
