"""Tests for v0.1.24's two new pure-logic modules.

unit_conversion.py (P1-21, P1-22) and provider_validation.py (P1-23).
Both are deliberately HA-free, so these are ordinary unit tests with no
stubbing involved.
"""
import math

import pytest

from swissweather_fusion import provider_validation as pv
from swissweather_fusion import unit_conversion as uc


# ---------------------------------------------------------------------------
# P1-21 — unit conversion
# ---------------------------------------------------------------------------
def test_temperature_fahrenheit_converted_correctly():
    """68 °F is exactly 20 °C — an exact-value check, not an approximation,
    so a wrong formula cannot hide inside a tolerance."""
    assert uc.convert_temperature_to_celsius(68.0, "°F") == pytest.approx(20.0)


def test_temperature_kelvin_converted_correctly():
    assert uc.convert_temperature_to_celsius(273.15, "K") == pytest.approx(0.0)


def test_temperature_missing_unit_treated_as_celsius():
    """Preserves prior behaviour for entities that don't populate
    unit_of_measurement — of which there are many. Changing this to a
    rejection would break working installations on upgrade."""
    assert uc.convert_temperature_to_celsius(20.0, None) == 20.0
    assert uc.convert_temperature_to_celsius(20.0, "") == 20.0


def test_temperature_unrecognised_unit_rejected_not_guessed():
    """Explicit rejection, not a silent pass-through. An unrecognised unit
    means we do not know what the number means, and a wrong number is
    worse than no number for a value that feeds a learning loop."""
    assert uc.convert_temperature_to_celsius(20.0, "furlongs") is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_temperature_non_finite_rejected(bad):
    assert uc.convert_temperature_to_celsius(bad, "°C") is None


def test_pressure_inhg_converted_correctly():
    # 29.92 inHg is the standard sea-level pressure, ~1013.25 hPa.
    assert uc.convert_pressure_to_hpa(29.92, "inHg") == pytest.approx(1013.2, abs=0.5)


def test_pressure_pascal_converted_correctly():
    assert uc.convert_pressure_to_hpa(101325.0, "Pa") == pytest.approx(1013.25)


def test_pressure_unrecognised_unit_rejected():
    assert uc.convert_pressure_to_hpa(1013.0, "bananas") is None


def test_humidity_percent_passthrough_and_rejection():
    assert uc.convert_humidity_to_percent(55.0, "%") == 55.0
    assert uc.convert_humidity_to_percent(55.0, "kg") is None


# ---------------------------------------------------------------------------
# P1-22 — sea-level reduction
# ---------------------------------------------------------------------------
def test_sea_level_reduction_increases_pressure_at_elevation():
    """Direction check. A station above sea level always reads LOWER than
    the sea-level equivalent, so reduction must increase the value —
    getting the sign backwards would double the error rather than remove
    it, and would still look plausible."""
    reduced = uc.reduce_station_pressure_to_sea_level(950.0, 500.0, 15.0)
    assert reduced > 950.0


def test_sea_level_reduction_plausible_magnitude():
    """A physically sane bound on the formula, not a precise empirical
    target: roughly 60 hPa of correction at 500 m."""
    reduced = uc.reduce_station_pressure_to_sea_level(950.0, 500.0, 15.0)
    assert 50.0 <= (reduced - 950.0) <= 70.0


def test_sea_level_reduction_uses_reference_temperature_when_unknown():
    """Defaulting rather than refusing: discarding the pressure reading
    entirely because no temperature was available would be a worse
    outcome than reducing it with the ISA standard reference."""
    with_temp = uc.reduce_station_pressure_to_sea_level(950.0, 500.0, 15.0)
    without_temp = uc.reduce_station_pressure_to_sea_level(950.0, 500.0, None)
    assert without_temp == pytest.approx(with_temp)


def test_sea_level_reduction_at_zero_elevation_is_identity():
    assert uc.reduce_station_pressure_to_sea_level(1013.0, 0.0, 15.0) == 1013.0


def test_sea_level_reduction_none_pressure_returns_none():
    assert uc.reduce_station_pressure_to_sea_level(None, 500.0, 15.0) is None


def test_sea_level_reduction_survives_absurd_temperature():
    """Below absolute zero is physically impossible input; the formula
    must not blow up or return a nonsense exponent."""
    result = uc.reduce_station_pressure_to_sea_level(950.0, 500.0, -400.0)
    assert result is not None and math.isfinite(result) and result > 950.0


# ---------------------------------------------------------------------------
# P1-23 — provider validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "variable,value",
    [
        ("temperature", 20.0),
        ("temperature", -59.0),
        ("humidity", 0.0),
        ("humidity", 100.0),
        ("pressure", 1013.0),
        ("precipitation", 0.0),
        ("wind_speed", 12.0),
    ],
)
def test_validate_forecast_value_accepts_plausible_values(variable, value):
    assert pv.validate_forecast_value(variable, value) == value


@pytest.mark.parametrize(
    "variable,value",
    [
        ("temperature", 200.0),
        ("temperature", -100.0),
        ("humidity", 150.0),
        ("humidity", -1.0),
        ("pressure", 5.0),
        ("pressure", 100000.0),
        ("precipitation", -1.0),
        ("wind_speed", 900.0),
    ],
)
def test_validate_forecast_value_rejects_out_of_bounds(variable, value):
    assert pv.validate_forecast_value(variable, value) is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_validate_forecast_value_rejects_non_finite(bad):
    """The case that motivated this module: a single non-finite sample
    permanently distorts an EMA bucket, and an EMA has no mechanism for
    forgetting a value it has already absorbed."""
    assert pv.validate_forecast_value("temperature", bad) is None


def test_validate_forecast_value_passes_none_through():
    """None is already the representation for "provider had no data for
    this hour" and must survive untouched."""
    assert pv.validate_forecast_value("temperature", None) is None


def test_validate_forecast_value_unknown_variable_is_bounded_only_by_finiteness():
    """An unknown variable means "added since these bounds were written";
    silently rejecting it would be worse than storing it."""
    assert pv.validate_forecast_value("cloud_cover", 42.0) == 42.0
    assert pv.validate_forecast_value("cloud_cover", float("inf")) is None


def test_validate_forecast_rows_preserves_row_count_and_shape():
    """A rejected value becomes None; the ROW stays. Every downstream
    consumer already handles a None-valued row, so reusing that
    representation means no new code path anywhere — and it preserves the
    evidence that the provider did return something for that hour."""
    rows = [
        ("ch1", "i", "v1", "temperature", 20.0, "scheduled"),
        ("ch1", "i", "v2", "temperature", 9999.0, "scheduled"),
        ("ch1", "i", "v3", "temperature", None, "scheduled"),
    ]
    validated, rejected = pv.validate_forecast_rows(rows)
    assert len(validated) == 3
    assert rejected == 1
    assert validated[0][4] == 20.0
    assert validated[1][4] is None
    assert validated[2][4] is None
    # Every non-value field is untouched.
    assert [r[:4] for r in validated] == [r[:4] for r in rows]


def test_validate_forecast_rows_does_not_count_preexisting_none_as_rejected():
    rows = [("ch1", "i", "v", "temperature", None, "scheduled")]
    _, rejected = pv.validate_forecast_rows(rows)
    assert rejected == 0


def test_validate_forecast_rows_passes_unexpected_shapes_through_untouched():
    """Schema enforcement is storage's job, not this module's. A
    malformed row is passed on so it fails loudly there rather than being
    silently reshaped here."""
    rows = [("too", "few")]
    validated, rejected = pv.validate_forecast_rows(rows)
    assert validated == rows
    assert rejected == 0
