"""Regression tests for the external ICS audit of v0.2.1.

Eighteen findings, several of them defects this project introduced. The
recurring theme — recorded in §9.8 of the remediation audit and now
appearing for the fourth consecutive release — is code that is
implemented but never reached. These tests therefore assert **reachability
and wiring**, not just that a function behaves correctly in isolation.
"""
import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from swissweather_fusion import provider_validation as pv
from swissweather_fusion.models import model_a
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


# ---------------------------------------------------------------------------
# SWF-021-006 (Critical) — radar field contract
# ---------------------------------------------------------------------------
def test_radar_pixel_exposes_the_renamed_field_only():
    """The rename that the persistence call site missed.

    v0.1.24 renamed precip_rate_mmh -> precip_accum_mm_1h because
    CombiPrecip reports a one-hour accumulation, not an instantaneous
    rate. One call site kept the old name and raised AttributeError on
    every radar cycle.
    """
    from swissweather_fusion.clients.combiprecip import RadarPixelValue

    pixel = RadarPixelValue(
        label="local", precip_accum_mm_1h=1.2,
        valid_at=datetime.now(timezone.utc), quality=9,
    )
    assert pixel.precip_accum_mm_1h == 1.2
    assert not hasattr(pixel, "precip_rate_mmh")


def test_radar_persistence_uses_the_current_field_and_stores_quality():
    """Asserted against the source, because the failure was a name that
    does not exist — which no amount of exercising the happy path with a
    mock would reveal."""
    import inspect

    from swissweather_fusion import coordinator as coord

    source = inspect.getsource(coord.CombiPrecipCoordinator)
    assert "local.precip_accum_mm_1h" in source
    assert "local.precip_rate_mmh" not in source
    assert "local.quality" in source, "the parsed quality code is still discarded"


def test_success_is_recorded_only_after_the_radar_row_is_written():
    """The reason this defect was invisible for a whole release.

    record_success() and the "N points extracted" diagnostic ran BEFORE
    the failing write, so health telemetry reported CombiPrecip healthy
    and succeeding while radar_observations stayed empty and Model B got
    no radar signal at all.
    """
    import inspect

    from swissweather_fusion import coordinator as coord

    source = inspect.getsource(coord.CombiPrecipCoordinator)
    # Compare the LAST occurrence of each: the class contains several
    # mentions in comments, and what matters is the executable order.
    assert source.rindex("insert_radar_observation") < source.rindex(
        "self.health.record_success"
    ), "success is recorded before the work is durably complete"


# ---------------------------------------------------------------------------
# SWF-021-010 / 011 — UV and sunshine must reach the blend
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("parameter", ["uv_index", "sunshine_duration"])
def test_registered_parameters_are_actually_fused(parameter):
    """Both were registered, requested, mapped and published on the
    entity — and omitted from FUSED_MEASUREMENTS, which is what the blend
    queries. So both were collected, stored, and never exposed. A user
    looking for UV found nothing."""
    from swissweather_fusion.coordinator import ModelABlendCoordinator

    assert parameter in ModelABlendCoordinator.MEASUREMENTS


def test_every_fusable_registry_parameter_reaches_the_blend():
    """Guards the class of defect rather than the two instances: a
    parameter cannot be fusable in the registry yet invisible to the
    blend."""
    from swissweather_fusion import forecast_parameters as fp
    from swissweather_fusion.coordinator import ModelABlendCoordinator

    missing = set(fp.fused_parameters()) - set(ModelABlendCoordinator.MEASUREMENTS)
    assert not missing, f"registered but never fused: {sorted(missing)}"


# ---------------------------------------------------------------------------
# SWF-021-009 — the UV fallback must actually be wired
# ---------------------------------------------------------------------------
def test_optional_variable_flag_is_consulted_not_merely_assigned():
    """v0.2.1 set _include_optional_variables and never read it, so the
    protection its own comment described did not exist: a model
    rejecting uv_index would fail all three Open-Meteo sources
    permanently."""
    import inspect

    from swissweather_fusion import coordinator as coord

    source = inspect.getsource(coord.OpenMeteoCoordinator)
    assert source.count("_include_optional_variables") >= 3, (
        "the flag is assigned but never consulted"
    )
    assert "include_optional=self._include_optional_variables" in source


def test_client_forwards_the_optional_flag():
    import inspect

    from swissweather_fusion.clients.open_meteo import OpenMeteoClient

    source = inspect.getsource(OpenMeteoClient.async_fetch_forecast)
    assert "include_optional=include_optional" in source


# ---------------------------------------------------------------------------
# SWF-022-001 / SWF-021-013 — validation vocabulary
# ---------------------------------------------------------------------------
def test_precipitation_is_bounds_checked_under_its_real_name():
    """The bounds dict used the key "precipitation" while every stored
    row uses "precip", so precipitation fell through to the
    unknown-variable path and received a finiteness check only."""
    assert "precip" in pv.PHYSICAL_BOUNDS
    assert pv.validate_forecast_value("precip", 9999.0) is None
    assert pv.validate_forecast_value("precip", -1.0) is None
    assert pv.validate_forecast_value("precip", 5.0) == 5.0


def test_every_fusable_parameter_is_also_validated():
    """Deriving bounds from the registry makes drift impossible: a
    parameter cannot be fusable without also being validated."""
    from swissweather_fusion import forecast_parameters as fp

    categorical = {"weather_code"}
    missing = {
        name for name in fp.fused_parameters()
        if name not in pv.PHYSICAL_BOUNDS and name not in categorical
    }
    assert not missing, f"fusable but unvalidated: {sorted(missing)}"


def test_pressure_keeps_the_wider_storage_bounds():
    """Raw provider pressure is MSL, but a station at 2000 m legitimately
    reads ~795 hPa. Storage must accommodate that; the tighter sea-level
    check belongs at the station coordinator."""
    assert pv.PHYSICAL_BOUNDS["pressure"] == (800.0, 1100.0)


def test_categorical_parameters_are_not_bounded_by_value():
    """Weather codes are labels, not magnitudes."""
    assert "weather_code" not in pv.PHYSICAL_BOUNDS
    assert pv.validate_forecast_value("weather_code", 95.0) == 95.0


# ---------------------------------------------------------------------------
# SWF-021-001 / 002 / 004 — condition resolution everywhere
# ---------------------------------------------------------------------------
def test_current_condition_uses_the_resolver():
    """The user-visible symptom: the entity contradicting itself,
    reporting "Sunny" beside a published cloud_coverage of 89%, because
    this site still inferred from humidity."""
    import inspect

    from swissweather_fusion.weather import SwissWeatherFusionWeather

    source = inspect.getsource(SwissWeatherFusionWeather.condition.fget)
    assert "resolve_condition" in source
    assert "cloud_coverage" in source


def test_clear_sky_code_is_reconciled_against_measured_cloud_cover():
    """A stated clear-sky code must not outrank contradictory measured
    cover. Reporting sunny at 89% cover is one entity disagreeing with
    itself."""
    assert model_a.resolve_condition(weather_code=0, cloud_coverage=89.0) == "cloudy"
    assert model_a.resolve_condition(weather_code=1, cloud_coverage=55.0) == "partlycloudy"
    # Consistent inputs are unchanged.
    assert model_a.resolve_condition(weather_code=0, cloud_coverage=5.0) == "sunny"


def test_significant_weather_codes_are_never_second_guessed():
    """Rain, fog and thunder describe something cloud cover cannot
    contradict."""
    assert model_a.resolve_condition(weather_code=95, cloud_coverage=10.0) == "lightning"
    assert model_a.resolve_condition(weather_code=45, cloud_coverage=10.0) == "fog"
    assert model_a.resolve_condition(weather_code=65, cloud_coverage=20.0) == "pouring"


def _hours(n, **overrides):
    base = datetime(2026, 1, 15, 0, 0, tzinfo=timezone.utc)
    entries = []
    for hour in range(n):
        entry = {
            "datetime": (base + timedelta(hours=hour)).isoformat(),
            "native_temperature": 6.0 if 10 <= hour <= 16 else -2.0,
            "humidity": 80.0,
            "native_precipitation": 1.0,
        }
        entry.update(overrides)
        entries.append(entry)
    return entries


def test_daily_aggregation_sees_stated_snowfall_despite_a_warm_afternoon():
    """SWF-021-003. The daily aggregation inferred snow from the DAILY
    MAXIMUM temperature, so a day with a +6 C afternoon and overnight
    snow was classified rain — the maximum never went below zero."""
    entries = _hours(24, snowfall=2.0)
    days = model_a.aggregate_daily_forecast(entries, local_tz=timezone.utc)
    assert days[0]["condition"] == "snowy"


def test_twice_daily_aggregation_also_sees_stated_snowfall():
    entries = _hours(24, snowfall=2.0)
    periods = model_a.aggregate_twice_daily_forecast(entries, local_tz=timezone.utc)
    assert any(p["condition"] == "snowy" for p in periods)


def test_aggregations_never_average_weather_codes():
    """The mean of codes 3 and 95 is 49, which is not a weather code."""
    assert model_a._majority([3, 3, 95]) == 3
    assert model_a._majority([3, 95]) == 95  # tie -> more severe
    assert model_a._majority([None, None]) is None


# ---------------------------------------------------------------------------
# SWF-021-005 — the storm coordinator must be listener-registered
# ---------------------------------------------------------------------------
def test_storm_reconciliation_coordinator_is_listener_registered():
    """DataUpdateCoordinator only schedules its recurring refresh while
    it has a listener. Omitted from that loop, the 30-minute cycle never
    ran after the first refresh — so storm_events, the table the whole
    Model B v1 plan depends on, could still only fill on a restart."""
    import inspect

    import swissweather_fusion as pkg

    source = inspect.getsource(pkg.async_setup_entry)
    listener_line = source.index("async_add_listener")
    # The tuple being iterated sits immediately above the registration.
    tuple_start = source.rindex("for coordinator in (", 0, listener_line)
    assert "storm_reconciliation_coordinator" in source[tuple_start:listener_line]


# ---------------------------------------------------------------------------
# SWF-021-007 — Degraded must use per-source health semantics
# ---------------------------------------------------------------------------
def test_degraded_sensor_passes_the_source_name_through():
    """Without it the entity used the default one-hour grace for every
    source and still reported Degraded for hours after a restart because
    Meteonomiqs runs once daily — the exact symptom SWF-P2-008 fixed,
    surviving on the entity most likely to drive an automation."""
    import inspect

    from swissweather_fusion.binary_sensor import DegradedBinarySensor

    source = inspect.getsource(DegradedBinarySensor.is_on.fget)
    assert "is_source_healthy(health, source)" in source


# ---------------------------------------------------------------------------
# SWF-021-008 — schema detection must check every migrated column
# ---------------------------------------------------------------------------
def test_partially_migrated_database_is_not_declared_current(tmp_path):
    """A sentinel set that is a subset of the real requirements will
    eventually declare a partially-migrated database current."""
    import sqlite3

    from tests.test_v0_1_24_storage import V0_1_23_SCHEMA

    path = str(tmp_path / "partial.db")
    raw = sqlite3.connect(path)
    raw.executescript(V0_1_23_SCHEMA)
    # Migrate two of the three tables, leaving radar_observations old.
    raw.execute("DROP TABLE storm_predictions")
    raw.execute(
        "CREATE TABLE storm_predictions (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, "
        "probability REAL NOT NULL, features TEXT, reconciled INTEGER NOT NULL DEFAULT 0)"
    )
    raw.commit()
    raw.close()

    database = SwissWeatherDB(path)
    try:
        columns = {
            r["name"]
            for r in database._conn.execute("PRAGMA table_info(radar_observations)")
        }
        assert "precip_accum_mm_1h" in columns
        assert "quality" in columns
    finally:
        database.close()


# ---------------------------------------------------------------------------
# SWF-021-012 — construction failures must not leak the connection
# ---------------------------------------------------------------------------
def test_setup_closes_the_database_if_construction_fails():
    """Home Assistant retries setup after a failure, so a leaked
    connection is opened again on every retry."""
    import inspect

    import swissweather_fusion as pkg

    source = inspect.getsource(pkg.async_setup_entry)
    construction_start = source.index("station_coordinator = StationCoordinator")
    # The guard opens before construction and closes after it.
    assert "try:" in source[:construction_start]
    after = source[construction_start:]
    assert "except Exception:" in after
    assert "db.close" in after


# ---------------------------------------------------------------------------
# SWF-021-014 — duplicate runs must still report the source
# ---------------------------------------------------------------------------
def test_duplicate_run_still_reports_the_source():
    """Omitting an unchanged source from results made the coordinator
    claim it was unavailable when its data is healthy and already
    persisted."""
    import inspect

    from swissweather_fusion import coordinator as coord

    source = inspect.getsource(coord.OpenMeteoCoordinator._async_update_data)
    dedup = source[source.index("== previous_fingerprint"):]
    # The source must be recorded before the `continue` that skips storage.
    continue_at = dedup.index("continue")
    assert "results[source] = parsed" in dedup[:continue_at]


# ---------------------------------------------------------------------------
# SWF-021-015 — meteoblue coverage
# ---------------------------------------------------------------------------
def test_meteoblue_maps_the_expanded_parameters():
    """These fields are already in the response the existing request
    returns — no extra call, no extra credit against the budget."""
    from swissweather_fusion.clients.meteoblue import _FIELD_MAP

    mapped = set(_FIELD_MAP.values())
    assert {"precip_probability", "wind_gust_speed", "dew_point",
            "apparent_temperature", "cloud_coverage", "uv_index"} <= mapped


# ---------------------------------------------------------------------------
# Reset button — re-verified end to end after the audit
# ---------------------------------------------------------------------------
def test_reset_button_clears_a_reproduced_poisoned_state(tmp_path):
    """Reproduces the real incident: a -66.8 hPa learned pressure bias
    and a 1089.8 hPa observation, both from a double-reduced datum."""
    from swissweather_fusion.button import ResetLearningButton
    from swissweather_fusion.storage.db import BucketKey

    db = SwissWeatherDB(str(tmp_path / "reset.db"))
    try:
        key = BucketKey(
            hour_of_day=12, season="summer", lead_time_bucket="short",
            source="ch1", measurement="pressure",
        )
        db.apply_reconciliation_batch(
            [(key, -66.8, 66.8, 0.5, 20, "2026-09-02T12:00:00+00:00")], [], []
        )
        db.insert_station_observation("2026-09-02T12:00:00+00:00", 24.0, 39.0, 1089.8)
        assert db.get_bucket_stats(key).ema_bias == pytest.approx(-66.8)

        refreshed = []

        class FakeLearning:
            async def async_request_refresh(self):
                refreshed.append(True)

        button = object.__new__(ResetLearningButton)
        button.hass = FakeHass()
        button._runtime = {"db": db, "learning_coordinator": FakeLearning()}
        button._last_result = None
        button.async_write_ha_state = lambda: None

        asyncio.run(button.async_press())

        assert db.get_bucket_stats(key) is None
        assert db.get_station_observations_since(
            "1970-01-01T00:00:00+00:00"
        )[0]["pressure"] is None
        assert refreshed, "relearning was not triggered"
    finally:
        db.close()


def test_reset_button_is_visible_so_it_can_be_found_when_needed():
    from swissweather_fusion.button import ResetLearningButton

    assert ResetLearningButton._attr_entity_registry_enabled_default is True
    assert ResetLearningButton._attr_entity_category == "diagnostic"
