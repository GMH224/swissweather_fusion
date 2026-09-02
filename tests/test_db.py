import os
import shutil
import tempfile

import pytest

from swissweather_fusion.storage.db import BucketKey, SwissWeatherDB


@pytest.fixture
def db():
    path = tempfile.mktemp(suffix=".db")
    database = SwissWeatherDB(path)
    yield database
    database.close()
    os.remove(path)


def test_creates_missing_parent_directory():
    """v0.1.19 regression test: found via a real Home Assistant
    functional test run, not static review — sqlite3.connect() does not
    create missing parent directories, and this integration's real call
    site points at HA's `.storage/` directory. That reliably exists in a
    normal HA install (core creates it during its own startup, before
    any integration's async_setup_entry runs), but a fresh test instance
    without a pre-existing `.storage/` directory reproduced an unhandled
    sqlite3.OperationalError immediately. Confirms SwissWeatherDB no
    longer depends on that external assumption.
    """
    base = tempfile.mkdtemp()
    try:
        nested_path = os.path.join(base, "does", "not", "exist", "yet", "test.db")
        assert not os.path.exists(os.path.dirname(nested_path))
        database = SwissWeatherDB(nested_path)
        try:
            database.insert_station_observation(
                "2026-07-25T12:00:00Z", 21.5, 55.0, 1015.2
            )
            row = database.get_latest_station_observation()
            assert row["temperature"] == 21.5
        finally:
            database.close()
        assert os.path.exists(nested_path)
    finally:
        shutil.rmtree(base)


def test_station_observation_roundtrip(db):
    db.insert_station_observation("2026-07-25T12:00:00Z", 21.5, 55.0, 1015.2)
    row = db.get_latest_station_observation()
    assert row["temperature"] == 21.5
    assert row["humidity"] == 55.0
    assert row["pressure"] == 1015.2


def test_forecast_snapshot_roundtrip(db):
    db.insert_forecast_snapshot(
        "ch1", "2026-07-25T09:00:00Z", "2026-07-25T15:00:00Z", "temperature", 22.1
    )
    rows = db.get_forecast_values_for_valid_at("ch1", "temperature", "2026-07-25T15:00:00Z")
    assert len(rows) == 1
    assert rows[0]["value"] == 22.1
    assert rows[0]["trigger_reason"] == "scheduled"


def test_forecast_snapshot_bulk_insert(db):
    rows = [
        ("ch2", "2026-07-25T00:00:00Z", "2026-07-26T00:00:00Z", "temperature", 18.0, "scheduled"),
        ("ch2", "2026-07-25T00:00:00Z", "2026-07-26T01:00:00Z", "temperature", 17.5, "scheduled"),
    ]
    db.insert_forecast_snapshots_bulk(rows)
    result = db.get_forecast_values_for_valid_at("ch2", "temperature", "2026-07-26T00:00:00Z")
    assert len(result) == 1
    assert result[0]["value"] == 18.0


def test_bucket_stats_upsert(db):
    key = BucketKey(
        hour_of_day=15, season="JJA", lead_time_bucket="short", source="ch1", measurement="temperature"
    )
    assert db.get_bucket_stats(key) is None

    db.upsert_bucket_stats(
        key, ema_bias=0.4, ema_abs_error=0.9, ema_weight=1.1, sample_count=3,
        last_updated="2026-07-25T15:00:00Z",
    )
    stats = db.get_bucket_stats(key)
    assert stats.sample_count == 3
    assert stats.ema_bias == 0.4

    db.upsert_bucket_stats(
        key, ema_bias=0.5, ema_abs_error=0.8, ema_weight=1.2, sample_count=4,
        last_updated="2026-07-25T16:00:00Z",
    )
    stats = db.get_bucket_stats(key)
    assert stats.sample_count == 4
    assert stats.ema_bias == 0.5


def test_storm_events_and_predictions(db):
    event_id = db.insert_storm_event("2026-07-25T17:00:00Z", "2026-07-25T18:00:00Z", 2.1, 3.5, 12.0)
    assert event_id == 1

    db.insert_storm_prediction("2026-07-25T16:55:00Z", 0.72, {"dp30": -1.2, "dh30": 9.0})
    predictions = db.get_storm_predictions_since("2026-07-25T00:00:00Z")
    assert len(predictions) == 1
    assert predictions[0]["probability"] == 0.72


def test_radar_observation_roundtrip(db):
    # v0.1.24 (P1-14): renamed — the value is mm accumulated over the
    # preceding hour (MeteoSwiss CPC), not an instantaneous mm/h rate.
    db.insert_radar_observation("2026-07-25T16:55:00Z", 4.2, "rain", 9)
    row = db.get_latest_radar_observation()
    assert row["precip_accum_mm_1h"] == 4.2
    assert row["quality"] == 9
    assert row["precip_type"] == "rain"


def test_purge_touches_only_high_volume_tables(db):
    db.insert_station_observation("2026-07-25T12:00:00Z", 20.0, 50.0, 1013.0)
    db.insert_forecast_snapshot("ch1", "2026-07-25T09:00:00Z", "2026-07-25T15:00:00Z", "temperature", 22.0)
    db.insert_radar_observation("2026-07-25T12:00:00Z", 1.0, None)
    db.insert_storm_prediction("2026-07-25T12:00:00Z", 0.3, {})
    key = BucketKey(hour_of_day=12, season="JJA", lead_time_bucket="short", source="ch1", measurement="temperature")
    db.upsert_bucket_stats(key, 0.1, 0.2, 1.0, 1, "2026-07-25T12:00:00Z")
    db.insert_storm_event("2026-07-25T12:00:00Z", None, 1.0, 1.0, 1.0)

    # v0.1.23 fix (L-10): forecast_snapshots rows still 'pending'
    # reconciliation are protected from purge regardless of age — mark
    # this one 'reconciled' first so the original assertion (it gets
    # purged past cutoff) still reflects the intended, now-explicit
    # behavior instead of accidentally passing either way.
    row = db.get_pending_forecast_snapshots(
        until_ts="2027-01-01T00:00:00Z", measurements=("temperature",)
    )[0]
    db.mark_forecast_snapshots_status([row["id"]], "reconciled")

    deleted = db.purge_older_than("2027-01-01T00:00:00Z")

    assert deleted["station_observations"] == 1
    assert deleted["forecast_snapshots"] == 1
    assert deleted["radar_observations"] == 1
    assert deleted["storm_predictions"] == 1
    # Never purged, regardless of cutoff:
    assert db.get_bucket_stats(key) is not None
    assert len(db.get_all_storm_events()) == 1


def test_purge_protects_pending_forecast_snapshots_regardless_of_age(db):
    """v0.1.23 fix (L-10, audit's explicit recommendation): a
    forecast_snapshots row Model A learning is still waiting to
    retry-match must never be purged out from under it just because
    purge_days is shorter than RETRY_GIVE_UP_AGE — that would silently
    convert a retryable gap into a permanently lost learning sample."""
    db.insert_forecast_snapshot("ch1", "2026-07-25T09:00:00Z", "2026-07-25T15:00:00Z", "temperature", 22.0)
    # Still 'pending' by default — never explicitly reconciled or skipped.

    deleted = db.purge_older_than("2027-01-01T00:00:00Z")

    assert deleted["forecast_snapshots"] == 0
    rows = db.get_pending_forecast_snapshots(
        until_ts="2027-01-01T00:00:00Z", measurements=("temperature",)
    )
    assert len(rows) == 1


def test_storage_ordering_unaffected_by_dst_transition_instant(db):
    """Requested edge cases: winter->summer and summer->winter transitions.
    All storage here is UTC ISO8601 strings — since UTC has no DST, there
    is nothing for a "transition" to disrupt at the storage layer. This
    test proves that directly: inserting a continuous run of hourly
    station observations spanning the exact UTC instant of both 2026
    European DST transitions, then confirming purge_older_than's simple
    string-comparison cutoff still keeps/deletes exactly the rows it
    should — no corruption, no ordering surprises, no crash.
    """
    # Spring-forward instant is 2026-03-29T01:00:00Z (Europe/Zurich
    # switches CET->CEST). Insert a run of hours straddling it.
    spring_hours = [
        f"2026-03-29T{h:02d}:00:00+00:00" for h in range(0, 4)
    ]
    for ts in spring_hours:
        db.insert_station_observation(ts, 10.0, 60.0, 1015.0)

    # Fall-back instant is 2026-10-25T01:00:00Z (Europe/Zurich switches
    # CEST->CET). Insert a run of hours straddling it too.
    fall_hours = [
        f"2026-10-25T{h:02d}:00:00+00:00" for h in range(0, 4)
    ]
    for ts in fall_hours:
        db.insert_station_observation(ts, 8.0, 65.0, 1018.0)

    all_rows = db.get_station_observations_since("2026-01-01T00:00:00+00:00")
    assert len(all_rows) == 8  # all 8 inserts present, nothing silently lost

    # Purge everything before the fall-back run — should remove exactly
    # the 4 spring-transition rows, keep exactly the 4 fall-transition
    # rows. A plain ISO8601 UTC string comparison has no DST ambiguity to
    # get this wrong.
    deleted = db.purge_older_than("2026-10-25T00:00:00+00:00")
    assert deleted["station_observations"] == 4
    remaining = db.get_station_observations_since("2026-01-01T00:00:00+00:00")
    assert len(remaining) == 4
    assert all(row["ts"].startswith("2026-10-25") for row in remaining)


def test_get_station_observations_between(db):
    db.insert_station_observation("2026-07-25T10:00:00+00:00", 18.0, 50.0, 1013.0)
    db.insert_station_observation("2026-07-25T12:00:00+00:00", 20.0, 48.0, 1012.5)
    db.insert_station_observation("2026-07-25T14:00:00+00:00", 22.0, 45.0, 1012.0)

    rows = db.get_station_observations_between(
        "2026-07-25T11:00:00+00:00", "2026-07-25T13:00:00+00:00"
    )
    assert len(rows) == 1
    assert rows[0]["ts"] == "2026-07-25T12:00:00+00:00"


def test_reconciliation_watermark_methods_are_gone(db):
    """v0.1.24 cleanup (IND-10): the watermark accessors implemented the
    pre-v0.1.23 reconciliation design that reconciliation_status
    replaced. They had zero production callers afterwards and actively
    misled readers into thinking two competing reconciliation mechanisms
    coexisted.

    This test now asserts their ABSENCE rather than their behaviour, so
    that reintroducing them is a deliberate act rather than an
    accident.
    """
    assert not hasattr(db, "get_reconciliation_watermark")
    assert not hasattr(db, "set_reconciliation_watermark")


def test_get_pending_forecast_snapshots_filters_by_measurement_and_window(db):
    """v0.1.23: renamed/rebuilt from the old
    get_forecast_snapshots_to_reconcile(since_ts=...) — see L-01/L-02 in
    the remediation audit for why the since_ts lower bound was removed
    entirely (status-based filtering replaces it). Newly-inserted rows
    default to reconciliation_status='pending', so this still needs no
    explicit status argument to find them."""
    db.insert_forecast_snapshot("ch1", "2026-07-25T09:00:00+00:00", "2026-07-25T12:00:00+00:00", "temperature", 22.0)
    db.insert_forecast_snapshot("ch1", "2026-07-25T09:00:00+00:00", "2026-07-25T12:00:00+00:00", "precip", 0.0)
    db.insert_forecast_snapshot("ch2", "2026-07-25T06:00:00+00:00", "2026-07-25T12:30:00+00:00", "humidity", 55.0)
    db.insert_forecast_snapshot("ch1", "2026-07-25T09:00:00+00:00", "2026-07-26T12:00:00+00:00", "temperature", 20.0)

    rows = db.get_pending_forecast_snapshots(
        until_ts="2026-07-25T13:00:00+00:00",
        measurements=("temperature", "humidity", "pressure"),
    )
    # precip is excluded (no station ground truth for it), and the row
    # valid_at 2026-07-26 is outside the until_ts window.
    variables = sorted(r["variable"] for r in rows)
    assert variables == ["humidity", "temperature"]


def test_get_pending_forecast_snapshots_excludes_already_reconciled_or_skipped(db):
    """v0.1.23 (L-01 fix, direct coverage): once a row's status leaves
    'pending', it must never be selected again by
    get_pending_forecast_snapshots() regardless of its valid_at — this is
    the actual guarantee that prevents re-learning a row (the old
    watermark design could re-select an already-processed row; per-row
    status structurally cannot)."""
    db.insert_forecast_snapshot("ch1", "2026-07-25T09:00:00+00:00", "2026-07-25T12:00:00+00:00", "temperature", 22.0)
    db.insert_forecast_snapshot("ch1", "2026-07-25T09:00:00+00:00", "2026-07-25T13:00:00+00:00", "temperature", 21.0)
    rows = db.get_pending_forecast_snapshots(
        until_ts="2026-07-25T23:59:59+00:00", measurements=("temperature",)
    )
    assert len(rows) == 2
    reconciled_id = rows[0]["id"]
    skipped_id = rows[1]["id"]

    db.mark_forecast_snapshots_status([reconciled_id], "reconciled")
    db.mark_forecast_snapshots_status([skipped_id], "skipped")

    rows_again = db.get_pending_forecast_snapshots(
        until_ts="2026-07-25T23:59:59+00:00", measurements=("temperature",)
    )
    assert rows_again == []


def test_get_pending_forecast_snapshots_finds_late_arriving_row(db):
    """v0.1.23 (L-02 fix, direct coverage): a row inserted AFTER other,
    later-valid_at rows have already been marked reconciled must still be
    found — this is the scenario the old strict `valid_at > since_ts`
    watermark query could never satisfy once the watermark had advanced
    past the late row's valid_at."""
    db.insert_forecast_snapshot("ch1", "2026-07-25T06:00:00+00:00", "2026-07-25T15:00:00+00:00", "temperature", 22.0)
    rows = db.get_pending_forecast_snapshots(
        until_ts="2026-07-25T23:59:59+00:00", measurements=("temperature",)
    )
    db.mark_forecast_snapshots_status([r["id"] for r in rows], "reconciled")

    # A forecast for an EARLIER valid_at arrives late (e.g. a slow ingest
    # path), inserted only now, well after the row above was reconciled.
    db.insert_forecast_snapshot("ch1", "2026-07-25T06:00:00+00:00", "2026-07-25T09:00:00+00:00", "temperature", 19.0)

    late_rows = db.get_pending_forecast_snapshots(
        until_ts="2026-07-25T23:59:59+00:00", measurements=("temperature",)
    )
    assert len(late_rows) == 1
    assert late_rows[0]["valid_at"] == "2026-07-25T09:00:00+00:00"


def test_get_forecast_snapshots_in_window_returns_everything_ordered_for_grouping(db):
    """v0.1.13: the bulk-fetch replacement for what used to be thousands
    of individual round-trip queries per blend cycle. Confirms it returns
    every row in the window (all sources/measurements together) ordered
    so the caller can group by (source, variable, valid_at) and take the
    freshest issued_at per group in one pass.
    """
    db.insert_forecast_snapshot("ch1", "2026-07-25T09:00:00+00:00", "2026-07-25T12:00:00+00:00", "temperature", 20.0)
    db.insert_forecast_snapshot("ch1", "2026-07-25T11:00:00+00:00", "2026-07-25T12:00:00+00:00", "temperature", 21.0)  # fresher issued_at, same valid_at
    db.insert_forecast_snapshot("ch2", "2026-07-25T06:00:00+00:00", "2026-07-25T12:00:00+00:00", "humidity", 55.0)
    db.insert_forecast_snapshot("ch1", "2026-07-26T09:00:00+00:00", "2026-07-26T12:00:00+00:00", "temperature", 18.0)  # outside window

    rows = db.get_forecast_snapshots_in_window(
        start_valid_at="2026-07-25T00:00:00+00:00", end_valid_at="2026-07-25T23:59:59+00:00"
    )
    assert len(rows) == 3  # the out-of-window row is excluded

    # Group by (source, variable, valid_at), keep first (freshest
    # issued_at, per the DESC ordering) — the same logic the coordinator
    # applies to this result set.
    latest: dict = {}
    for row in rows:
        key = (row["source"], row["variable"], row["valid_at"])
        if key not in latest:
            latest[key] = row
    ch1_temp = latest[("ch1", "temperature", "2026-07-25T12:00:00+00:00")]
    assert ch1_temp["value"] == 21.0  # the fresher issued_at's value won


def test_get_all_bucket_stats_returns_whole_table(db):
    key1 = BucketKey(hour_of_day=12, season="JJA", lead_time_bucket="short", source="ch1", measurement="temperature")
    key2 = BucketKey(hour_of_day=15, season="JJA", lead_time_bucket="medium", source="ch2", measurement="humidity")
    db.upsert_bucket_stats(key1, 0.1, 0.2, 1.0, 5, "2026-07-25T12:00:00+00:00")
    db.upsert_bucket_stats(key2, 0.3, 0.4, 2.0, 8, "2026-07-25T12:00:00+00:00")

    rows = db.get_all_bucket_stats()
    assert len(rows) == 2
    sources = sorted(r["source"] for r in rows)
    assert sources == ["ch1", "ch2"]


# -- v0.1.23 additions: durable runtime state (L-04/05/06/07/08/09) ---------


def test_provider_run_fingerprint_persists_across_get_set(db):
    assert db.get_provider_run_fingerprint("meteoblue") is None
    db.set_provider_run_fingerprint("meteoblue", "abc123")
    assert db.get_provider_run_fingerprint("meteoblue") == "abc123"
    db.set_provider_run_fingerprint("meteoblue", "def456")
    assert db.get_provider_run_fingerprint("meteoblue") == "def456"
    # Independent per source.
    assert db.get_provider_run_fingerprint("srf") is None


def test_annual_call_budget_state_persists_across_get_set(db):
    assert db.get_annual_call_budget_state("meteonomiqs") is None
    db.set_annual_call_budget_state("meteonomiqs", year=2026, calls_used=42)
    state = db.get_annual_call_budget_state("meteonomiqs")
    assert state == {"year": 2026, "calls_used": 42}


def test_bonus_call_tracker_state_persists_across_get_set(db):
    assert db.get_bonus_call_tracker_state("meteoblue") is None
    db.set_bonus_call_tracker_state("meteoblue", {"2026-07-25": 1})
    assert db.get_bonus_call_tracker_state("meteoblue") == {"2026-07-25": 1}


def test_last_scheduled_call_hour_persists_across_get_set(db):
    assert db.get_last_scheduled_call_hour("meteoblue") is None
    db.set_last_scheduled_call_hour("meteoblue", "2026-07-25T12:00:00+00:00")
    assert db.get_last_scheduled_call_hour("meteoblue") == "2026-07-25T12:00:00+00:00"


def test_model_b_previous_probability_persists_across_get_set(db):
    assert db.get_model_b_previous_probability() is None
    db.set_model_b_previous_probability(0.73)
    assert db.get_model_b_previous_probability() == 0.73


# -- v0.1.23 schema migration (L-01/L-02 fix) --------------------------------


def test_fresh_database_has_reconciliation_status_column_and_current_schema(db):
    """A brand-new database should never need migration — the column
    exists from _SCHEMA_SQL directly, and schema_version is written
    immediately.

    v0.1.24: asserts against SCHEMA_VERSION rather than a hard-coded "2",
    so a future bump does not silently leave this test asserting an old
    value while still passing.
    """
    from swissweather_fusion.storage.db import SCHEMA_VERSION

    row = db._conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    assert row["value"] == str(SCHEMA_VERSION)
    cols = {r["name"] for r in db._conn.execute("PRAGMA table_info(forecast_snapshots)")}
    assert "reconciliation_status" in cols


def test_migration_from_v1_reopens_recent_rows_and_archives_old_ones():
    """Simulates a real v0.1.22 database (schema_version=1, no
    reconciliation_status column) being opened by v0.1.23 code. Recent
    rows (within MIGRATION_REOPEN_WINDOW) must come back out as 'pending'
    so they get a correct reconciliation pass; old rows must be marked
    'reconciled' so the migration doesn't try to reprocess years of
    history. bucket_stats must be wiped (see _migrate_to_v2's docstring
    for why that's the correct, audit-aligned choice, not a side effect)."""
    import sqlite3
    import tempfile
    from datetime import datetime, timedelta, timezone

    from swissweather_fusion.storage.db import BucketKey, SwissWeatherDB

    path = tempfile.mktemp(suffix=".db")
    try:
        # Build a raw v1-shaped database by hand (no reconciliation_status
        # column, schema_version=1) — bypassing SwissWeatherDB entirely so
        # this test doesn't depend on the (now-removed) v1 code path.
        raw = sqlite3.connect(path)
        raw.executescript(
            """
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE forecast_snapshots (
                id INTEGER PRIMARY KEY,
                source TEXT NOT NULL,
                issued_at TEXT NOT NULL,
                valid_at TEXT NOT NULL,
                variable TEXT NOT NULL,
                value REAL,
                trigger_reason TEXT DEFAULT 'scheduled'
            );
            CREATE TABLE bucket_stats (
                hour_of_day INTEGER NOT NULL, season TEXT NOT NULL,
                lead_time_bucket TEXT NOT NULL, source TEXT NOT NULL,
                measurement TEXT NOT NULL, ema_bias REAL NOT NULL DEFAULT 0.0,
                ema_abs_error REAL NOT NULL DEFAULT 0.0,
                ema_weight REAL NOT NULL DEFAULT 0.0,
                sample_count INTEGER NOT NULL DEFAULT 0, last_updated TEXT,
                PRIMARY KEY (hour_of_day, season, lead_time_bucket, source, measurement)
            );
            """
        )
        now = datetime.now(timezone.utc)
        old_valid_at = (now - timedelta(days=30)).isoformat()  # older than the 14-day window
        recent_valid_at = (now - timedelta(hours=2)).isoformat()  # within the window
        raw.execute(
            "INSERT INTO forecast_snapshots (source, issued_at, valid_at, variable, value) "
            "VALUES ('ch1', ?, ?, 'temperature', 20.0)",
            (old_valid_at, old_valid_at),
        )
        raw.execute(
            "INSERT INTO forecast_snapshots (source, issued_at, valid_at, variable, value) "
            "VALUES ('ch1', ?, ?, 'temperature', 21.0)",
            (recent_valid_at, recent_valid_at),
        )
        raw.execute(
            "INSERT INTO bucket_stats VALUES (12, 'JJA', 'short', 'ch1', 'temperature', 0.5, 0.5, 1.0, 10, ?)",
            (now.isoformat(),),
        )
        raw.execute("INSERT INTO schema_meta (key, value) VALUES ('schema_version', '1')")
        raw.commit()
        raw.close()

        # Now open it with the real SwissWeatherDB — this must trigger
        # migration. v0.1.24: the target is _migrate_to_v3(), which is a
        # clean rebuild of the derived tables rather than an additive
        # ALTER; the forecast_snapshots re-open behaviour this test
        # actually checks is unchanged.
        db = SwissWeatherDB(path)
        try:
            row = db._conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            from swissweather_fusion.storage.db import SCHEMA_VERSION

            assert row["value"] == str(SCHEMA_VERSION)

            statuses = {
                r["valid_at"]: r["reconciliation_status"]
                for r in db._conn.execute(
                    "SELECT valid_at, reconciliation_status FROM forecast_snapshots"
                )
            }
            assert statuses[old_valid_at] == "reconciled"
            assert statuses[recent_valid_at] == "pending"

            # bucket_stats must be wiped — the old entry (built under the
            # buggy watermark logic) cannot be trusted, per the audit.
            key = BucketKey(hour_of_day=12, season="JJA", lead_time_bucket="short", source="ch1", measurement="temperature")
            assert db.get_bucket_stats(key) is None

            # The recent row is genuinely reachable via the new query.
            pending = db.get_pending_forecast_snapshots(
                until_ts=now.isoformat(), measurements=("temperature",)
            )
            assert len(pending) == 1
            assert pending[0]["valid_at"] == recent_valid_at
        finally:
            db.close()
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_migration_is_idempotent_if_run_twice():
    """Opening an already-migrated (v2) database a second time must not
    re-wipe bucket_stats or re-touch reconciliation_status — the migration
    must only run once, guarded by the stored schema_version."""
    import tempfile

    from swissweather_fusion.storage.db import BucketKey, SwissWeatherDB

    path = tempfile.mktemp(suffix=".db")
    try:
        db = SwissWeatherDB(path)
        db.insert_forecast_snapshot("ch1", "2026-07-25T09:00:00+00:00", "2026-07-25T12:00:00+00:00", "temperature", 22.0)
        key = BucketKey(hour_of_day=12, season="JJA", lead_time_bucket="short", source="ch1", measurement="temperature")
        db.upsert_bucket_stats(key, 0.1, 0.2, 1.0, 3, "2026-07-25T12:00:00+00:00")
        db.close()

        db2 = SwissWeatherDB(path)  # re-open the same (already v2) file
        try:
            assert db2.get_bucket_stats(key) is not None  # NOT wiped again
            rows = db2._conn.execute("SELECT reconciliation_status FROM forecast_snapshots").fetchall()
            assert rows[0]["reconciliation_status"] == "pending"  # untouched
        finally:
            db2.close()
    finally:
        if os.path.exists(path):
            os.remove(path)
