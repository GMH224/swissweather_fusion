"""Construction smoke tests — every coordinator, really instantiated.

**Why this file exists.** v0.1.25 shipped with

    self._annual_budget = AnnualCallBudget(max_calls_per_year=...)

against a class whose parameter is `annual_budget`. Setup died with
`TypeError: AnnualCallBudget.__init__() got an unexpected keyword
argument 'max_calls_per_year'` at construction time, before a single
coordinator started.

The suite had 361 passing tests and every one of them passed with that
bug in place. The reason is structural: every coordinator test in this
project builds its subject with `object.__new__(cls)` and hand-sets the
handful of attributes the method under test reads. That is a reasonable
way to test a *method* without a running Home Assistant — but it means
`__init__` itself, and therefore every constructor call it makes, was
never executed by anything.

This is the same class of gap that let the v0.1.24 index bug ship: a test
that appears to cover a path while covering something adjacent to it.

These tests are deliberately shallow. They assert almost nothing about
behaviour. Their entire job is to run each `__init__` to completion with
realistic arguments, so that a wrong keyword, a renamed parameter or a
missing attribute fails here rather than on a user's installation.
"""
import asyncio

import pytest

from swissweather_fusion import coordinator as coord
from swissweather_fusion.storage.db import SwissWeatherDB


class FakeHass:
    """Enough hass for a constructor. Coordinators only stash it."""

    def __init__(self):
        self.data = {}
        self.config = type("C", (), {"latitude": 46.9481, "longitude": 7.4474})()

    async def async_add_executor_job(self, func, *args):
        return func(*args)


@pytest.fixture
def hass():
    return FakeHass()


@pytest.fixture
def db(tmp_path):
    database = SwissWeatherDB(str(tmp_path / "test.db"))
    yield database
    database.close()


LAT, LON = 46.9481, 7.4474


def test_open_meteo_coordinator_constructs(hass, db):
    c = coord.OpenMeteoCoordinator(
        hass, db, LAT, LON, api_key="key", diagnostics=None, actual_elevation_m=500.0
    )
    assert c is not None


def test_srf_coordinator_constructs(hass, db):
    c = coord.SrfCoordinator(
        hass, db, LAT, LON, "consumer-key", "consumer-secret", diagnostics=None
    )
    assert c is not None


def test_meteoblue_coordinator_constructs(hass, db):
    """The exact constructor that failed in v0.1.25.

    P1-06 added an AnnualCallBudget here, reusing the class already built
    for Meteonomiqs — and passed it a keyword the class does not have.
    """
    c = coord.MeteoblueCoordinator(
        hass, db, LAT, LON, "api-key", diagnostics=None
    )
    assert c._annual_budget is not None


def test_meteoblue_annual_budget_is_usable_not_merely_constructed(hass, db):
    """Constructing is not enough: the budget must actually gate calls,
    since P1-06's whole purpose is bounding the annual total."""
    from datetime import date

    from swissweather_fusion.const import METEOBLUE_ANNUAL_CALL_BUDGET

    c = coord.MeteoblueCoordinator(hass, db, LAT, LON, "api-key", diagnostics=None)
    today = date(2026, 9, 2)
    assert c._annual_budget.try_call(today=today) is True
    state = c._annual_budget.to_state()
    assert state == {"year": 2026, "calls_used": 1}
    # And it round-trips through the persistence shape the coordinator uses.
    c._annual_budget.load_state({"year": 2026, "calls_used": METEOBLUE_ANNUAL_CALL_BUDGET})
    assert c._annual_budget.try_call(today=today) is False


def test_meteonomiqs_coordinator_constructs(hass, db):
    c = coord.MeteonomiqsCoordinator(hass, db, LAT, LON, "api-key", diagnostics=None)
    assert c._budget is not None


def test_combiprecip_coordinator_constructs(hass, db):
    c = coord.CombiPrecipCoordinator(hass, db, LAT, LON, diagnostics=None)
    assert c is not None


def test_station_coordinator_constructs_with_pressure_reference(hass, db):
    """P1-22 added two parameters here; both call site and signature
    changed in the same release, which is exactly when they can silently
    disagree."""
    c = coord.StationCoordinator(
        hass,
        db,
        "sensor.temp",
        "sensor.humidity",
        "sensor.pressure",
        pressure_is_sea_level=False,
        elevation_m=540.0,
        diagnostics=None,
    )
    assert c._pressure_is_sea_level is False
    assert c._elevation_m == 540.0


def test_station_coordinator_defaults_are_backwards_compatible(hass, db):
    """The two new parameters must be optional, or any caller that has
    not been updated breaks."""
    c = coord.StationCoordinator(
        hass, db, "sensor.temp", "sensor.humidity", "sensor.pressure"
    )
    assert c is not None


def test_model_b_coordinator_constructs(hass, db):
    station = coord.StationCoordinator(
        hass, db, "sensor.t", "sensor.h", "sensor.p"
    )
    combiprecip = coord.CombiPrecipCoordinator(hass, db, LAT, LON)
    meteoblue = coord.MeteoblueCoordinator(hass, db, LAT, LON, "k")
    meteonomiqs = coord.MeteonomiqsCoordinator(hass, db, LAT, LON, "k")
    c = coord.ModelBCoordinator(
        hass, db, station, combiprecip, meteoblue, meteonomiqs, diagnostics=None
    )
    assert c is not None


def test_blend_coordinator_constructs(hass, db):
    c = coord.ModelABlendCoordinator(hass, db)
    assert c is not None


def test_learning_coordinator_constructs_with_and_without_a_lock(hass, db):
    """P2-03/P2-04 added an injectable lock. It must be optional, so the
    coordinator stays independently constructible."""
    injected = asyncio.Lock()
    with_lock = coord.ModelALearningCoordinator(hass, db, reconcile_lock=injected)
    assert with_lock._reconcile_lock is injected

    without = coord.ModelALearningCoordinator(hass, db)
    assert without._reconcile_lock is not None


def test_retention_coordinator_constructs_with_and_without_a_lock(hass, db):
    injected = asyncio.Lock()
    with_lock = coord.RetentionCoordinator(
        hass, db, purge_days=90, retention_lock=injected, diagnostics=None
    )
    assert with_lock._retention_lock is injected

    without = coord.RetentionCoordinator(hass, db, purge_days=90)
    assert without._retention_lock is not None


def test_storm_reconciliation_coordinator_constructs(hass, db):
    """P2-08's new coordinator — new code has no prior call site to
    validate it against, so constructing it is the only check there is."""
    c = coord.StormEventReconciliationCoordinator(hass, db, diagnostics=None)
    assert c.last_confirmed_count == 0


def test_the_shared_lock_really_is_one_object_across_both_coordinators(hass, db):
    """P2-04's correctness depends on identity, not merely on both
    coordinators having a lock. Two independent locks would serialize
    each coordinator against itself and nothing against the other."""
    shared = asyncio.Lock()
    learning = coord.ModelALearningCoordinator(hass, db, reconcile_lock=shared)
    retention = coord.RetentionCoordinator(
        hass, db, purge_days=90, retention_lock=shared
    )
    assert learning._reconcile_lock is retention._retention_lock


def test_every_coordinator_class_is_covered_by_this_file():
    """Guards the guard: a coordinator added later without a construction
    test here would silently reopen the gap that let v0.1.25 ship."""
    import inspect

    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

    defined = {
        name
        for name, obj in inspect.getmembers(coord, inspect.isclass)
        if issubclass(obj, DataUpdateCoordinator)
        and obj is not DataUpdateCoordinator
        and obj.__module__ == coord.__name__
    }
    tested = set()
    source = open(__file__, encoding="utf-8").read()
    for name in defined:
        if f"coord.{name}(" in source:
            tested.add(name)

    assert defined == tested, (
        f"coordinator(s) with no construction test: {sorted(defined - tested)}"
    )


# ---------------------------------------------------------------------------
# First-refresh smoke tests — "does it actually start"
# ---------------------------------------------------------------------------
# Construction alone was not enough to catch every v0.1.24/25 startup bug:
# P2-09's missing `self._diagnostics` would only have surfaced once
# _async_update_data_inner ran and encountered a future-dated row. So
# these run each coordinator's REAL refresh once, against a real migrated
# database, in the order __init__.py refreshes them.
#
# They assert little about the results — with no network and no entities
# the answers are mostly empty. The point is that every refresh path
# executes to completion rather than raising, which is the question three
# consecutive failed releases were unable to answer.
class StatefulHass(FakeHass):
    """FakeHass plus a states registry, which StationCoordinator reads."""

    def __init__(self, states=None):
        super().__init__()
        self._states = states or {}
        outer = self

        class States:
            def get(self, entity_id):
                return outer._states.get(entity_id)

        self.states = States()


class FakeState:
    def __init__(self, state, unit=None):
        self.state = state
        self.attributes = {"unit_of_measurement": unit} if unit else {}


@pytest.fixture
def migrated_db(tmp_path):
    """A database that has been through the real v0.1.23 -> v3 migration,
    not a freshly created one."""
    import sqlite3

    from tests.test_v0_1_24_storage import V0_1_23_SCHEMA

    path = str(tmp_path / "migrated.db")
    raw = sqlite3.connect(path)
    raw.executescript(V0_1_23_SCHEMA)
    raw.commit()
    raw.close()
    database = SwissWeatherDB(path)
    yield database
    database.close()


def test_storm_reconciliation_first_refresh_runs(migrated_db):
    hass = StatefulHass()
    c = coord.StormEventReconciliationCoordinator(hass, migrated_db)
    assert asyncio.run(c._async_update_data()) == {"checked": 0, "confirmed": 0}


def test_retention_first_refresh_runs(migrated_db):
    hass = StatefulHass()
    c = coord.RetentionCoordinator(hass, migrated_db, purge_days=90)
    result = asyncio.run(c._async_update_data())
    assert "forecast_snapshots" in result


def test_learning_first_refresh_runs_on_a_migrated_database(migrated_db):
    """P0-01 rewrote this method around apply_reconciliation_batch, and
    the migration re-opens recent forecast rows as 'pending' — so this
    exercises the new atomic path against real re-opened rows."""
    hass = StatefulHass()
    c = coord.ModelALearningCoordinator(hass, migrated_db)
    assert asyncio.run(c._async_update_data()) is not None


def test_blend_first_refresh_runs(migrated_db):
    hass = StatefulHass()
    c = coord.ModelABlendCoordinator(hass, migrated_db)
    result = asyncio.run(c._async_update_data())
    assert set(result) >= {"current", "expert_weights", "hourly_forecast"}


def test_model_b_first_refresh_runs(migrated_db):
    """The path that referenced a `self._diagnostics` which did not
    exist. Runs end to end now."""
    hass = StatefulHass()
    station = coord.StationCoordinator(hass, migrated_db, "sensor.t", "sensor.h", "sensor.p")
    cp = coord.CombiPrecipCoordinator(hass, migrated_db, LAT, LON)
    mb = coord.MeteoblueCoordinator(hass, migrated_db, LAT, LON, "k")
    mn = coord.MeteonomiqsCoordinator(hass, migrated_db, LAT, LON, "k")
    c = coord.ModelBCoordinator(hass, migrated_db, station, cp, mb, mn)
    assert asyncio.run(c._async_update_data()) == 0.0


def test_model_b_first_refresh_survives_a_future_dated_sample(migrated_db):
    """The exact condition that would have raised AttributeError: a
    future-dated station row reaching the diagnostics branch of P2-09's
    filter."""
    from datetime import datetime, timedelta, timezone

    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    migrated_db.insert_station_observation(future, 20.0, 50.0, 1013.0)

    hass = StatefulHass()
    station = coord.StationCoordinator(hass, migrated_db, "sensor.t", "sensor.h", "sensor.p")
    cp = coord.CombiPrecipCoordinator(hass, migrated_db, LAT, LON)
    mb = coord.MeteoblueCoordinator(hass, migrated_db, LAT, LON, "k")
    mn = coord.MeteonomiqsCoordinator(hass, migrated_db, LAT, LON, "k")
    c = coord.ModelBCoordinator(hass, migrated_db, station, cp, mb, mn)
    assert asyncio.run(c._async_update_data()) == 0.0


def test_station_first_refresh_reads_converts_and_reduces(migrated_db):
    """One pass covering P1-20, P1-21 and P1-22 through the real refresh:
    a Fahrenheit temperature and an absolute-pressure reading at 540 m."""
    hass = StatefulHass({
        "sensor.t": FakeState("68.0", "°F"),
        "sensor.h": FakeState("55.0", "%"),
        "sensor.p": FakeState("950.0", "hPa"),
    })
    c = coord.StationCoordinator(
        hass, migrated_db, "sensor.t", "sensor.h", "sensor.p",
        pressure_is_sea_level=False, elevation_m=540.0,
    )
    result = asyncio.run(c._async_update_data())

    assert result["temperature"] == pytest.approx(20.0)       # 68 °F
    assert result["humidity"] == pytest.approx(55.0)
    assert result["pressure"] > 1000.0                        # reduced to MSL
    stored = migrated_db.get_station_observations_since("1970-01-01T00:00:00+00:00")
    assert stored, "the reading was not persisted"


def test_station_first_refresh_writes_nothing_when_all_sensors_are_down(migrated_db):
    """IND-02's other half, through the real refresh."""
    hass = StatefulHass({
        "sensor.t": FakeState("unavailable"),
        "sensor.h": FakeState("unknown"),
        "sensor.p": FakeState("nan"),
    })
    c = coord.StationCoordinator(hass, migrated_db, "sensor.t", "sensor.h", "sensor.p")
    # Count first: the migrated fixture already carries one preserved
    # v0.1.23 observation, so an absolute emptiness check would be
    # asserting the fixture rather than the behaviour.
    before = len(migrated_db.get_station_observations_since("1970-01-01T00:00:00+00:00"))
    result = asyncio.run(c._async_update_data())
    after = len(migrated_db.get_station_observations_since("1970-01-01T00:00:00+00:00"))

    assert result == {"temperature": None, "humidity": None, "pressure": None}
    assert after == before, "an all-None row was written"
