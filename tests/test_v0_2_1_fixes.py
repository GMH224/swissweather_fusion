"""Regression tests for v0.2.1.

Covers SWF-P1-008 (Class B fusion was never called), SWF-P1-009
(implausible station pressure), SWF-P2-007 (missing current-condition
properties), SWF-P2-008 (health rule vs once-daily sources), UV
acquisition, and the database size sensor.

**SWF-P1-008 is the important one, and it is a repeat of a known
pattern.** v0.2.0 implemented per-parameter fusion strategies, tested
them directly, and never wired them into the blend. The strategies were
correct; nothing called them. That is the same shape recorded in §9.8 of
the remediation audit — a test whose notion of success was satisfiable
without the code being reachable — so this file tests the *routing*, not
just the strategies.
"""
import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from swissweather_fusion import coordinator as coord
from swissweather_fusion.clients import open_meteo
from swissweather_fusion.storage.db import SwissWeatherDB

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
ISO = NOW.isoformat()


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
    database = SwissWeatherDB(str(tmp_path / "v021.db"))
    yield database
    database.close()


# ---------------------------------------------------------------------------
# SWF-P1-008 — the dispatch must actually route by parameter class
# ---------------------------------------------------------------------------
def _blend_coordinator(db):
    return coord.ModelABlendCoordinator(FakeHass(), db)


def _forecast(measurement, values):
    """latest_forecast shaped as _compute_blend builds it."""
    sources = ["ch1", "ch2", "icon_d2", "srf", "meteoblue"]
    return {
        (src, measurement, ISO): (value, NOW)
        for src, value in zip(sources, values)
    }


def test_precipitation_is_routed_to_the_median_not_the_learned_blend(db):
    """The reproduced defect.

    Every measurement went through _blend_at(), the learned-bias path.
    Class B parameters have no bucket_stats, so each source hit the
    cold-start branch and the result was a plain arithmetic mean — the
    exact behaviour the v0.2.0 registry was written to prevent.
    """
    c = _blend_coordinator(db)
    values = [0.0, 0.0, 8.0]
    result = c._blend_by_class(
        "precip", NOW,
        latest_forecast=_forecast("precip", values), bucket_lookup={},
    )
    assert result == 0.0, "precipitation is being averaged, not median-fused"
    assert result != pytest.approx(sum(values) / len(values))


def test_gusts_are_routed_to_max_not_the_learned_blend(db):
    c = _blend_coordinator(db)
    result = c._blend_by_class(
        "wind_gust_speed", NOW,
        latest_forecast=_forecast("wind_gust_speed", [12.0, 25.0, 18.0]),
        bucket_lookup={},
    )
    assert result == 25.0


def test_wind_bearing_is_routed_to_the_circular_mean(db):
    """Linear averaging of 350 deg and 10 deg gives 180 deg — due south
    when both sources say due north."""
    c = _blend_coordinator(db)
    result = c._blend_by_class(
        "wind_bearing", NOW,
        latest_forecast=_forecast("wind_bearing", [350.0, 10.0]), bucket_lookup={},
    )
    assert result == pytest.approx(0.0)


def test_weather_code_is_routed_to_majority_never_averaged(db):
    """The mean of codes 3 and 95 is 49, which is not a weather code at
    all — it is a number that happens to sit between two categories."""
    c = _blend_coordinator(db)
    result = c._blend_by_class(
        "weather_code", NOW,
        latest_forecast=_forecast("weather_code", [3.0, 3.0, 95.0]), bucket_lookup={},
    )
    assert result == 3.0


def test_learned_measurements_still_use_the_bias_corrected_path(db):
    """The dispatch must not accidentally demote Class A. Asserted
    structurally, since the learned path's behaviour is covered
    elsewhere."""
    import inspect

    source = inspect.getsource(coord.ModelABlendCoordinator._blend_by_class)
    assert "LEARNED_MEASUREMENTS" in source
    assert "_blend_at" in source


def test_every_measurement_is_covered_by_exactly_one_class():
    """A parameter belonging to no class would silently fall through to
    Class B; one in two classes would be ambiguous."""
    cls = coord.ModelABlendCoordinator
    learned = set(cls.LEARNED_MEASUREMENTS)
    fused = set(cls.FUSED_MEASUREMENTS)
    categorical = set(cls.CATEGORICAL_MEASUREMENTS)

    assert not (learned & fused)
    assert not (learned & categorical)
    assert not (fused & categorical)
    assert learned | fused | categorical == set(cls.MEASUREMENTS)


# ---------------------------------------------------------------------------
# SWF-P1-009 — implausible pressure must be discarded, not learned
# ---------------------------------------------------------------------------
def _station(db, hass, *, sea_level: bool, elevation=540.0):
    return coord.StationCoordinator(
        hass, db, "sensor.t", "sensor.h", "sensor.p",
        pressure_is_sea_level=sea_level, elevation_m=elevation,
    )


def test_double_reduced_pressure_is_discarded(db):
    """The exact failure from a real installation.

    A Netatmo entity reporting normalised pressure (1024.2 hPa) was
    configured as station-level, so it was reduced to sea level a SECOND
    time, becoming 1089.8. Model A learned that as a -66.8 hPa forecast
    bias and began dragging blended pressure upward. Every individual
    step worked exactly as designed; nothing compared the result against
    reality.
    """
    hass = FakeHass({
        "sensor.t": FakeState("24.0", "°C"),
        "sensor.h": FakeState("39.0", "%"),
        "sensor.p": FakeState("1024.2", "hPa"),
    })
    c = _station(db, hass, sea_level=False)
    result = asyncio.run(c._async_update_data())

    assert result["pressure"] is None, (
        "an implausible pressure was accepted and would poison an EMA bucket"
    )
    rows = db.get_station_observations_since("1970-01-01T00:00:00+00:00")
    assert all(r["pressure"] is None for r in rows)


def test_correctly_configured_sea_level_pressure_is_accepted(db):
    """With the setting right, the same reading passes through untouched."""
    hass = FakeHass({
        "sensor.t": FakeState("24.0", "°C"),
        "sensor.h": FakeState("39.0", "%"),
        "sensor.p": FakeState("1024.2", "hPa"),
    })
    c = _station(db, hass, sea_level=True)
    result = asyncio.run(c._async_update_data())
    assert result["pressure"] == pytest.approx(1024.2)


def test_genuine_station_pressure_is_still_reduced_and_accepted(db):
    """An absolute sensor at 540 m reads ~959 hPa on a 1023 hPa day; the
    reduction must still work and land in range."""
    hass = FakeHass({
        "sensor.t": FakeState("24.0", "°C"),
        "sensor.h": FakeState("39.0", "%"),
        "sensor.p": FakeState("959.3", "hPa"),
    })
    c = _station(db, hass, sea_level=False)
    result = asyncio.run(c._async_update_data())
    assert 1010 < result["pressure"] < 1035


def test_temperature_and_humidity_survive_a_discarded_pressure(db):
    """Discarding one bad measurement must not take the others down."""
    hass = FakeHass({
        "sensor.t": FakeState("24.0", "°C"),
        "sensor.h": FakeState("39.0", "%"),
        "sensor.p": FakeState("1024.2", "hPa"),
    })
    c = _station(db, hass, sea_level=False)
    result = asyncio.run(c._async_update_data())
    assert result["temperature"] == pytest.approx(24.0)
    assert result["humidity"] == pytest.approx(39.0)


# ---------------------------------------------------------------------------
# SWF-P2-007 — current-condition properties
# ---------------------------------------------------------------------------
def test_weather_entity_publishes_the_standard_optional_properties():
    """v0.2.0 fused these into the blend but the entity exposed only
    four properties, so a card configured to show them displayed
    nothing — correctly, because the entity provided nothing.

    All are documented WeatherEntity members, so they need no custom
    sensor entities (architecture review AR-01/AR-04).
    """
    from swissweather_fusion.weather import SwissWeatherFusionWeather

    for prop in (
        "native_dew_point", "native_apparent_temperature", "cloud_coverage",
        "native_visibility", "native_wind_gust_speed", "wind_bearing",
        "uv_index",
    ):
        assert hasattr(SwissWeatherFusionWeather, prop), f"missing {prop}"


def test_weather_entity_reads_those_properties_from_the_blend():
    from swissweather_fusion.weather import SwissWeatherFusionWeather as W

    entity = object.__new__(W)
    entity.coordinator = SimpleNamespace(
        data={"current": {
            "dew_point": 11.0, "apparent_temperature": 25.5,
            "cloud_coverage": 40.0, "visibility": 24000.0,
            "wind_gust_speed": 9.5,
        }}
    )
    assert W.native_dew_point.fget(entity) == 11.0
    assert W.native_apparent_temperature.fget(entity) == 25.5
    assert W.cloud_coverage.fget(entity) == 40.0
    assert W.native_visibility.fget(entity) == 24000.0
    assert W.native_wind_gust_speed.fget(entity) == 9.5


def test_visibility_unit_is_declared():
    """Without a declared unit Home Assistant cannot convert or render
    visibility. Open-Meteo reports metres."""
    from swissweather_fusion.weather import SwissWeatherFusionWeather

    assert SwissWeatherFusionWeather._attr_native_visibility_unit is not None


# ---------------------------------------------------------------------------
# UV acquisition
# ---------------------------------------------------------------------------
def test_uv_index_is_requested_but_kept_optional():
    """Requested by default, but separable — a rejection must not be able
    to take all three Open-Meteo models offline for one nice-to-have
    variable. Same reasoning as the v0.1.28 CombiPrecip lesson."""
    assert "uv_index" in open_meteo.OPTIONAL_HOURLY_VARIABLES
    assert "uv_index" not in open_meteo.HOURLY_VARIABLES

    with_optional = open_meteo.build_forecast_url(
        source="ch1", latitude=46.9, longitude=7.4
    )
    without = open_meteo.build_forecast_url(
        source="ch1", latitude=46.9, longitude=7.4, include_optional=False
    )
    assert "uv_index" in with_optional
    assert "uv_index" not in without
    # The core variables must survive the fallback.
    assert "temperature_2m" in without and "weather_code" in without


def test_uv_index_maps_into_the_common_vocabulary():
    from swissweather_fusion import forecast_parameters as fp

    parsed = open_meteo.parse_forecast_response(
        {"hourly": {"time": ["2026-09-02T12:00"], "uv_index": [6.2]}}
    )
    assert {p.variable for p in parsed.points} == {"uv_index"}
    assert fp.get("uv_index") is not None


# ---------------------------------------------------------------------------
# SWF-P2-008 — health rule vs once-daily sources
# ---------------------------------------------------------------------------
def _health(*, failures=0, succeeded=False, age_hours=0.0):
    return SimpleNamespace(
        consecutive_failures=failures,
        last_success_time=datetime.now(timezone.utc) if succeeded else None,
        created_at=datetime.now(timezone.utc) - timedelta(hours=age_hours),
    )


def test_once_daily_source_is_not_degraded_before_its_first_run():
    """The reported symptom: the integration showed "Degraded" for hours
    after every restart because Meteonomiqs runs once a day and had not
    run yet. A health indicator that cries wolf gets ignored."""
    from swissweather_fusion.sensor import is_source_healthy

    assert is_source_healthy(_health(age_hours=2), "meteonomiqs") is True
    assert is_source_healthy(_health(age_hours=2), "meteoblue") is True


def test_once_daily_source_is_degraded_after_its_grace_expires():
    """The grace must not become a permanent excuse — a genuinely dead
    source still has to surface, on the same day."""
    from swissweather_fusion.sensor import is_source_healthy

    assert is_source_healthy(_health(age_hours=30), "meteonomiqs") is False


def test_frequently_polled_source_gets_a_short_grace():
    from swissweather_fusion.sensor import is_source_healthy

    assert is_source_healthy(_health(age_hours=0.1), "combiprecip") is True
    assert is_source_healthy(_health(age_hours=2), "combiprecip") is False


def test_a_failing_source_is_unhealthy_regardless_of_grace():
    from swissweather_fusion.sensor import is_source_healthy

    assert is_source_healthy(_health(failures=3, age_hours=0.1), "meteonomiqs") is False


def test_a_succeeded_source_is_healthy_regardless_of_age():
    from swissweather_fusion.sensor import is_source_healthy

    assert is_source_healthy(_health(succeeded=True, age_hours=99), "meteonomiqs") is True


# ---------------------------------------------------------------------------
# AR-02 — database size sensor
# ---------------------------------------------------------------------------
def test_retention_coordinator_records_storage_stats_even_when_purge_disabled(db):
    """Reporting size is arguably most useful when retention is off,
    so the stats read sits outside the purge_days gate."""
    c = coord.RetentionCoordinator(FakeHass(), db, purge_days=0)
    asyncio.run(c._async_update_data())
    assert c.storage_stats is not None
    assert c.storage_stats["file_size_bytes"] > 0


def test_storage_sensor_reports_megabytes_and_row_counts(db):
    from swissweather_fusion.sensor import StorageSizeSensor

    c = coord.RetentionCoordinator(FakeHass(), db, purge_days=90)
    asyncio.run(c._async_update_data())

    sensor = object.__new__(StorageSizeSensor)
    sensor._runtime = {"retention_coordinator": c}
    assert StorageSizeSensor.native_value.fget(sensor) >= 0
    attrs = StorageSizeSensor.extra_state_attributes.fget(sensor)
    assert "forecast_snapshots_rows" in attrs


def test_storage_sensor_is_blank_before_the_first_retention_run():
    from swissweather_fusion.sensor import StorageSizeSensor

    sensor = object.__new__(StorageSizeSensor)
    sensor._runtime = {}
    assert StorageSizeSensor.native_value.fget(sensor) is None


# ---------------------------------------------------------------------------
# Learning reset button (SWF-P1-009 remediation)
# ---------------------------------------------------------------------------
def _seed_learning(db):
    from swissweather_fusion.storage.db import BucketKey

    for measurement in ("temperature", "humidity", "pressure"):
        key = BucketKey(
            hour_of_day=12, season="summer", lead_time_bucket="short",
            source="ch1", measurement=measurement,
        )
        db.apply_reconciliation_batch(
            [(key, 1.0, 1.0, 1.0, 10, "2026-09-02T12:00:00+00:00")], [], []
        )
    return lambda m: BucketKey(
        hour_of_day=12, season="summer", lead_time_bucket="short",
        source="ch1", measurement=m,
    )


def test_reset_clears_every_measurement_not_just_the_broken_one(db):
    """Deliberate design choice, not an oversight.

    A per-measurement reset was built first and rejected: it leaves the
    learned state at mixed vintages, so bucket confidence means different
    things for different measurements. That is subtler and longer-lived
    than simply relearning everything, and the samples being discarded
    are cheap to reacquire.
    """
    key = _seed_learning(db)
    result = db.reset_all_learning()

    assert result["buckets_cleared"] == 3
    for measurement in ("temperature", "humidity", "pressure"):
        assert db.get_bucket_stats(key(measurement)) is None


def test_reset_clears_implausible_observations_but_keeps_valid_ones(db):
    """Without this the poisoned observations are still inside the
    reconciliation window and would simply re-teach the same bias. Only
    physically impossible values are touched, so pressing the button can
    never destroy a legitimate reading."""
    db.insert_station_observation("2026-09-02T12:00:00+00:00", 24.0, 39.0, 1089.8)
    db.insert_station_observation("2026-09-02T12:05:00+00:00", 24.0, 39.0, 1024.2)

    result = db.reset_all_learning()

    pressures = [
        r["pressure"]
        for r in db.get_station_observations_since("1970-01-01T00:00:00+00:00")
    ]
    assert result["observations_cleared"] == 1
    assert pressures == [None, 1024.2]


def test_reset_reopens_recent_forecasts_so_relearning_is_fast(db):
    """Step three is what makes the reset cheap: learning rebuilds from
    forecasts already held rather than waiting for new ones, so recovery
    is hours rather than the days a cold start would take."""
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    db.insert_forecast_snapshot("ch1", "i", future, "temperature", 20.0)
    row_id = db._conn.execute("SELECT id FROM forecast_snapshots").fetchone()["id"]
    db.mark_forecast_snapshots_status([row_id], "reconciled")

    result = db.reset_all_learning()

    assert result["forecasts_reopened"] == 1
    status = db._conn.execute(
        "SELECT reconciliation_status FROM forecast_snapshots WHERE id = ?", (row_id,)
    ).fetchone()["reconciliation_status"]
    assert status == "pending"


def test_reset_preserves_raw_forecasts_and_observations(db):
    """Raw provider forecasts and valid sensor readings are facts. Only
    the derived interpretation is discarded."""
    _seed_learning(db)
    db.insert_station_observation("2026-09-02T12:00:00+00:00", 24.0, 39.0, 1024.2)
    db.insert_forecast_snapshot("ch1", "i", "2026-09-02T13:00:00+00:00", "temperature", 20.0)

    db.reset_all_learning()

    assert db._conn.execute(
        "SELECT COUNT(*) AS n FROM forecast_snapshots"
    ).fetchone()["n"] == 1
    assert db._conn.execute(
        "SELECT COUNT(*) AS n FROM station_observations"
    ).fetchone()["n"] == 1


def test_reset_on_a_fresh_database_is_a_harmless_noop(db):
    assert db.reset_all_learning() == {
        "buckets_cleared": 0, "observations_cleared": 0, "forecasts_reopened": 0,
    }


def test_reset_button_reports_what_it_did(db):
    """A recovery control with no feedback leaves the user unable to tell
    a successful reset from a no-op."""
    from swissweather_fusion.button import ResetLearningButton

    _seed_learning(db)
    button = object.__new__(ResetLearningButton)
    button.hass = FakeHass()
    button._runtime = {"db": db}
    button._last_result = None
    button.async_write_ha_state = lambda: None

    before = ResetLearningButton.extra_state_attributes.fget(button)
    assert before["last_reset_buckets_cleared"] is None

    asyncio.run(button.async_press())

    after = ResetLearningButton.extra_state_attributes.fget(button)
    assert after["last_reset_buckets_cleared"] == 3
    assert "preserved" in after["effect"]


def test_reset_button_is_hidden_by_default():
    """A recovery control, not a routine one — an always-visible reset
    invites experimentation with something that costs relearning time."""
    from swissweather_fusion.button import ResetLearningButton

    assert ResetLearningButton._attr_entity_registry_enabled_default is False
    assert ResetLearningButton._attr_entity_category == "diagnostic"


def test_button_platform_is_registered():
    """The entity is useless if the platform is never forwarded — the
    same 'implemented but not wired' failure as SWF-P1-008."""
    from swissweather_fusion import PLATFORMS

    assert "button" in [str(p) for p in PLATFORMS]
