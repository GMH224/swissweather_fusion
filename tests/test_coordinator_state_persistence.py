"""Coordinator-level tests for the v0.1.23 restart-safety and retention
fixes (L-07/L-08/L-09/L-10, and own-review finding F-2).

**Why this file exists, and why it's new territory for this project**:
tests/test_syntax.py's own docstring states plainly that coordinator.py
"cannot be functionally exercised in this test suite without a running
Home Assistant instance" — every other test file only imports the pure
business-logic modules (models/, clients/, storage/), never
coordinator.py itself. That's still broadly true: the HA stub in
conftest.py makes `import coordinator` and instantiate-a-plain-object
possible, but `DataUpdateCoordinator.__init__` in that stub is a no-op
(doesn't set `self.hass`, doesn't wire up scheduling), so a fully normal
`SomeCoordinator(hass, db, ...)` construction can't be driven through
`_async_update_data()` end-to-end the way a real HA test harness could.

What CAN be done, and what this file does: bypass `__init__` via
`object.__new__(cls)`, hand-set only the specific attributes the method
under test actually reads (`self.hass`, `self._db`, etc.), and call the
one async method being verified directly. This is narrower than a true
integration test, but it's genuinely more than the "syntax-valid only"
bar every other coordinator test in this project has previously cleared,
and it directly exercises the real production code — not a mirror/copy
of it, unlike tests/test_learning_integration.py's necessary approach
for the same underlying constraint.

A minimal FakeHass below provides only what these methods need:
async_add_executor_job(func, *args) -> awaits func(*args) via a real
asyncio thread executor, matching Home Assistant's actual contract
closely enough for this purpose.
"""
import asyncio
import os
import tempfile
from datetime import date, datetime, timedelta, timezone

import pytest

from swissweather_fusion import coordinator as coord
from swissweather_fusion.clients.meteoblue import BonusCallTracker
from swissweather_fusion.clients.meteonomiqs import AnnualCallBudget, HourlyForecastPoint
from swissweather_fusion.storage.db import SwissWeatherDB


class FakeHass:
    """Provides just enough of Home Assistant's `hass` surface for the
    coordinator methods under test: async_add_executor_job, matching the
    real contract of "runs this sync callable off the event loop and
    returns its result", which is all these coordinator methods need.
    """

    async def async_add_executor_job(self, func, *args):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, func, *args)


@pytest.fixture
def db():
    path = tempfile.mktemp(suffix=".db")
    database = SwissWeatherDB(path)
    yield database
    database.close()
    os.remove(path)


# -- RetentionCoordinator (L-10) ---------------------------------------------


def test_retention_coordinator_noops_when_purge_days_is_zero(db):
    """purge_days=0 means 'keep forever' per const.py's own documented
    meaning — the coordinator must not touch the database at all, not
    even compute a cutoff."""
    db.insert_forecast_snapshot(
        "ch1", "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00", "temperature", 10.0
    )
    retention = object.__new__(coord.RetentionCoordinator)
    retention.hass = FakeHass()
    retention._db = db
    retention._purge_days = 0
    retention._diagnostics = None

    result = asyncio.run(retention._async_update_data())

    assert result is None
    # Nothing purged — the ancient row from 2020 is still there.
    rows = db._conn.execute("SELECT COUNT(*) AS n FROM forecast_snapshots").fetchone()
    assert rows["n"] == 1


def test_retention_coordinator_purges_when_purge_days_positive(db):
    """The actual L-10 fix: with purge_days set, this coordinator must
    genuinely call through to purge_older_than() with a real cutoff —
    this is the exact wiring the audit found was missing in production
    (purge_older_than existed and worked, but nothing ever called it)."""
    old_ts = "2020-01-01T00:00:00+00:00"
    recent_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    db.insert_forecast_snapshot("ch1", old_ts, old_ts, "temperature", 10.0)
    db.insert_forecast_snapshot("ch1", recent_ts, recent_ts, "temperature", 12.0)
    # Both default to 'pending'; mark them 'reconciled' so purge isn't
    # blocked by the (correct, separately-tested) pending-row protection
    # — this test is specifically about the purge_days wiring, not that.
    all_ids = [r["id"] for r in db._conn.execute("SELECT id FROM forecast_snapshots")]
    db.mark_forecast_snapshots_status(all_ids, "reconciled")

    retention = object.__new__(coord.RetentionCoordinator)
    retention.hass = FakeHass()
    retention._db = db
    retention._purge_days = 30  # anything older than 30 days back gets purged
    retention._diagnostics = None
    # v0.1.24 (P2-04): RetentionCoordinator now serializes its purge
    # against ModelALearningCoordinator's reconciliation through a shared
    # lock. Constructed explicitly here because this test bypasses
    # __init__ (see this file's docstring).
    retention._retention_lock = asyncio.Lock()

    result = asyncio.run(retention._async_update_data())

    assert result is not None
    assert result["forecast_snapshots"] == 1  # only the 2020 row
    remaining = db._conn.execute("SELECT valid_at FROM forecast_snapshots").fetchall()
    assert len(remaining) == 1
    assert remaining[0]["valid_at"] == recent_ts


# -- ModelBCoordinator previous_probability persistence (L-09) --------------


def test_model_b_coordinator_loads_persisted_probability_once(db):
    db.set_model_b_previous_probability(0.62)
    model_b_coord = object.__new__(coord.ModelBCoordinator)
    model_b_coord.hass = FakeHass()
    model_b_coord._db = db
    model_b_coord._previous_probability = 0.0  # the old, unsafe default
    model_b_coord._state_loaded_from_db = False

    asyncio.run(model_b_coord._async_load_persisted_state_if_needed())

    assert model_b_coord._previous_probability == 0.62
    assert model_b_coord._state_loaded_from_db is True

    # A second call must NOT re-hit the DB / re-overwrite — simulate a
    # local change since the load (as a real scoring cycle would produce)
    # and confirm it's respected, not clobbered by a redundant reload.
    model_b_coord._previous_probability = 0.81
    asyncio.run(model_b_coord._async_load_persisted_state_if_needed())
    assert model_b_coord._previous_probability == 0.81


def test_model_b_coordinator_first_run_with_no_persisted_state_keeps_default(db):
    """No persisted value yet (e.g. genuinely first-ever start) must
    behave exactly like the old always-0.0 default — restart-safety must
    not change first-run behavior."""
    model_b_coord = object.__new__(coord.ModelBCoordinator)
    model_b_coord.hass = FakeHass()
    model_b_coord._db = db
    model_b_coord._previous_probability = 0.0
    model_b_coord._state_loaded_from_db = False

    asyncio.run(model_b_coord._async_load_persisted_state_if_needed())

    assert model_b_coord._previous_probability == 0.0


# -- MeteonomiqsCoordinator hourly-forecast persistence (own finding F-2) ---


class _FakeMeteonomiqsClient:
    def __init__(self, points):
        self._points = points

    async def async_fetch_hourly_forecast(self, *, latitude, longitude):
        return self._points


class _FakeHealth:
    def record_success(self, *, duration_ms=None):
        pass

    def record_error(self, err, *, duration_ms=None):
        pass


def test_meteonomiqs_hourly_forecast_is_persisted_with_prefixed_variable_names(db):
    """Direct regression test for F-2 (own-review finding, not in the
    external audit): the hourly forecast fetch previously discarded its
    own data after parsing — nothing downstream ever read
    last_hourly_forecast. This confirms the fix actually lands rows in
    forecast_snapshots, under the METEONOMIQS_HOURLY_VARIABLE_PREFIX
    names specifically (so Model A's blend — which only recognizes plain
    "pressure"/"precip" names — can never pick these up by accident,
    since Meteonomiqs stays deliberately excluded from
    ALL_FORECAST_SOURCES).
    """
    valid_at = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    points = [
        HourlyForecastPoint(
            valid_at=valid_at,
            mean_sea_level_pressure=1013.5,
            precipitation_sum_mm=0.4,
            precipitation_probability=30.0,
        ),
    ]

    mc = object.__new__(coord.MeteonomiqsCoordinator)
    mc.hass = FakeHass()
    mc._db = db
    mc._client = _FakeMeteonomiqsClient(points)
    mc._latitude = 46.9
    mc._longitude = 7.4
    mc._diagnostics = None
    mc.health = _FakeHealth()
    mc._budget = AnnualCallBudget(1000)
    mc._bonus_tracker = BonusCallTracker()
    mc.last_hourly_forecast = None
    mc._last_successful_call_date = None

    asyncio.run(mc._async_fetch_hourly_forecast(today=date(2026, 7, 25)))

    rows = db._conn.execute(
        "SELECT variable, value FROM forecast_snapshots WHERE source = 'meteonomiqs' "
        "ORDER BY variable"
    ).fetchall()
    variables = {r["variable"]: r["value"] for r in rows}
    assert variables == {
        "meteonomiqs_pressure": 1013.5,
        "meteonomiqs_precip_probability": 30.0,
        "meteonomiqs_precip_sum": 0.4,
    }
    # v0.1.24 (P1-07): this used to assert calls_used == 1 here, because
    # _async_fetch_hourly_forecast called self._budget.record_call()
    # itself. That was the TOCTOU bug: the reservation happened AFTER an
    # awaited HTTP call, so two paths sharing the budget could both pass
    # a check before either committed. Reservation now happens exactly
    # once, synchronously, at the CALLER — so a direct call to the fetch
    # method, as this test makes, correctly records nothing.
    #
    # The L-07 persistence wiring this test also covers is still
    # exercised: the state is written, it simply reflects that no
    # reservation was made on this path.
    assert db.get_annual_call_budget_state("meteonomiqs") == {
        "year": None, "calls_used": 0,
    }
    # And the P1-08 daily marker is now persisted alongside it.
    assert db.get_meteonomiqs_last_successful_call_date() is not None


def test_meteonomiqs_hourly_forecast_never_produces_a_plain_pressure_row(db):
    """Explicit negative check: no row from this source should EVER use
    the bare 'pressure' variable name that Model A's blend recognizes —
    the whole point of the prefix is that this data cannot enter the
    blend even by an accidental future refactor elsewhere."""
    valid_at = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    points = [
        HourlyForecastPoint(
            valid_at=valid_at, mean_sea_level_pressure=1013.5,
            precipitation_sum_mm=0.0, precipitation_probability=0.0,
        ),
    ]
    mc = object.__new__(coord.MeteonomiqsCoordinator)
    mc.hass = FakeHass()
    mc._db = db
    mc._client = _FakeMeteonomiqsClient(points)
    mc._latitude, mc._longitude = 46.9, 7.4
    mc._diagnostics = None
    mc.health = _FakeHealth()
    mc._budget = AnnualCallBudget(1000)
    mc._bonus_tracker = BonusCallTracker()
    mc.last_hourly_forecast = None
    mc._last_successful_call_date = None

    asyncio.run(mc._async_fetch_hourly_forecast(today=date(2026, 7, 25)))

    variables = {
        r["variable"]
        for r in db._conn.execute("SELECT variable FROM forecast_snapshots")
    }
    assert "pressure" not in variables
    assert "precip" not in variables
