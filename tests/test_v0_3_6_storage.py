"""v0.3.6: storage performance without changing any learned or blended value.

What a real 14.5 million row installation showed, and what each test here
pins down:

* 58 % of forecast rows had no value (beyond each model's horizon). They
  are no longer stored, and the one-time maintenance removes the old ones.
* 86 % of rows were 'pending' although Model A can never learn from their
  variable, so the purge could not remove them. New rows of such variables
  are 'not_learned'; the purge protects only learned pending rows.
* The station reference query used substr(valid_at), a full scan, three
  times per poll. It now uses a range on an index.
* A timed-out executor job kept running and holding the database lock, and
  the next cycle queued another behind it. _SingleFlight stops that.

The equivalence claim itself — identical learning selection, buckets,
blend, comparison rows and report — was proven on a copy of the real
database (RELEASE_AUDIT_v0.3.6.md). These tests keep it true on synthetic
data.
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from swissweather_fusion import coordinator as coord
from swissweather_fusion.storage import db as dbmod
from swissweather_fusion.storage.db import (
    LEARNED_VARIABLES,
    STATUS_NOT_LEARNED,
    SwissWeatherDB,
)

NOW = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def indexes(database: SwissWeatherDB) -> set[str]:
    return {
        r["name"]
        for r in database._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'forecast_snapshots'"
        )
    }


def plan(database: SwissWeatherDB, sql: str, params=()) -> str:
    return " | ".join(
        r[3] for r in database._conn.execute(f"EXPLAIN QUERY PLAN {sql}", params)
    )


@pytest.fixture
def db(tmp_path):
    database = SwissWeatherDB(str(tmp_path / "v036.db"))
    yield database
    database.close()


def make_legacy(database: SwissWeatherDB) -> None:
    """Turn a database into the pre-v0.3.6 shape: old index only, no
    maintenance marker. Rows are added by the caller with raw SQL."""
    c = database._conn
    c.execute("DROP INDEX IF EXISTS idx_forecast_valid")
    c.execute("DROP INDEX IF EXISTS idx_forecast_learn_pending")
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_forecast_pending "
        "ON forecast_snapshots(reconciliation_status, valid_at) "
        "WHERE reconciliation_status = 'pending'"
    )
    c.execute("DELETE FROM schema_meta WHERE key = ?", (dbmod._V6_MAINTENANCE_KEY,))
    c.commit()


def legacy_rows(database: SwissWeatherDB, n_hours: int = 30) -> None:
    """Rows the way v0.3.5 stored them: NULLs included, all 'pending'."""
    rows = []
    for k in range(-n_hours, n_hours):
        valid = iso(NOW + timedelta(hours=k))
        issued = iso(NOW + timedelta(hours=k - 3))
        for variable in ("temperature", "humidity", "pressure", "wind_speed", "cloud_cover"):
            for source in ("icon_ch1", "icon_ch2", "srf"):
                value = None if (k + len(source)) % 3 == 0 else float(k + len(variable))
                rows.append((source, issued, valid, variable, value, "scheduled", "pending"))
    database._conn.executemany(
        "INSERT INTO forecast_snapshots (source, issued_at, valid_at, variable, "
        "value, trigger_reason, reconciliation_status) VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    database._conn.commit()


# ---------------------------------------------------------------------------
# constants and inserts
# ---------------------------------------------------------------------------


def test_learned_variables_match_what_learning_reconciles():
    assert tuple(LEARNED_VARIABLES) == tuple(
        coord.ModelALearningCoordinator.RECONCILIATION_MEASUREMENTS
    )


def test_learned_sql_list_is_derived_from_the_constant():
    for variable in LEARNED_VARIABLES:
        assert f"'{variable}'" in dbmod._LEARNED_SQL_LIST
    assert dbmod._LEARNED_SQL_LIST.count("'") == 2 * len(LEARNED_VARIABLES)


def test_bulk_insert_drops_rows_without_a_value(db):
    db.insert_forecast_snapshots_bulk([
        ("srf", iso(NOW), iso(NOW + timedelta(hours=1)), "temperature", 5.0, "scheduled"),
        ("srf", iso(NOW), iso(NOW + timedelta(hours=2)), "temperature", None, "scheduled"),
        ("icon_ch2", iso(NOW), iso(NOW + timedelta(hours=1)), "wind_speed", None,
         "scheduled", iso(NOW), "run"),
        ("icon_ch2", iso(NOW), iso(NOW + timedelta(hours=1)), "wind_speed", 3.0,
         "scheduled", iso(NOW), "run"),
    ])
    rows = db._conn.execute(
        "SELECT variable, value FROM forecast_snapshots ORDER BY id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [("temperature", 5.0), ("wind_speed", 3.0)]


def test_bulk_insert_of_only_empty_rows_is_a_no_op(db):
    db.insert_forecast_snapshots_bulk([
        ("srf", iso(NOW), iso(NOW), "temperature", None, "scheduled"),
    ])
    assert db._conn.execute("SELECT count(*) FROM forecast_snapshots").fetchone()[0] == 0


@pytest.mark.parametrize("width", [6, 8])
def test_initial_status_follows_the_variable(db, width):
    extra = (iso(NOW), "run") if width == 8 else ()
    rows = [
        ("srf", iso(NOW), iso(NOW + timedelta(hours=1)), v, 1.0, "scheduled", *extra)
        for v in (*LEARNED_VARIABLES, "wind_speed", "precipitation", "cloud_cover_low")
    ]
    db.insert_forecast_snapshots_bulk(rows)
    status = dict(db._conn.execute(
        "SELECT variable, reconciliation_status FROM forecast_snapshots"
    ).fetchall())
    for v in LEARNED_VARIABLES:
        assert status[v] == "pending"
    for v in ("wind_speed", "precipitation", "cloud_cover_low"):
        assert status[v] == STATUS_NOT_LEARNED


def test_single_insert_applies_the_same_status_rule(db):
    db.insert_forecast_snapshot("srf", iso(NOW), iso(NOW), "wind_speed", 2.0)
    db.insert_forecast_snapshot("srf", iso(NOW), iso(NOW), "humidity", 70.0)
    status = dict(db._conn.execute(
        "SELECT variable, reconciliation_status FROM forecast_snapshots"
    ).fetchall())
    assert status == {"wind_speed": STATUS_NOT_LEARNED, "humidity": "pending"}


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------


def test_fresh_database_has_the_new_indexes_and_not_the_old(db):
    names = indexes(db)
    assert {"idx_forecast_valid", "idx_forecast_learn_pending",
            "idx_forecast_source_ts"} <= names
    assert "idx_forecast_pending" not in names
    assert db.storage_maintenance_state() == "done"


def test_pending_query_uses_the_partial_index(db):
    sql = (
        f"SELECT * FROM forecast_snapshots WHERE {dbmod._LEARNED_PENDING_SQL} "
        "AND valid_at <= ? AND variable IN (?,?,?) ORDER BY valid_at ASC"
    )
    assert "idx_forecast_learn_pending" in plan(db, sql, (iso(NOW), *LEARNED_VARIABLES))


def test_pending_query_source_carries_the_literal_clause():
    import inspect
    src = inspect.getsource(SwissWeatherDB.get_pending_forecast_snapshots)
    assert "_LEARNED_PENDING_SQL" in src


def test_pending_query_rejects_a_variable_that_is_never_learned(db):
    with pytest.raises(ValueError):
        db.get_pending_forecast_snapshots(
            until_ts=iso(NOW), measurements=("temperature", "wind_speed")
        )


def test_pending_query_returns_only_learned_pending_rows(db):
    legacy_rows(db)
    db.insert_forecast_snapshots_bulk([
        ("srf", iso(NOW), iso(NOW - timedelta(hours=1)), "wind_speed", 1.0, "scheduled"),
    ])
    rows = db.get_pending_forecast_snapshots(
        until_ts=iso(NOW), measurements=LEARNED_VARIABLES
    )
    assert rows
    assert {r["variable"] for r in rows} <= set(LEARNED_VARIABLES)
    assert all(r["valid_at"] <= iso(NOW) for r in rows)
    expected = db._conn.execute(
        "SELECT count(*) FROM forecast_snapshots WHERE reconciliation_status='pending' "
        "AND valid_at <= ? AND variable IN ('temperature','humidity','pressure')",
        (iso(NOW),),
    ).fetchone()[0]
    assert len(rows) == expected


def _legacy_reference(database, variable, prefix, cutoff):
    values = [r[0] for r in database._conn.execute(
        "SELECT value FROM forecast_snapshots WHERE variable = ? AND value IS NOT NULL "
        "AND source != 'blend' AND substr(valid_at, 1, 13) = ? AND issued_at >= ? "
        "ORDER BY value", (variable, prefix, cutoff))]
    return values


def test_reference_range_selects_exactly_what_substr_selected(db):
    base = NOW.replace(hour=10) if NOW.hour != 23 else NOW
    rows = []
    for minute in (0, 30, 59):
        for hour_offset in (-1, 0, 1):
            t = base + timedelta(hours=hour_offset, minutes=minute)
            for source in ("srf", "icon_ch1", "blend"):
                rows.append((source, iso(NOW - timedelta(hours=1)), iso(t),
                             "pressure", 1000.0 + hour_offset * 10 + minute / 100, "scheduled"))
    db.insert_forecast_snapshots_bulk(rows)
    cutoff = iso(NOW - dbmod.REFERENCE_VINTAGE_WINDOW)
    for hour_offset in (-1, 0, 1, 2):
        prefix = (base + timedelta(hours=hour_offset)).strftime("%Y-%m-%dT%H")
        legacy = _legacy_reference(db, "pressure", prefix, cutoff)
        got = db.get_reference_value("pressure", prefix)
        if not legacy:
            assert got is None
        else:
            mid = len(legacy) // 2
            want = legacy[mid] if len(legacy) % 2 else (legacy[mid - 1] + legacy[mid]) / 2
            assert got == pytest.approx(want)
        legacy_all = [r[0] for r in db._conn.execute(
            "SELECT value FROM forecast_snapshots WHERE variable = 'pressure' "
            "AND value IS NOT NULL AND substr(valid_at, 1, 13) = ? ORDER BY value",
            (prefix,))]
        got_all = db.get_reference_pressure_hpa(prefix)
        if not legacy_all:
            assert got_all is None
        else:
            mid = len(legacy_all) // 2
            want = (legacy_all[mid] if len(legacy_all) % 2
                    else (legacy_all[mid - 1] + legacy_all[mid]) / 2)
            assert got_all == pytest.approx(want)


def test_reference_query_is_served_by_the_valid_at_index(db):
    sql = (
        "SELECT value FROM forecast_snapshots WHERE variable = ? AND value IS NOT NULL "
        "AND source != 'blend' AND valid_at >= ? AND valid_at < ? AND issued_at >= ? "
        "ORDER BY value"
    )
    assert "idx_forecast_valid" in plan(db, sql, ("pressure", "a", "b", "c"))


def test_hour_range_bounds_are_a_prefix_match():
    lo, hi = dbmod._hour_range("2026-10-31T10")
    for inside in ("2026-10-31T10", "2026-10-31T10:00:00+00:00", "2026-10-31T10:59:59.999999+00:00"):
        assert lo <= inside < hi
    for outside in ("2026-10-31T09:59:59+00:00", "2026-10-31T11:00:00+00:00", "2026-10-31T1"):
        assert not (lo <= outside < hi)


def test_no_substr_on_valid_at_remains_in_a_where_clause():
    import inspect
    sql_lines = [
        line.strip() for line in inspect.getsource(dbmod).splitlines()
        if line.strip().startswith(('"', 'f"'))
    ]
    assert not [l for l in sql_lines if "substr(valid_at, 1, 13) = ?" in l]
    assert not [l for l in sql_lines if "substr(f.valid_at, 1, 13) = substr" in l]


# ---------------------------------------------------------------------------
# purge and reset
# ---------------------------------------------------------------------------


def test_purge_protects_only_learned_pending_rows(db):
    old = iso(NOW - timedelta(days=100))
    c = db._conn
    c.executemany(
        "INSERT INTO forecast_snapshots (source, issued_at, valid_at, variable, value, "
        "reconciliation_status) VALUES ('srf', ?, ?, ?, 1.0, ?)",
        [
            (old, old, "temperature", "pending"),       # kept: learning may still need it
            (old, old, "temperature", "reconciled"),    # purged
            (old, old, "wind_speed", "pending"),        # purged: legacy, never learnable
            (old, old, "wind_speed", STATUS_NOT_LEARNED),  # purged
            (iso(NOW), iso(NOW), "wind_speed", STATUS_NOT_LEARNED),  # kept: recent
        ],
    )
    c.commit()
    deleted = db.purge_older_than(iso(NOW - timedelta(days=90)))
    assert deleted["forecast_snapshots"] == 3
    left = {(r[0], r[1]) for r in c.execute(
        "SELECT variable, reconciliation_status FROM forecast_snapshots")}
    assert left == {("temperature", "pending"), ("wind_speed", STATUS_NOT_LEARNED)}


def test_purge_runs_in_batches_each_committed(db, monkeypatch):
    monkeypatch.setattr(dbmod, "PURGE_BATCH_ROWS", 7)
    old = iso(NOW - timedelta(days=100))
    db._conn.executemany(
        "INSERT INTO forecast_snapshots (source, issued_at, valid_at, variable, value, "
        "reconciliation_status) VALUES ('srf', ?, ?, 'wind_speed', 1.0, 'not_learned')",
        [(old, old)] * 30,
    )
    db._conn.commit()
    transactions = []
    real = db._transaction

    def counting():
        transactions.append(1)
        return real()

    monkeypatch.setattr(db, "_transaction", counting)
    deleted = db.purge_older_than(iso(NOW - timedelta(days=90)))
    assert deleted["forecast_snapshots"] == 30
    # 30 rows in batches of 7: 5 forecast batches, plus the other tables.
    assert len(transactions) == 6
    assert db._conn.execute("SELECT count(*) FROM forecast_snapshots").fetchone()[0] == 0


def test_purge_query_uses_the_valid_at_index(db):
    sql = (
        "SELECT id FROM forecast_snapshots WHERE valid_at < ? "
        f"AND NOT ({dbmod._LEARNED_PENDING_SQL}) LIMIT 5"
    )
    assert "idx_forecast_valid" in plan(db, sql, ("x",))


def test_reset_reopens_only_learned_variables(db):
    recent = iso(NOW - timedelta(days=1))
    db._conn.executemany(
        "INSERT INTO forecast_snapshots (source, issued_at, valid_at, variable, value, "
        "reconciliation_status) VALUES ('srf', ?, ?, ?, 1.0, ?)",
        [
            (recent, recent, "temperature", "reconciled"),
            (recent, recent, "wind_speed", STATUS_NOT_LEARNED),
        ],
    )
    db._conn.commit()
    db.reset_all_learning()
    status = dict(db._conn.execute(
        "SELECT variable, reconciliation_status FROM forecast_snapshots"
    ).fetchall())
    assert status == {"temperature": "pending", "wind_speed": STATUS_NOT_LEARNED}


def test_reset_pressure_cleanup_still_matches_by_hour(db):
    """The correlated range must still pair an observation at :07 with the
    forecasts valid at :00 of the same hour."""
    hour = NOW - timedelta(hours=2)
    db.insert_forecast_snapshots_bulk([
        (s, iso(hour - timedelta(hours=1)), iso(hour), "pressure", 1013.0, "scheduled")
        for s in ("srf", "icon_ch1")
    ])
    db.insert_station_observation(iso(hour + timedelta(minutes=7)), 10.0, 70.0, 1075.0)
    db.insert_station_observation(iso(hour + timedelta(minutes=12)), 10.0, 70.0, 1013.5)
    db.reset_all_learning()
    pressures = sorted(
        (r[0] is None) for r in db._conn.execute("SELECT pressure FROM station_observations")
    )
    assert pressures == [False, True]  # the 1075 reading was cleared, 1013.5 kept


# ---------------------------------------------------------------------------
# one-time maintenance
# ---------------------------------------------------------------------------


def run_maintenance(database: SwissWeatherDB) -> list[dict]:
    steps = []
    while True:
        steps.append(database.run_storage_maintenance_step())
        if steps[-1]["state"] == "done":
            return steps
        assert len(steps) < 10_000


def test_populated_legacy_database_keeps_the_old_index_at_startup(tmp_path):
    path = str(tmp_path / "legacy.db")
    first = SwissWeatherDB(path)
    make_legacy(first)
    legacy_rows(first)
    first.close()
    reopened = SwissWeatherDB(path)
    try:
        names = indexes(reopened)
        assert "idx_forecast_pending" in names
        assert "idx_forecast_valid" not in names
        assert reopened.storage_maintenance_state() == "delete_nulls:0"
    finally:
        reopened.close()


def test_maintenance_removes_only_empty_rows_and_swaps_indexes(db, monkeypatch):
    monkeypatch.setattr(dbmod, "MAINTENANCE_BATCH_IDS", 50)
    make_legacy(db)
    legacy_rows(db)
    c = db._conn
    before = [tuple(r) for r in c.execute(
        "SELECT * FROM forecast_snapshots WHERE value IS NOT NULL ORDER BY id")]
    nulls = c.execute("SELECT count(*) FROM forecast_snapshots WHERE value IS NULL").fetchone()[0]
    assert nulls > 0
    steps = run_maintenance(db)
    assert sum(s["deleted"] for s in steps) == nulls
    after = [tuple(r) for r in c.execute("SELECT * FROM forecast_snapshots ORDER BY id")]
    assert after == before
    names = indexes(db)
    assert {"idx_forecast_valid", "idx_forecast_learn_pending"} <= names
    assert "idx_forecast_pending" not in names
    assert [s["state"] for s in steps][-3:] == ["index_valid", "index_learn_pending", "done"]
    # done is final, and a further step is a no-op
    assert db.run_storage_maintenance_step() == {"state": "done", "deleted": 0}


def test_learning_selection_is_identical_before_and_after_maintenance(db):
    make_legacy(db)
    legacy_rows(db)
    before = db.get_pending_forecast_snapshots(until_ts=iso(NOW), measurements=LEARNED_VARIABLES)
    run_maintenance(db)
    after = db.get_pending_forecast_snapshots(until_ts=iso(NOW), measurements=LEARNED_VARIABLES)
    assert [r["id"] for r in after] == [r["id"] for r in before if r["value"] is not None]


def test_blend_is_identical_before_and_after_maintenance(db):
    from tests.test_v0_2_1_fixes import FakeHass

    make_legacy(db)
    legacy_rows(db)
    blend = coord.ModelABlendCoordinator(FakeHass(), db)
    before = blend._compute_blend()
    run_maintenance(db)
    after = blend._compute_blend()
    for key in ("current", "hourly_forecast", "daily_forecast", "expert_weights"):
        assert after[key] == before[key]


def test_maintenance_resumes_after_a_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(dbmod, "MAINTENANCE_BATCH_IDS", 40)
    path = str(tmp_path / "resume.db")
    first = SwissWeatherDB(path)
    make_legacy(first)
    legacy_rows(first)
    first.run_storage_maintenance_step()
    first.run_storage_maintenance_step()
    state = first.storage_maintenance_state()
    assert state == "delete_nulls:80"
    first.close()
    second = SwissWeatherDB(path)
    try:
        assert second.storage_maintenance_state() == state
        run_maintenance(second)
        assert second._conn.execute(
            "SELECT count(*) FROM forecast_snapshots WHERE value IS NULL").fetchone()[0] == 0
    finally:
        second.close()


def test_restart_after_maintenance_does_not_bring_the_old_index_back(tmp_path):
    path = str(tmp_path / "done.db")
    first = SwissWeatherDB(path)
    make_legacy(first)
    legacy_rows(first)
    run_maintenance(first)
    # a downgrade to v0.3.5 and back would recreate the old index
    first._conn.execute(
        "CREATE INDEX idx_forecast_pending ON forecast_snapshots"
        "(reconciliation_status, valid_at) WHERE reconciliation_status = 'pending'"
    )
    first._conn.commit()
    first.close()
    second = SwissWeatherDB(path)
    try:
        assert "idx_forecast_pending" not in indexes(second)
    finally:
        second.close()


def test_unknown_maintenance_state_restarts_the_sequence(db):
    db._conn.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, 'something')",
        (dbmod._V6_MAINTENANCE_KEY,),
    )
    db._conn.commit()
    assert db.run_storage_maintenance_step()["state"] == "delete_nulls:0"
    run_maintenance(db)
    assert db.storage_maintenance_state() == "done"


def test_maintenance_step_failure_rolls_back_and_keeps_progress(db, monkeypatch):
    make_legacy(db)
    legacy_rows(db)
    real_conn = db._conn

    class Failing:
        def __init__(self, conn):
            self._c = conn

        def execute(self, sql, *a):
            if sql.startswith("DELETE FROM forecast_snapshots WHERE id >="):
                raise sqlite3.OperationalError("disk I/O error")
            return self._c.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(self._c, name)

    db._conn = Failing(real_conn)
    with pytest.raises(sqlite3.OperationalError):
        db.run_storage_maintenance_step()
    db._conn = real_conn
    assert db.storage_maintenance_state() == "delete_nulls:0"
    run_maintenance(db)


def test_schema_version_is_six(db):
    assert dbmod.SCHEMA_VERSION == 6
    assert db._get_meta("schema_version") == "6"


# ---------------------------------------------------------------------------
# retention coordinator
# ---------------------------------------------------------------------------


class LoopHass:
    """Runs executor jobs in real threads, like Home Assistant."""

    def __init__(self):
        self.data = {}
        self.background = []

    def async_add_executor_job(self, func, *args):
        return asyncio.get_running_loop().run_in_executor(None, func, *args)

    def async_create_background_task(self, coro, name):
        task = asyncio.ensure_future(coro)
        self.background.append((name, task))
        return task


def test_retention_defers_purge_and_starts_maintenance(db, monkeypatch):
    make_legacy(db)
    legacy_rows(db)
    hass = LoopHass()
    c = coord.RetentionCoordinator(hass, db, purge_days=90)
    monkeypatch.setattr(c, "MAINTENANCE_START_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(c, "MAINTENANCE_PAUSE_SECONDS", 0.0)
    purged = []
    real_purge = db.purge_older_than
    monkeypatch.setattr(db, "purge_older_than", lambda cutoff: purged.append(cutoff) or real_purge(cutoff))

    async def scenario():
        assert await c._async_update_data() is None
        assert purged == []
        assert len(hass.background) == 1
        # a second cycle while it runs does not start another
        await c._async_update_data()
        assert len(hass.background) == 1
        await hass.background[0][1]
        assert c.maintenance_state == "done"
        assert len(purged) == 1  # the maintenance ran the deferred purge
        result = await c._async_update_data()
        assert "forecast_snapshots" in result

    asyncio.run(scenario())
    assert db.storage_maintenance_state() == "done"


def test_retention_maintenance_never_blocks_setup(db, monkeypatch):
    make_legacy(db)
    legacy_rows(db)
    hass = LoopHass()
    c = coord.RetentionCoordinator(hass, db, purge_days=90)

    async def scenario():
        await asyncio.wait_for(c._async_update_data(), 5)
        task = hass.background[0][1]
        assert not task.done()  # waiting out its start delay
        await c.async_shutdown()
        assert task.cancelled()

    asyncio.run(scenario())
    assert db.storage_maintenance_state() == "delete_nulls:0"


def test_retention_maintenance_failure_is_logged_and_resumable(db, monkeypatch, caplog):
    make_legacy(db)
    legacy_rows(db)
    hass = LoopHass()
    c = coord.RetentionCoordinator(hass, db, purge_days=90)
    monkeypatch.setattr(c, "MAINTENANCE_START_DELAY_SECONDS", 0.0)
    calls = {"n": 0}
    real_step = db.run_storage_maintenance_step

    def flaky():
        calls["n"] += 1
        if calls["n"] == 2:
            raise sqlite3.OperationalError("database is locked")
        return real_step()

    monkeypatch.setattr(db, "run_storage_maintenance_step", flaky)

    async def scenario():
        await c._async_update_data()
        await hass.background[0][1]
        assert c.maintenance_state != "done"
        await c._async_update_data()
        await hass.background[1][1]
        assert c.maintenance_state == "done"

    asyncio.run(scenario())
    assert "resumes on the next retention cycle" in caplog.text


# ---------------------------------------------------------------------------
# single flight
# ---------------------------------------------------------------------------


def test_single_flight_refuses_a_second_job_while_busy():
    async def scenario():
        flight = coord._SingleFlight("x")
        gate = asyncio.Event()
        fut = flight.start(gate.wait())
        assert flight.busy
        second = asyncio.sleep(0)
        with pytest.raises(RuntimeError):
            flight.start(second)
        second.close()
        gate.set()
        await fut
        assert not flight.busy

    asyncio.run(scenario())


def test_single_flight_timeout_does_not_cancel_the_job():
    async def scenario():
        flight = coord._SingleFlight("x")
        done = asyncio.Event()

        async def job():
            await asyncio.sleep(0.05)
            done.set()
            return 7

        fut = flight.start(job())
        with pytest.raises(TimeoutError):
            await flight.wait(fut, 0.01)
        assert flight.busy
        assert await fut == 7
        assert done.is_set()

    asyncio.run(scenario())


def test_single_flight_retrieves_a_late_exception(caplog):
    async def scenario():
        flight = coord._SingleFlight("x")

        async def job():
            await asyncio.sleep(0.02)
            raise ValueError("late")

        fut = flight.start(job())
        with pytest.raises(TimeoutError):
            await flight.wait(fut, 0.005)
        await asyncio.sleep(0.05)
        assert fut.done()

    asyncio.run(scenario())
    assert "never retrieved" not in caplog.text


def test_blend_serves_previous_result_while_a_slow_job_runs(db, monkeypatch):
    hass = LoopHass()
    blend = coord.ModelABlendCoordinator(hass, db)
    release = threading.Event()
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        if calls["n"] == 2:
            release.wait(5)
        return {"cycle": calls["n"]}

    monkeypatch.setattr(blend, "_compute_blend", compute)

    async def scenario():
        blend.data = await blend._async_update_data()
        assert blend.data == {"cycle": 1}
        monkeypatch.setattr(blend._flight, "wait",
                            lambda fut, timeout: coord._SingleFlight.wait(blend._flight, fut, 0.05))
        # cycle 2 times out: previous blend served, entity stays available
        assert await blend._async_update_data() == {"cycle": 1}
        # cycle 3 while 2 is still running: no second job is queued
        assert await blend._async_update_data() == {"cycle": 1}
        assert calls["n"] == 2
        release.set()
        while blend._flight.busy:
            await asyncio.sleep(0.01)
        assert await blend._async_update_data() == {"cycle": 3}

    asyncio.run(scenario())


def test_blend_fails_once_the_previous_result_is_too_old(db, monkeypatch):
    blend = coord.ModelABlendCoordinator(LoopHass(), db)
    blend.data = {"cycle": 1}
    blend._last_success_monotonic = coord.time.monotonic() - blend.BLEND_MAX_STALE_SECONDS - 1
    with pytest.raises(coord.UpdateFailed):
        blend._serve_previous("test")
    blend.data = None
    blend._last_success_monotonic = coord.time.monotonic()
    with pytest.raises(coord.UpdateFailed):
        blend._serve_previous("test")


def test_learning_holds_the_shared_lock_until_the_job_finishes(db, monkeypatch):
    hass = LoopHass()
    lock = asyncio.Lock()
    learn = coord.ModelALearningCoordinator(hass, db, reconcile_lock=lock)
    release = threading.Event()
    monkeypatch.setattr(learn, "_reconcile", lambda: release.wait(5) and NOW)
    monkeypatch.setattr(learn._flight, "wait",
                        lambda fut, timeout: coord._SingleFlight.wait(learn._flight, fut, 0.05))

    async def scenario():
        with pytest.raises(coord.UpdateFailed):
            await learn._async_update_data()
        # timed out, but the job still runs: retention must still be kept out
        assert lock.locked()
        learn.data = "previous"
        assert await learn._async_update_data() == "previous"  # skipped, not queued
        release.set()
        while learn._flight.busy:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0)
        assert not lock.locked()

    asyncio.run(scenario())


def test_model_b_skips_while_previous_scoring_runs(db, monkeypatch):
    hass = LoopHass()
    station = coord.StationCoordinator(hass, db, "sensor.t", "sensor.h", "sensor.p")
    cp = coord.CombiPrecipCoordinator(hass, db, 47.0, 8.0)
    mb = coord.MeteoblueCoordinator(hass, db, 47.0, 8.0, "k")
    mn = coord.MeteonomiqsCoordinator(hass, db, 47.0, 8.0, "k")
    model_b = coord.ModelBCoordinator(hass, db, station, cp, mb, mn)
    gate = asyncio.Event()
    runs = {"n": 0}

    async def cycle():
        runs["n"] += 1
        await gate.wait()
        return 0.25

    monkeypatch.setattr(model_b, "_async_scoring_cycle", cycle)

    async def scenario():
        first = asyncio.ensure_future(model_b._async_update_data())
        await asyncio.sleep(0)
        with pytest.raises(coord.UpdateFailed):  # no previous score yet
            await model_b._async_update_data()
        model_b.data = 0.1
        assert await model_b._async_update_data() == 0.1
        gate.set()
        assert await first == 0.25
        assert runs["n"] == 1

    asyncio.run(scenario())


def test_station_readings_survive_a_slow_database_write(db, monkeypatch, caplog):
    """The readings come from Home Assistant sensors; a slow write must not
    make the station entities unavailable, and the write must still land."""
    from tests.test_v0_1_26_construction import FakeState

    hass = LoopHass()
    states = {
        "sensor.t": FakeState("12.0", "°C"),
        "sensor.h": FakeState("60.0", "%"),
        "sensor.p": FakeState("1015.0", "hPa"),
    }
    hass.states = type("S", (), {"get": lambda self, e: states.get(e)})()
    station = coord.StationCoordinator(hass, db, "sensor.t", "sensor.h", "sensor.p")
    real_insert = db.insert_station_observation
    written = threading.Event()

    def slow_insert(*args):
        threading.Event().wait(0.3)
        real_insert(*args)
        written.set()

    monkeypatch.setattr(db, "insert_station_observation", slow_insert)
    real_timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda seconds: real_timeout(min(seconds, 0.05)))

    async def scenario():
        result = await station._async_update_data()
        assert result["temperature"] == pytest.approx(12.0)
        assert not written.is_set()
        while not written.is_set():
            await asyncio.sleep(0.02)

    asyncio.run(scenario())
    assert db.get_station_observations_since("1970-01-01T00:00:00+00:00")
    assert "completes in the background" in caplog.text


def test_a_late_station_write_failure_is_still_logged(db, monkeypatch, caplog):
    from tests.test_v0_1_26_construction import FakeState

    hass = LoopHass()
    states = {"sensor.t": FakeState("12.0", "°C")}
    hass.states = type("S", (), {"get": lambda self, e: states.get(e)})()
    station = coord.StationCoordinator(hass, db, "sensor.t", "sensor.h", "sensor.p")

    def failing_insert(*args):
        threading.Event().wait(0.2)
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(db, "insert_station_observation", failing_insert)
    real_timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda seconds: real_timeout(min(seconds, 0.05)))

    async def scenario():
        await station._async_update_data()
        await asyncio.sleep(0.4)

    asyncio.run(scenario())
    assert "Background database write failed" in caplog.text


def test_storage_sensor_shows_maintenance_progress_and_fresh_counts(db, monkeypatch):
    from swissweather_fusion.sensor import StorageSizeSensor

    make_legacy(db)
    legacy_rows(db)
    total = db._conn.execute("SELECT count(*) FROM forecast_snapshots").fetchone()[0]
    hass = LoopHass()
    c = coord.RetentionCoordinator(hass, db, purge_days=90)
    monkeypatch.setattr(c, "MAINTENANCE_START_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(c, "MAINTENANCE_PAUSE_SECONDS", 0.0)
    sensor = object.__new__(StorageSizeSensor)
    sensor._runtime = {"retention_coordinator": c}

    async def scenario():
        await c._async_update_data()
        attrs = StorageSizeSensor.extra_state_attributes.fget(sensor)
        assert attrs["storage_maintenance"] == "delete_nulls:0"
        assert attrs["forecast_snapshots_rows"] == total
        await hass.background[0][1]

    asyncio.run(scenario())
    attrs = StorageSizeSensor.extra_state_attributes.fget(sensor)
    assert attrs["storage_maintenance"] == "done"
    assert attrs["forecast_snapshots_rows"] < total  # refreshed, not next day
