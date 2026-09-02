"""Regression tests for v0.2.3 — station pressure cross-check.

The v0.2.1 plausibility check bounded the processed reading at
870-1085 hPa: correct for "is this physically possible", useless for "is
this correctly configured".

A sea-level-normalised reading reduced to sea level a second time gains
about 65 hPa at 540 m. On a 1024 hPa day that lands at 1090 and is
caught. On a 1010 hPa day it lands at 1075 and is not. **The error is
identical; only the weather differs** — so whether the defect was
detected came down to luck.

These tests fix the detection at the level of the error rather than the
level of the resulting number.
"""
import asyncio
from datetime import datetime, timezone

import pytest

from swissweather_fusion import coordinator as coord
from swissweather_fusion.const import (
    PRESSURE_PLAUSIBLE_MAX_HPA,
    STATION_PRESSURE_REFERENCE_TOLERANCE_HPA,
)
from swissweather_fusion.storage.db import SwissWeatherDB


class FakeHass:
    def __init__(self, states=None):
        self.data = {}
        self._states = states or {}
        outer = self

        class States:
            def get(self, entity_id):
                return outer._states.get(entity_id)

        self.states = States()

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class FakeState:
    def __init__(self, state, unit=None):
        self.state = state
        self.attributes = {"unit_of_measurement": unit} if unit else {}


@pytest.fixture
def db(tmp_path):
    database = SwissWeatherDB(str(tmp_path / "xcheck.db"))
    yield database
    database.close()


def _seed_providers(db, hpa, hour=None):
    """Provider MSL forecasts for the current hour."""
    hour = hour or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    for source in ("ch1", "ch2", "icon_d2", "srf", "meteoblue"):
        db.insert_forecast_snapshot(
            source, "2026-09-02T00:00:00+00:00", f"{hour}:00:00+00:00",
            "pressure", hpa,
        )


def _station(db, hass, *, sea_level=False, elevation=540.0):
    return coord.StationCoordinator(
        hass, db, "sensor.t", "sensor.h", "sensor.p",
        pressure_is_sea_level=sea_level, elevation_m=elevation,
    )


# ---------------------------------------------------------------------------
# The reference itself
# ---------------------------------------------------------------------------
def test_reference_is_the_median_of_provider_forecasts(db):
    """Median, not mean, so one absurd provider value cannot drag the
    reference far enough to mask a genuine station error."""
    hour = "2026-09-02T12"
    for source, value in (
        ("ch1", 1022.0), ("ch2", 1023.0), ("icon_d2", 1024.0),
        ("srf", 1023.5), ("meteoblue", 9999.0),
    ):
        db.insert_forecast_snapshot(
            source, "i", f"{hour}:00:00+00:00", "pressure", value
        )
    assert db.get_reference_pressure_hpa(hour) == pytest.approx(1023.5)


def test_reference_is_none_when_no_provider_data_exists(db):
    """No reference means no cross-check — the station reading must be
    accepted rather than discarded on an absence of evidence."""
    assert db.get_reference_pressure_hpa("2026-09-02T12") is None


# ---------------------------------------------------------------------------
# The case the absolute-bounds check could not catch
# ---------------------------------------------------------------------------
def test_double_reduction_is_caught_on_an_ordinary_pressure_day(db):
    """The gap this release exists to close.

    1010 hPa double-reduced at 540 m is 1075.6 — wrong by 65 hPa, below
    the 1085 ceiling, and therefore invisible to the v0.2.1 check. It
    would have been stored and learned.
    """
    _seed_providers(db, 1010.0)
    hass = FakeHass({
        "sensor.t": FakeState("20.0", "°C"),
        "sensor.h": FakeState("50.0", "%"),
        "sensor.p": FakeState("1010.0", "hPa"),
    })
    c = _station(db, hass, sea_level=False)
    result = asyncio.run(c._async_update_data())

    assert result["pressure"] is None, (
        "a 65 hPa datum error passed because it did not exceed a world record"
    )


def test_the_same_value_would_have_passed_the_absolute_bounds_check():
    """Documents precisely why the previous check was insufficient, so a
    future reader does not mistake it for redundancy."""
    from swissweather_fusion import unit_conversion as uc

    doubled = uc.reduce_station_pressure_to_sea_level(1010.0, 540.0, 20.0)
    assert doubled < PRESSURE_PLAUSIBLE_MAX_HPA
    assert doubled - 1010.0 > 60.0


def test_correctly_configured_station_agrees_with_providers(db):
    """The reading must survive when the setting is right."""
    _seed_providers(db, 1023.0)
    hass = FakeHass({
        "sensor.t": FakeState("20.0", "°C"),
        "sensor.h": FakeState("50.0", "%"),
        "sensor.p": FakeState("1024.2", "hPa"),
    })
    c = _station(db, hass, sea_level=True)
    result = asyncio.run(c._async_update_data())
    assert result["pressure"] == pytest.approx(1024.2)
    assert abs(c.pressure_reference_delta) < 5


def test_genuine_station_pressure_reduced_correctly_agrees(db):
    """An absolute sensor at 540 m on a 1023 hPa day reads ~959; the
    reduction must land it back within tolerance."""
    _seed_providers(db, 1023.0)
    hass = FakeHass({
        "sensor.t": FakeState("20.0", "°C"),
        "sensor.h": FakeState("50.0", "%"),
        "sensor.p": FakeState("959.3", "hPa"),
    })
    c = _station(db, hass, sea_level=False)
    result = asyncio.run(c._async_update_data())
    assert result["pressure"] is not None
    assert abs(c.pressure_reference_delta) <= STATION_PRESSURE_REFERENCE_TOLERANCE_HPA


def test_normal_disagreement_within_tolerance_is_accepted(db):
    """Models are not perfect and neither is a domestic barometer. A few
    hPa must not trip the check."""
    _seed_providers(db, 1023.0)
    hass = FakeHass({
        "sensor.t": FakeState("20.0", "°C"),
        "sensor.h": FakeState("50.0", "%"),
        "sensor.p": FakeState("1029.0", "hPa"),
    })
    c = _station(db, hass, sea_level=True)
    assert asyncio.run(c._async_update_data())["pressure"] == pytest.approx(1029.0)


def test_no_provider_reference_means_the_reading_is_still_accepted(db):
    """Absence of evidence is not evidence of misconfiguration — on a
    fresh install no forecasts exist yet."""
    hass = FakeHass({
        "sensor.t": FakeState("20.0", "°C"),
        "sensor.h": FakeState("50.0", "%"),
        "sensor.p": FakeState("1024.2", "hPa"),
    })
    c = _station(db, hass, sea_level=True)
    result = asyncio.run(c._async_update_data())
    assert result["pressure"] == pytest.approx(1024.2)
    assert c.pressure_reference_delta is None


def test_temperature_and_humidity_survive_a_rejected_pressure(db):
    _seed_providers(db, 1010.0)
    hass = FakeHass({
        "sensor.t": FakeState("20.0", "°C"),
        "sensor.h": FakeState("50.0", "%"),
        "sensor.p": FakeState("1010.0", "hPa"),
    })
    c = _station(db, hass, sea_level=False)
    result = asyncio.run(c._async_update_data())
    assert result["temperature"] == pytest.approx(20.0)
    assert result["humidity"] == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# Reset must clear disagreeing history, not just impossible values
# ---------------------------------------------------------------------------
def test_reset_clears_observations_that_disagree_with_providers(db):
    """The residue problem. A previous reset only nulled values outside
    absolute bounds, so sub-1085 corrupted readings survived and would
    re-teach the same bias on the next reconciliation."""
    hour = "2026-09-02T12"
    _seed_providers(db, 1010.0, hour=hour)
    db.insert_station_observation(f"{hour}:00:00+00:00", 20.0, 50.0, 1075.6)
    db.insert_station_observation(f"{hour}:05:00+00:00", 20.0, 50.0, 1011.0)

    result = db.reset_all_learning()

    pressures = [
        r["pressure"]
        for r in db.get_station_observations_since("1970-01-01T00:00:00+00:00")
    ]
    assert result["observations_cleared"] >= 1
    assert pressures == [None, 1011.0], "a disagreeing reading survived the reset"


def test_reset_does_not_touch_observations_without_a_reference(db):
    """No provider data for that hour means no basis to judge — the
    observation must be left alone rather than destroyed on suspicion."""
    db.insert_station_observation("2026-09-02T12:00:00+00:00", 20.0, 50.0, 1024.2)
    db.reset_all_learning()
    rows = db.get_station_observations_since("1970-01-01T00:00:00+00:00")
    assert rows[0]["pressure"] == pytest.approx(1024.2)


# ---------------------------------------------------------------------------
# The delta must be visible, not inferred
# ---------------------------------------------------------------------------
def test_delta_sensor_exposes_the_relationship(db):
    """The disagreement was self-diagnosing for a whole day and nothing
    was looking. This sensor is the looking."""
    from swissweather_fusion.sensor import PressureReferenceDeltaSensor

    _seed_providers(db, 1023.0)
    hass = FakeHass({
        "sensor.t": FakeState("20.0", "°C"),
        "sensor.h": FakeState("50.0", "%"),
        "sensor.p": FakeState("1029.0", "hPa"),
    })
    c = _station(db, hass, sea_level=True)
    asyncio.run(c._async_update_data())

    sensor = object.__new__(PressureReferenceDeltaSensor)
    sensor._runtime = {"station_coordinator": c}
    assert PressureReferenceDeltaSensor.native_value.fget(sensor) == pytest.approx(6.0)
    attrs = PressureReferenceDeltaSensor.extra_state_attributes.fget(sensor)
    assert attrs["within_tolerance"] is True


def test_delta_sensor_is_blank_before_any_reading():
    from swissweather_fusion.sensor import PressureReferenceDeltaSensor

    sensor = object.__new__(PressureReferenceDeltaSensor)
    sensor._runtime = {}
    assert PressureReferenceDeltaSensor.native_value.fget(sensor) is None
