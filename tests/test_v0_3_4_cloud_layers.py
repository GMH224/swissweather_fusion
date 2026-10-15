"""Regression tests for v0.3.4 — cloud cover by layer (low / mid / high),
and SRF irradiance keyed by period like the solar series.

Purpose: low cloud and fog are what break a radiation
forecast, and total cover cannot tell a thin high veil from a closed low
deck. The three ICON models state the layer split natively.

The release must not touch the Class A trial running until end of
November 2026. Several tests here guard the trial, not the feature:

- the run fingerprint must not change at upgrade (a changed fingerprint
  re-stores every model's current run and double-counts it in learning);
- the layers must never enter the learning loop or the blend verification;
- the condition shown on the card must not change (it reads total cover).

The output tests pin where the consumer reads the values: the hourly
forecast that weather.get_forecasts returns.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from swissweather_fusion import coordinator as coord
from swissweather_fusion import forecast_parameters as fp
from swissweather_fusion import provider_validation
from swissweather_fusion.clients import meteoblue, open_meteo
from swissweather_fusion.storage.db import SwissWeatherDB

UTC = timezone.utc
NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
LAYERS = ("cloud_coverage_low", "cloud_coverage_mid", "cloud_coverage_high")
OM_LAYERS = ("cloud_cover_low", "cloud_cover_mid", "cloud_cover_high")


class FakeHass:
    def __init__(self):
        self.data = {}
        self.config = type("C", (), {"latitude": 47.55, "longitude": 8.91})()

    async def async_add_executor_job(self, func, *args):
        return func(*args)


@pytest.fixture
def db(tmp_path):
    database = SwissWeatherDB(str(tmp_path / "v034.db"))
    yield database
    database.close()


def _blend(db):
    return coord.ModelABlendCoordinator(FakeHass(), db)


# ---------------------------------------------------------------------------
# Client: what is requested, how it is parsed
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("source", ["ch1", "ch2", "icon_d2"])
def test_layers_are_requested_from_every_icon_model_and_kept_optional(source):
    url = open_meteo.build_forecast_url(source=source, latitude=47.55, longitude=8.91)
    for variable in OM_LAYERS:
        assert variable in url
        assert variable in open_meteo.OPTIONAL_HOURLY_VARIABLES
        assert variable not in open_meteo.HOURLY_VARIABLES
    core_only = open_meteo.build_forecast_url(
        source=source, latitude=47.55, longitude=8.91, include_optional=False
    )
    assert "cloud_cover_low" not in core_only
    # the core request is untouched: total cover and Class A still there
    assert "cloud_cover" in core_only and "temperature_2m" in core_only


def test_radiation_and_uv_are_still_requested():
    """Extending the optional set must not displace what v0.3.3 added."""
    url = open_meteo.build_forecast_url(source="ch2", latitude=47.55, longitude=8.91)
    for variable in open_meteo.RADIATION_HOURLY_VARIABLES + ("uv_index",):
        assert variable in url


def _payload(include_layers: bool, hours: int = 3) -> dict:
    times = [(NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(hours)]
    hourly = {
        "time": times,
        "temperature_2m": [10.0 + h for h in range(hours)],
        "relative_humidity_2m": [70.0] * hours,
        "pressure_msl": [1020.0] * hours,
        "cloud_cover": [80.0] * hours,
    }
    if include_layers:
        hourly["cloud_cover_low"] = [70.0, 20.0, 0.0][:hours]
        hourly["cloud_cover_mid"] = [10.0, 30.0, 0.0][:hours]
        hourly["cloud_cover_high"] = [5.0, 90.0, 100.0][:hours]
    return {"hourly": hourly, "elevation": 471.0}


def test_layers_parse_into_the_internal_names_at_their_labels():
    parsed = open_meteo.parse_forecast_response(_payload(True))
    got = {(p.variable, p.valid_at): p.value for p in parsed.points}
    assert got[("cloud_coverage_low", NOW)] == 70.0
    assert got[("cloud_coverage_mid", NOW + timedelta(hours=1))] == 30.0
    assert got[("cloud_coverage_high", NOW + timedelta(hours=2))] == 100.0
    # total cover keeps its own name; the layers do not overwrite it
    assert got[("cloud_coverage", NOW)] == 80.0


def test_the_run_fingerprint_is_unchanged_by_the_layers():
    """**Guards the trial.** If the layers changed the fingerprint, the
    first poll after upgrade would re-store every model's current run and
    the learning loop would fold the same forecast errors in twice."""
    with_layers = open_meteo.parse_forecast_response(_payload(True))
    without = open_meteo.parse_forecast_response(_payload(False))
    assert with_layers.run_fingerprint == without.run_fingerprint


def test_the_run_fingerprint_matches_the_v0_3_3_algorithm():
    """Persisted v0.3.3 fingerprints stay valid across the upgrade."""
    from swissweather_fusion.fingerprint import compute_content_fingerprint

    hourly = _payload(True)["hourly"]
    old_source = {"time": hourly["time"]}
    for key in open_meteo._VARIABLE_NAME_MAP:
        if key in hourly:
            old_source[key] = hourly[key]
    expected = compute_content_fingerprint(old_source)
    assert open_meteo.parse_forecast_response(_payload(True)).run_fingerprint == expected


def test_layer_names_are_not_part_of_run_identity():
    for key in OM_LAYERS:
        assert key not in open_meteo._VARIABLE_NAME_MAP
        assert key in open_meteo._PARSED_VARIABLE_NAME_MAP


def test_only_the_icon_models_supply_layers():
    """meteoblue's basic package and SRF have no layer split; nothing may
    be mapped onto the layer names from them."""
    for name in getattr(meteoblue, "_VARIABLE_NAME_MAP", {}).values():
        assert name not in LAYERS


# ---------------------------------------------------------------------------
# Registry, bounds, classification
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", LAYERS)
def test_layers_are_fused_class_b_with_percent_bounds(name):
    parameter = fp.get(name)
    assert parameter is not None
    assert parameter.parameter_class == fp.ParameterClass.FUSED
    assert parameter.unit == "%"
    assert parameter.fuse_values([20.0, 40.0, 90.0]) == pytest.approx(50.0)
    assert provider_validation.validate_forecast_value(name, 100.0) == 100.0
    assert provider_validation.validate_forecast_value(name, 0.0) == 0.0
    assert provider_validation.validate_forecast_value(name, 100.5) is None
    assert provider_validation.validate_forecast_value(name, -1.0) is None


def test_layers_never_reach_the_learning_loop():
    """**Guards the trial.** No local ground truth exists for cloud
    layers; a learned bias for them would be fabricated, and anything
    added to the learned or reconciled set changes the trial."""
    for name in LAYERS:
        assert name in coord.ModelABlendCoordinator.FUSED_MEASUREMENTS
        assert name not in coord.ModelABlendCoordinator.LEARNED_MEASUREMENTS
    assert coord.ModelABlendCoordinator.LEARNED_MEASUREMENTS == ("temperature", "humidity", "pressure")
    reconciled = [
        getattr(cls, "RECONCILIATION_MEASUREMENTS")
        for cls in vars(coord).values()
        if isinstance(cls, type) and hasattr(cls, "RECONCILIATION_MEASUREMENTS")
    ]
    assert reconciled and all(r == ("temperature", "humidity", "pressure") for r in reconciled)


# ---------------------------------------------------------------------------
# Fusion and output, end to end through storage
# ---------------------------------------------------------------------------
def _store(db, source, issued, values_by_hour, total=80.0):
    rows = []
    for h, layers in values_by_hour.items():
        label = (NOW + timedelta(hours=h)).isoformat()
        rows.append((source, issued.isoformat(), label, "temperature", 12.0, "scheduled"))
        rows.append((source, issued.isoformat(), label, "cloud_coverage", total, "scheduled"))
        for name, value in zip(LAYERS, layers):
            if value is not None:
                rows.append((source, issued.isoformat(), label, name, value, "scheduled"))
    validated, _rejected, _dropped = provider_validation.validate_forecast_rows(rows)
    db.insert_forecast_snapshots_bulk(validated)


def _run(db, monkeypatch):
    from swissweather_fusion.models import model_a

    monkeypatch.setattr(model_a, "utcnow", lambda: NOW + timedelta(minutes=20))
    return asyncio.run(_blend(db)._async_update_data())


def test_hourly_forecast_carries_the_fused_layers(db, monkeypatch):
    issued = NOW - timedelta(hours=1)
    _store(db, "ch1", issued, {0: (80.0, 10.0, 0.0), 1: (60.0, 20.0, 10.0)})
    _store(db, "ch2", issued, {0: (40.0, 30.0, 20.0), 1: (20.0, 40.0, 30.0)})
    _store(db, "icon_d2", issued, {0: (60.0, 20.0, 40.0)})
    data = _run(db, monkeypatch)
    by_time = {e["datetime"]: e for e in data["hourly_forecast"]}
    first = by_time[NOW.isoformat()]
    assert first["cloud_coverage_low"] == pytest.approx(60.0)    # mean of 80, 40, 60
    assert first["cloud_coverage_mid"] == pytest.approx(20.0)
    assert first["cloud_coverage_high"] == pytest.approx(20.0)
    second = by_time[(NOW + timedelta(hours=1)).isoformat()]
    assert second["cloud_coverage_low"] == pytest.approx(40.0)   # only ch1 and ch2 reach it
    assert second["cloud_coverage_high"] == pytest.approx(20.0)
    # total cover is fused on its own, unaffected by the layers
    assert first["cloud_coverage"] == pytest.approx(80.0)
    assert json.loads(json.dumps(data["hourly_forecast"])) == data["hourly_forecast"]


def test_hours_without_layer_data_omit_the_keys(db, monkeypatch):
    """Like every other optional field: absent, never None or 0. A 0 %
    low-cloud value that nobody forecast would read as 'clear sky'."""
    issued = NOW - timedelta(hours=1)
    _store(db, "ch2", issued, {0: (50.0, 50.0, 50.0), 1: (None, None, None)})
    data = _run(db, monkeypatch)
    by_time = {e["datetime"]: e for e in data["hourly_forecast"]}
    assert "cloud_coverage_low" in by_time[NOW.isoformat()]
    later = by_time[(NOW + timedelta(hours=1)).isoformat()]
    for name in LAYERS:
        assert name not in later
    assert later["cloud_coverage"] == pytest.approx(80.0)


def test_the_condition_is_not_changed_by_the_layers(db, tmp_path, monkeypatch):
    """The card's condition reads total cover. Layers that disagree with
    it (here: a clear low layer under a total of 80 %) must not alter it."""
    issued = NOW - timedelta(hours=1)
    _store(db, "ch2", issued, {0: (0.0, 0.0, 0.0)})
    with_layers = _run(db, monkeypatch)["hourly_forecast"][0]["condition"]
    other = SwissWeatherDB(str(tmp_path / "without_layers.db"))
    try:
        _store(other, "ch2", issued, {0: (None, None, None)})
        without = _run(other, monkeypatch)["hourly_forecast"][0]["condition"]
    finally:
        other.close()
    assert with_layers == without


def test_current_values_carry_the_layers(db, monkeypatch):
    issued = NOW - timedelta(hours=1)
    _store(db, "ch1", issued, {0: (30.0, 20.0, 10.0)})
    current = _run(db, monkeypatch)["current"]
    assert current["cloud_coverage_low"] == pytest.approx(30.0)
    assert current["cloud_coverage_high"] == pytest.approx(10.0)


def test_weather_entity_passes_the_layers_through_unchanged():
    """weather.get_forecasts reads async_forecast_hourly. The entity must
    hand the entries over as built, extra keys included."""
    from swissweather_fusion.weather import SwissWeatherFusionWeather as W

    entry = {"datetime": NOW.isoformat(), "native_temperature": 12.0,
             "cloud_coverage": 80.0, "cloud_coverage_low": 60.0,
             "cloud_coverage_mid": 20.0, "cloud_coverage_high": 20.0}
    entity = object.__new__(W)
    entity.coordinator = SimpleNamespace(data={"hourly_forecast": [entry]})
    out = asyncio.run(W.async_forecast_hourly(entity))
    assert out == [entry]


# ---------------------------------------------------------------------------
# The optional-variable fallback still protects Class A
# ---------------------------------------------------------------------------
def test_a_rejection_still_drops_only_the_optional_set():
    """If a model ever refused a layer variable (HTTP 400), the retry
    without the optional set must still carry everything Class A needs."""
    core_only = open_meteo.build_forecast_url(
        source="ch1", latitude=47.55, longitude=8.91, include_optional=False
    )
    for variable in ("temperature_2m", "relative_humidity_2m", "pressure_msl"):
        assert variable in core_only
    for variable in open_meteo.OPTIONAL_HOURLY_VARIABLES:
        assert variable not in core_only.split("hourly=")[1].split("&")[0].split(",")


# ---------------------------------------------------------------------------
# SRF irradiance: keyed by period, like the solar series (v0.3.4)
# ---------------------------------------------------------------------------
def _store_srf(db, values_by_offset_h):
    db.insert_forecast_snapshots_bulk([
        ("srf", (NOW - timedelta(hours=1)).isoformat(),
         (NOW + timedelta(hours=h)).isoformat(), "srf_irradiance", v, "scheduled")
        for h, v in values_by_offset_h.items()
    ])


def _srf_series(db, monkeypatch):
    from swissweather_fusion.models import model_a

    monkeypatch.setattr(model_a, "utcnow", lambda: NOW + timedelta(minutes=20))
    return asyncio.run(_blend(db)._async_update_data())["srf_irradiance"]


def test_srf_value_labelled_t_describes_the_hour_before_t(db, monkeypatch):
    """Measured against the Taenikon pyranometer: SRF labels the END of
    its interval. The hour starting NOW is described by the label NOW+1h."""
    _store_srf(db, {0: 100.0, 1: 200.0, 2: 300.0})
    srf = _srf_series(db, monkeypatch)
    assert srf["current"] == 200.0
    first = srf["hourly"][0]
    assert first == {"period_start": NOW.isoformat(),
                     "period_end": (NOW + timedelta(hours=1)).isoformat(),
                     "value": 200.0}
    assert srf["hourly"][1]["period_start"] == (NOW + timedelta(hours=1)).isoformat()
    # the interval that ended at NOW is history, not forecast
    assert all(e["value"] != 100.0 for e in srf["hourly"])


def test_srf_series_uses_the_same_keys_as_the_solar_series(db, monkeypatch):
    """Consistency: one convention for every radiation series."""
    _store_srf(db, {1: 200.0})
    entry = _srf_series(db, monkeypatch)["hourly"][0]
    assert set(entry) == {"period_start", "period_end", "value"}
    assert "time" not in entry


def test_srf_three_hourly_values_cover_three_hours(db, monkeypatch):
    """Beyond SRF's hourly window the labels are 3 h apart; each value
    then describes the 3 hours before its label."""
    _store_srf(db, {1: 200.0, 2: 210.0, 5: 300.0, 8: 150.0})
    by_end = {e["period_end"]: e for e in _srf_series(db, monkeypatch)["hourly"]}
    e5 = by_end[(NOW + timedelta(hours=5)).isoformat()]
    assert e5["period_start"] == (NOW + timedelta(hours=2)).isoformat()
    e8 = by_end[(NOW + timedelta(hours=8)).isoformat()]
    assert e8["period_start"] == (NOW + timedelta(hours=5)).isoformat()


def test_srf_periods_never_stretch_across_a_data_gap(db, monkeypatch):
    """A missing stretch of SRF data must not turn the next value into a
    'mean' over 10 hours it never described."""
    _store_srf(db, {1: 200.0, 11: 300.0})
    by_end = {e["period_end"]: e for e in _srf_series(db, monkeypatch)["hourly"]}
    e11 = by_end[(NOW + timedelta(hours=11)).isoformat()]
    assert e11["period_start"] == (NOW + timedelta(hours=8)).isoformat()


def test_no_srf_data_means_no_current_value(db, monkeypatch):
    assert _srf_series(db, monkeypatch) == {"current": None, "hourly": []}


def test_only_a_past_srf_label_gives_no_current_value(db, monkeypatch):
    _store_srf(db, {0: 100.0})
    assert _srf_series(db, monkeypatch) == {"current": None, "hourly": []}


def test_srf_sensor_semantics_state_the_convention():
    from swissweather_fusion.sensor import SrfIrradianceSensor

    entity = object.__new__(SrfIrradianceSensor)
    entity._runtime = {"blend_coordinator": SimpleNamespace(data={"srf_irradiance": {
        "current": 200.0, "hourly": [{"period_start": "a", "period_end": "b", "value": 200.0}]}})}
    assert SrfIrradianceSensor.native_value.fget(entity) == 200.0
    attrs = SrfIrradianceSensor.extra_state_attributes.fget(entity)
    assert "period_start" in attrs["semantics"] and "END" in attrs["semantics"]
    assert attrs["hourly_forecast"][0]["period_end"] == "b"


def test_first_srf_label_takes_its_span_from_the_next_label(db, monkeypatch):
    """With only 3-hourly labels, the first value still covers 3 hours,
    so the current hour is covered by it."""
    _store_srf(db, {3: 240.0, 6: 260.0})
    srf = _srf_series(db, monkeypatch)
    assert srf["hourly"][0]["period_start"] == NOW.isoformat()
    assert srf["current"] == 240.0
