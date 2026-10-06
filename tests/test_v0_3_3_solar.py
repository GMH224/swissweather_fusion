"""Regression tests for v0.3.3 — solar radiation, snow depth, and the
source-health mapping drift (backlog items 19 + 20).

The release adds location-level solar radiation (GHI, DNI, DHI as hourly
averages and instants) for a downstream solar layer, exposes snow depth,
and exposes SRF's own irradiance as a separate series. It must not touch
the Class A measurement running until end of November 2026, so several
tests here guard the trial rather than the feature:

- the run fingerprint must not change at upgrade (a changed fingerprint
  re-stores the current upstream run and double-counts it in learning);
- a transient error must not drop the optional variables (that would
  silently remove radiation and UV until restart);
- one model refusing a variable must not strip it from the other two.

The fusion tests pin the physics: GHI = DHI + DNI * cos(zenith) holds
within every model, and the fused triple must preserve it.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from swissweather_fusion import coordinator as coord
from swissweather_fusion import forecast_parameters as fp
from swissweather_fusion import provider_validation
from swissweather_fusion.clients import open_meteo
from swissweather_fusion.health import SourceHealth
from swissweather_fusion.storage.db import SwissWeatherDB

UTC = timezone.utc
NOW = datetime(2026, 10, 6, 11, 0, tzinfo=UTC)
COS_Z = 0.55  # one solar zenith, shared by every model at one place/time


class FakeHass:
    def __init__(self):
        self.data = {}
        self.config = type("C", (), {"latitude": 47.55, "longitude": 8.91})()

    async def async_add_executor_job(self, func, *args):
        return func(*args)


@pytest.fixture
def db(tmp_path):
    database = SwissWeatherDB(str(tmp_path / "v033.db"))
    yield database
    database.close()


def _blend(db):
    return coord.ModelABlendCoordinator(FakeHass(), db)


def _consistent_triple(dni: float, dhi: float) -> tuple[float, float, float]:
    """(GHI, DNI, DHI) obeying the closure identity for COS_Z."""
    return dhi + dni * COS_Z, dni, dhi


def _rows_for(label: datetime, source: str, triple, issued: datetime = NOW,
              names=("ghi", "dni", "dhi")) -> dict:
    return {
        (source, name, label.isoformat()): (value, issued)
        for name, value in zip(names, triple)
        if value is not None
    }


# ---------------------------------------------------------------------------
# Client: what is requested, how it is parsed
# ---------------------------------------------------------------------------
def test_radiation_is_requested_and_kept_optional():
    url = open_meteo.build_forecast_url(source="ch1", latitude=47.55, longitude=8.91)
    for variable in open_meteo.RADIATION_HOURLY_VARIABLES:
        assert variable in url
        assert variable in open_meteo.OPTIONAL_HOURLY_VARIABLES
        assert variable not in open_meteo.HOURLY_VARIABLES
    core_only = open_meteo.build_forecast_url(
        source="ch1", latitude=47.55, longitude=8.91, include_optional=False
    )
    assert "shortwave_radiation" not in core_only
    assert "temperature_2m" in core_only and "relative_humidity_2m" in core_only


def test_no_tilted_irradiance_is_requested():
    """Owner decision: panel geometry belongs to the solar layer. A GTI
    request would need a tilt and azimuth, i.e. knowledge of the arrays."""
    url = open_meteo.build_forecast_url(source="ch2", latitude=47.55, longitude=8.91)
    assert "global_tilted" not in url
    assert "&tilt=" not in url and "&azimuth=" not in url


def _payload(include_radiation: bool, hours: int = 3) -> dict:
    times = [(NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(hours)]
    hourly = {
        "time": times,
        "temperature_2m": [10.0 + h for h in range(hours)],
        "relative_humidity_2m": [70.0] * hours,
        "pressure_msl": [1020.0] * hours,
    }
    if include_radiation:
        for variable in open_meteo.RADIATION_HOURLY_VARIABLES:
            hourly[variable] = [100.0 + h for h in range(hours)]
    return {"hourly": hourly, "elevation": 471.0}


def test_radiation_parses_into_the_internal_names():
    parsed = open_meteo.parse_forecast_response(_payload(True))
    names = {p.variable for p in parsed.points}
    assert {"ghi", "dni", "dhi", "ghi_instant", "dni_instant", "dhi_instant"} <= names


def test_the_run_fingerprint_is_unchanged_by_radiation():
    """**Guards the trial.** The fingerprint decides whether a poll is a
    new upstream run. If adding radiation changed it, the first poll after
    upgrade would re-store every model's current run and the learning
    loop would fold the same forecast errors in twice."""
    with_radiation = open_meteo.parse_forecast_response(_payload(True))
    without = open_meteo.parse_forecast_response(_payload(False))
    assert with_radiation.run_fingerprint == without.run_fingerprint


def test_the_run_fingerprint_matches_the_v0_3_2_algorithm():
    """Persisted v0.3.2 fingerprints must stay valid across the upgrade:
    recompute with the old rule (time axis + _VARIABLE_NAME_MAP keys
    only) and compare."""
    from swissweather_fusion.fingerprint import compute_content_fingerprint

    hourly = _payload(True)["hourly"]
    old_source = {"time": hourly["time"]}
    for key in open_meteo._VARIABLE_NAME_MAP:
        if key in hourly:
            old_source[key] = hourly[key]
    expected = compute_content_fingerprint(old_source)
    assert open_meteo.parse_forecast_response(_payload(True)).run_fingerprint == expected


class _FakeResponse:
    def __init__(self, status, body, body_is_json=True):
        self.status = status
        self._body = body
        self._json = body_is_json

    async def json(self):
        if not self._json:
            raise ValueError("not JSON")
        return self._body

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, response):
        self._response = response

    def get(self, url, **kwargs):
        return self._response


@pytest.mark.parametrize("body,is_json", [
    ({"error": True, "reason": "Cannot initialize model from invalid variable"}, True),
    ({}, True),
    ("<html>Bad Request</html>", False),
])
def test_every_http_400_is_a_rejection(body, is_json):
    """A 400 is the one signal that justifies dropping the optional set.
    Before v0.3.3 a 400 without a readable reason escaped as a plain
    HTTP error, and a non-JSON body as a JSON decode error."""
    client = open_meteo.OpenMeteoClient(_FakeSession(_FakeResponse(400, body, is_json)))
    with pytest.raises(open_meteo.OpenMeteoRequestRejected):
        asyncio.run(client.async_fetch_forecast(source="ch1", latitude=47.5, longitude=8.9))


def test_a_rejection_is_still_a_data_error_for_health():
    from swissweather_fusion.health import classify_exception

    assert classify_exception(open_meteo.OpenMeteoRequestRejected("x")) == "data"
    assert isinstance(open_meteo.OpenMeteoRequestRejected("x"), ValueError)


# ---------------------------------------------------------------------------
# Coordinator: the optional-variable fallback
# ---------------------------------------------------------------------------
class _Status503(Exception):
    status = 503


class _ScriptedClient:
    def __init__(self, errors):
        self.errors = errors
        self.calls: list[tuple[str, bool]] = []

    async def async_fetch_forecast(self, *, source, latitude, longitude, include_optional):
        self.calls.append((source, include_optional))
        raise self.errors[source]

    async def async_fetch_model_metadata(self, source):
        return None


def _open_meteo_coordinator(db, errors):
    c = coord.OpenMeteoCoordinator(
        FakeHass(), db, 47.55, 8.91, api_key=None, diagnostics=None,
        actual_elevation_m=471.0,
    )
    c._client = _ScriptedClient(errors)
    return c


def _run_ignoring_failure(c):
    try:
        asyncio.run(c._async_update_data())
    except Exception:  # noqa: BLE001 - every source fails by design here
        pass


def test_a_transient_error_does_not_drop_the_optional_variables(db):
    """**The pre-existing defect.** Any "data" error used to flip the
    flag. The owner's diagnostics show HTTP 503 on all three models; each
    one removed UV index until restart, and would now remove radiation."""
    c = _open_meteo_coordinator(db, {
        "ch1": _Status503("Service Unavailable"),
        "ch2": asyncio.TimeoutError(),
        "icon_d2": ValueError("malformed JSON"),
    })
    _run_ignoring_failure(c)
    _run_ignoring_failure(c)
    second_cycle = c._client.calls[3:]
    assert all(include for _, include in second_cycle), second_cycle


def test_a_rejection_drops_the_optional_set_for_that_model_only(db):
    """One model refusing a variable must not cost the others their
    radiation. Before v0.3.3 the flag was one bool for all three."""
    c = _open_meteo_coordinator(db, {
        "ch1": open_meteo.OpenMeteoRequestRejected("unknown variable"),
        "ch2": _Status503("Service Unavailable"),
        "icon_d2": _Status503("Service Unavailable"),
    })
    _run_ignoring_failure(c)
    _run_ignoring_failure(c)
    second_cycle = dict(c._client.calls[3:])
    assert second_cycle == {"ch1": False, "ch2": True, "icon_d2": True}


# ---------------------------------------------------------------------------
# Fusion: the triple
# ---------------------------------------------------------------------------
def test_the_fused_triple_preserves_the_closure_identity(db):
    c = _blend(db)
    latest = {}
    for source, (dni, dhi) in zip(("ch1", "ch2", "icon_d2"),
                                  ((700.0, 120.0), (500.0, 200.0), (50.0, 310.0))):
        latest.update(_rows_for(NOW, source, _consistent_triple(dni, dhi)))
    fused = c._fuse_radiation(("ghi", "dni", "dhi"), NOW, latest)
    assert fused["source_count"] == 3
    assert fused["ghi"] == pytest.approx(fused["dhi"] + fused["dni"] * COS_Z)


def test_a_median_would_break_the_identity_which_is_why_it_is_not_used(db):
    """Documents the design choice with numbers, then checks the routing:
    a radiation parameter asked of _blend_by_class gets the triple mean,
    never the per-parameter path."""
    # Medians drawn from DIFFERENT models (DNI from one, DHI from another)
    # — the realistic case. If all three medians came from one model the
    # identity would hold by accident, which is what an earlier draft of
    # this test did.
    triples = [_consistent_triple(700.0, 300.0), _consistent_triple(500.0, 100.0),
               _consistent_triple(100.0, 200.0)]
    median = [sorted(component)[1] for component in zip(*triples)]
    assert median[0] != pytest.approx(median[2] + median[1] * COS_Z)

    c = _blend(db)
    latest = {}
    for source, triple in zip(("ch1", "ch2", "icon_d2"), triples):
        latest.update(_rows_for(NOW, source, triple))
    mean_ghi = sum(t[0] for t in triples) / 3

    # Behavioural, not a source-text search: a text search matched the
    # word "_fuse_class_b" in _fuse_radiation's own docstring (the v0.3.1
    # comment-string false positive, repeated). Make the per-parameter
    # path fail loudly if radiation ever reaches it.
    def _must_not_be_called(*_args, **_kwargs):
        raise AssertionError("radiation reached the per-parameter Class B path")

    c._fuse_class_b = _must_not_be_called
    for name in ("ghi", "dni", "dhi"):
        c._blend_by_class(name, NOW, latest_forecast=latest, bucket_lookup={})
    assert c._blend_by_class("ghi", NOW, latest_forecast=latest, bucket_lookup={}) == (
        pytest.approx(mean_ghi)
    )


def test_an_incomplete_triple_is_excluded_from_all_three(db):
    """A model missing DNI must not still pull GHI and DHI: averaging GHI
    over three models and DNI over two yields a triple nobody forecast."""
    c = _blend(db)
    latest = {}
    latest.update(_rows_for(NOW, "ch1", _consistent_triple(600.0, 100.0)))
    ghi, _dni, dhi = _consistent_triple(100.0, 400.0)
    latest.update(_rows_for(NOW, "ch2", (ghi, None, dhi)))
    fused = c._fuse_radiation(("ghi", "dni", "dhi"), NOW, latest)
    assert fused["source_count"] == 1
    assert fused["ghi"] == pytest.approx(_consistent_triple(600.0, 100.0)[0])


def test_a_triple_patched_from_two_model_runs_is_excluded(db):
    """Freshest-row selection works per variable, so a gap in one
    component would otherwise be filled from an older run."""
    c = _blend(db)
    ghi, dni, dhi = _consistent_triple(600.0, 100.0)
    older = NOW - timedelta(hours=3)
    latest = {
        ("ch1", "ghi", NOW.isoformat()): (ghi, NOW),
        ("ch1", "dni", NOW.isoformat()): (dni, older),
        ("ch1", "dhi", NOW.isoformat()): (dhi, NOW),
    }
    assert c._fuse_radiation(("ghi", "dni", "dhi"), NOW, latest) is None


def test_an_out_of_bounds_member_invalidates_that_source_only(db):
    c = _blend(db)
    latest = {}
    latest.update(_rows_for(NOW, "ch1", (2500.0, 700.0, 120.0)))   # GHI nonsense
    latest.update(_rows_for(NOW, "ch2", _consistent_triple(500.0, 200.0)))
    fused = c._fuse_radiation(("ghi", "dni", "dhi"), NOW, latest)
    assert fused["source_count"] == 1
    assert fused["dni"] == pytest.approx(500.0)


def test_no_complete_triple_means_no_value_not_zero(db):
    c = _blend(db)
    assert c._fuse_radiation(("ghi", "dni", "dhi"), NOW, {}) is None


# ---------------------------------------------------------------------------
# Output semantics, end to end through storage
# ---------------------------------------------------------------------------
def _store_run(db, source, issued, hours, value_for):
    rows = []
    for h in range(hours + 1):
        label = NOW + timedelta(hours=h)
        for name in ("ghi", "dni", "dhi", "ghi_instant", "dni_instant", "dhi_instant"):
            rows.append((source, issued.isoformat(), label.isoformat(), name,
                         value_for(name, h), "scheduled"))
    validated, _rejected, _dropped = provider_validation.validate_forecast_rows(rows)
    db.insert_forecast_snapshots_bulk(validated)


def _value(name: str, h: int) -> float:
    # Average labelled at hour h carries 1000 + h; instant carries 2000 + h.
    # Both obey the identity with DNI 0 (pure diffuse), so GHI == DHI.
    base = 2000.0 if name.endswith("_instant") else 1000.0
    return 0.0 if name.startswith("dni") else (base + h) / 10.0


def test_hour_entries_pair_the_right_labels(db, monkeypatch):
    """**The off-by-one trap.** Open-Meteo labels an average at the END
    of its hour. The entry for the hour starting at T must therefore carry
    the average labelled T + 1 h, and the instant labelled T."""
    from swissweather_fusion.models import model_a

    monkeypatch.setattr(model_a, "utcnow", lambda: NOW + timedelta(minutes=20))
    _store_run(db, "ch1", NOW - timedelta(hours=1), 6, _value)
    c = _blend(db)
    data = asyncio.run(c._async_update_data())
    current = data["solar"]["current"]
    assert current["period_start"] == NOW.isoformat()
    assert current["period_end"] == (NOW + timedelta(hours=1)).isoformat()
    assert current["ghi"] == pytest.approx((1000.0 + 1) / 10.0)        # labelled T+1h
    assert current["ghi_instant"] == pytest.approx((2000.0 + 0) / 10.0)  # labelled T
    second = data["solar"]["hourly"][1]
    assert second["period_start"] == (NOW + timedelta(hours=1)).isoformat()
    assert second["ghi"] == pytest.approx((1000.0 + 2) / 10.0)
    assert json.loads(json.dumps(data["solar"])) == data["solar"]


def test_source_count_reflects_model_horizons(db, monkeypatch):
    from swissweather_fusion.models import model_a

    monkeypatch.setattr(model_a, "utcnow", lambda: NOW)
    _store_run(db, "ch1", NOW - timedelta(hours=1), 3, _value)    # short horizon
    _store_run(db, "ch2", NOW - timedelta(hours=1), 10, _value)   # long horizon
    data = asyncio.run(_blend(db)._async_update_data())
    by_start = {e["period_start"]: e for e in data["solar"]["hourly"]}
    assert by_start[NOW.isoformat()]["sources"] == 2
    assert by_start[(NOW + timedelta(hours=8)).isoformat()]["sources"] == 1


def test_srf_irradiance_is_passed_through_and_never_fused(db, monkeypatch):
    from swissweather_fusion.models import model_a

    monkeypatch.setattr(model_a, "utcnow", lambda: NOW)
    _store_run(db, "ch1", NOW - timedelta(hours=1), 3, _value)
    # SRF values at EVERY label the solar series reads (both the instant
    # label T and the average label T + 1 h). An earlier draft placed them
    # only where average labels did not line up and checked only the
    # averaged triple — mutation testing showed SRF could then leak into
    # the instant triple undetected.
    srf_values = {0: 640.0, 1: 655.0, 2: 500.0, 3: 410.0}
    db.insert_forecast_snapshots_bulk([
        ("srf", NOW.isoformat(), (NOW + timedelta(hours=h)).isoformat(),
         "srf_irradiance", v, "scheduled")
        for h, v in srf_values.items()
    ])
    data = asyncio.run(_blend(db)._async_update_data())
    assert data["srf_irradiance"]["current"] == 640.0
    assert [e["value"] for e in data["srf_irradiance"]["hourly"]] == list(srf_values.values())
    for entry in data["solar"]["hourly"]:
        h = int((datetime.fromisoformat(entry["period_start"]) - NOW).total_seconds() // 3600)
        if "ghi" in entry:
            assert entry["sources"] == 1
            assert entry["ghi"] == pytest.approx(_value("ghi", h + 1))
        if "ghi_instant" in entry:
            assert entry["instant_sources"] == 1
            assert entry["ghi_instant"] == pytest.approx(_value("ghi_instant", h))


def test_srf_irradiance_now_has_storage_bounds():
    assert provider_validation.validate_forecast_value("srf_irradiance", 800.0) == 800.0
    assert provider_validation.validate_forecast_value("srf_irradiance", 9999.0) is None
    assert provider_validation.validate_forecast_value("srf_irradiance", -5.0) is None


def test_radiation_never_reaches_the_learning_loop():
    """Class A learning reconciles temperature, humidity and pressure
    only. Radiation rows must never reach bucket_stats."""
    measurements = set(coord.ModelALearningCoordinator.RECONCILIATION_MEASUREMENTS)
    assert not measurements & fp.RADIATION_PARAMETERS
    assert not set(coord.ModelABlendCoordinator.LEARNED_MEASUREMENTS) & fp.RADIATION_PARAMETERS


# ---------------------------------------------------------------------------
# Sensors
# ---------------------------------------------------------------------------
def _runtime_with_blend_data(data):
    return {"blend_coordinator": SimpleNamespace(data=data)}


def _sensor(cls, runtime, *args):
    entry = SimpleNamespace(entry_id="e1", data={}, options={}, title="t")
    sensor = cls.__new__(cls)
    sensor._runtime = runtime
    if cls.__name__ == "SolarIrradianceSensor":
        sensor._key = args[0]
    return sensor, entry


def test_solar_sensors_report_the_current_hour_and_carry_the_series_once():
    from swissweather_fusion.sensor import SOLAR_SENSOR_KEYS, SolarIrradianceSensor

    entry = {"period_start": NOW.isoformat(),
             "period_end": (NOW + timedelta(hours=1)).isoformat(),
             "ghi": 410.0, "dni": 520.0, "dhi": 124.0, "sources": 3,
             "ghi_instant": 380.0, "dni_instant": 500.0, "dhi_instant": 105.0,
             "instant_sources": 2}
    runtime = _runtime_with_blend_data({"solar": {"current": entry, "hourly": [entry]}})
    for key in SOLAR_SENSOR_KEYS:
        sensor, _ = _sensor(SolarIrradianceSensor, runtime, key)
        assert sensor.native_value == entry[key]
        attrs = sensor.extra_state_attributes
        assert ("hourly_forecast" in attrs) is (key == "ghi")
        assert attrs["sources"] == (2 if key.endswith("_instant") else 3)
    assert SolarIrradianceSensor._attr_device_class == "irradiance"
    assert SolarIrradianceSensor._attr_native_unit_of_measurement == "W/m²"
    assert "hourly_forecast" in SolarIrradianceSensor._unrecorded_attributes


def test_solar_sensors_are_unknown_not_zero_without_data():
    from swissweather_fusion.sensor import SolarIrradianceSensor, SrfIrradianceSensor

    sensor, _ = _sensor(SolarIrradianceSensor, _runtime_with_blend_data({}), "ghi")
    assert sensor.native_value is None
    srf, _ = _sensor(SrfIrradianceSensor, _runtime_with_blend_data({}))
    assert srf.native_value is None


def test_the_new_entities_are_created():
    from swissweather_fusion import sensor

    setup = inspect.getsource(sensor.async_setup_entry)
    assert '"snow_depth"' in setup
    assert "SolarIrradianceSensor(entry, runtime, key) for key in SOLAR_SENSOR_KEYS" in setup
    assert "SrfIrradianceSensor(entry, runtime)" in setup
    assert set(sensor.SOLAR_SENSOR_KEYS) == fp.RADIATION_PARAMETERS


def test_snow_depth_sensor_reads_the_fused_value():
    from swissweather_fusion.sensor import BlendedValueSensor

    s = BlendedValueSensor.__new__(BlendedValueSensor)
    s._runtime = _runtime_with_blend_data({"current": {"snow_depth": 0.12}})
    s._measurement = "snow_depth"
    assert s.native_value == 0.12


# ---------------------------------------------------------------------------
# Backlog items 19 + 20: one source-to-health mapping
# ---------------------------------------------------------------------------
def test_every_telemetry_source_has_a_health_owner():
    """The drift guard. A source that gets telemetry sensors but no owner
    is exactly the v0.2.6 Wetter-Alarm defect."""
    from swissweather_fusion.const import SOURCE_HEALTH_OWNER
    from swissweather_fusion.sensor import ALL_TELEMETRY_SOURCES

    assert set(ALL_TELEMETRY_SOURCES) == set(SOURCE_HEALTH_OWNER)


def test_every_owner_is_a_real_runtime_key():
    from pathlib import Path

    from swissweather_fusion.const import SOURCE_HEALTH_OWNER

    init_source = (Path(coord.__file__).parent / "__init__.py").read_text()
    for owner in set(SOURCE_HEALTH_OWNER.values()):
        assert f'"{owner}": {owner}' in init_source, owner


def _health_runtime():
    shared = {s: SourceHealth() for s in ("ch1", "ch2", "icon_d2")}
    runtime = {"open_meteo_coordinator": SimpleNamespace(health=shared)}
    for name in ("station", "srf", "meteoblue", "combiprecip", "meteonomiqs", "wetteralarm"):
        runtime[f"{name}_coordinator"] = SimpleNamespace(
            health=SourceHealth(), update_interval=timedelta(minutes=20),
            last_update_success=True,
        )
    return runtime


def test_wetteralarm_health_is_visible_to_its_sensors():
    """**The reported symptom.** All four Wetter-Alarm telemetry sensors
    read unknown / 0 while the coordinator was polling successfully,
    because _get_health had no Wetter-Alarm branch."""
    from swissweather_fusion.sensor import ALL_TELEMETRY_SOURCES, _get_health

    runtime = _health_runtime()
    runtime["wetteralarm_coordinator"].health.record_success(duration_ms=120.0)
    assert _get_health(runtime, "wetteralarm").last_success_time is not None
    for source in ALL_TELEMETRY_SOURCES:
        assert _get_health(runtime, source) is not None, source
    assert _get_health(runtime, "ch2") is runtime["open_meteo_coordinator"].health["ch2"]


def test_diagnostics_export_end_to_end_includes_every_source():
    """The smoke test diagnostics.py has referred to since v0.1.22 but
    which did not exist: nothing ever called the export end to end. It is
    the November deliverable, so it is called here, JSON-encoded exactly
    as Home Assistant does, and checked for Wetter-Alarm (item 19)."""
    from swissweather_fusion import diagnostics
    from swissweather_fusion.const import DOMAIN, SOURCE_HEALTH_OWNER

    runtime = _health_runtime()
    runtime["open_meteo_coordinator"].update_interval = timedelta(minutes=15)
    runtime["open_meteo_coordinator"].last_update_success = True
    entry = SimpleNamespace(entry_id="e1", data={"latitude": 47.55, "longitude": 8.91},
                            options={})
    hass = SimpleNamespace(data={DOMAIN: {"e1": runtime}})

    payload = asyncio.run(diagnostics.async_get_config_entry_diagnostics(hass, entry))
    encoded = json.loads(json.dumps(payload))
    expected = {"station", "open_meteo"} | {
        s for s, owner in SOURCE_HEALTH_OWNER.items() if owner != "open_meteo_coordinator"
    }
    assert set(encoded["source_health"]) == expected
    assert "wetteralarm" in encoded["source_health"]
    assert "47.55" not in json.dumps(encoded)


def test_the_comment_promising_a_smoke_test_is_now_true():
    from pathlib import Path

    here = Path(__file__).read_text()
    assert "def test_diagnostics_export_end_to_end_includes_every_source" in here
    assert math.isfinite(COS_Z)
