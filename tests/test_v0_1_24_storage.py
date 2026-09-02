"""Regression tests for v0.1.24's storage-layer fixes.

P0-01 (atomic reconciliation), P2-01 (migration detection), P2-02
(corrupt persisted state), P2-08 (storm reconciliation queries) and the
new persisted-state accessors (P1-08, IND-07).
"""
import json
import sqlite3
import tempfile

import pytest

from swissweather_fusion.storage.db import (
    SCHEMA_VERSION,
    BucketKey,
    SwissWeatherDB,
)


@pytest.fixture
def db():
    path = tempfile.mktemp(suffix=".db")
    database = SwissWeatherDB(path)
    yield database
    database.close()


def _key(source="ch1", measurement="temperature", hour=12):
    return BucketKey(
        hour_of_day=hour,
        season="summer",
        lead_time_bucket="short",
        source=source,
        measurement=measurement,
    )


# ---------------------------------------------------------------------------
# P0-01 — reconciliation atomicity
# ---------------------------------------------------------------------------
def test_apply_reconciliation_batch_writes_bucket_stats_and_status_together(db):
    db.insert_forecast_snapshot("ch1", "i", "2026-07-25T12:00:00+00:00", "temperature", 20.0)
    row_id = db._conn.execute("SELECT id FROM forecast_snapshots").fetchone()["id"]

    db.apply_reconciliation_batch(
        [(_key(), 0.5, 0.5, 2.0, 1, "2026-07-25T13:00:00+00:00")],
        [row_id],
        [],
    )

    stats = db.get_bucket_stats(_key())
    assert stats is not None and stats.sample_count == 1
    status = db._conn.execute(
        "SELECT reconciliation_status FROM forecast_snapshots WHERE id = ?", (row_id,)
    ).fetchone()["reconciliation_status"]
    assert status == "reconciled"


def test_apply_reconciliation_batch_is_all_or_nothing(db):
    """The original defect, reproduced at the boundary that caused it.

    upsert_bucket_stats() used to commit per row inside the loop while
    the status transitions ran once at the end. A crash in between left
    bucket_stats already updated for rows still marked 'pending' — so the
    next cycle re-selected them and folded them into the EMA a SECOND
    time. An EMA cannot un-absorb a duplicated sample, so there is no
    recovery after the fact.

    Fault is injected via a deliberately malformed bucket update, which
    fails partway through the transaction after an earlier update has
    already been executed.
    """
    db.insert_forecast_snapshot("ch1", "i", "2026-07-25T12:00:00+00:00", "temperature", 20.0)
    row_id = db._conn.execute("SELECT id FROM forecast_snapshots").fetchone()["id"]

    good = (_key(), 0.5, 0.5, 2.0, 1, "2026-07-25T13:00:00+00:00")
    malformed = (_key(source="ch2"), 0.5, 0.5, 2.0, 1, object())

    with pytest.raises(Exception):
        db.apply_reconciliation_batch([good, malformed], [row_id], [])

    # Neither half may have landed.
    assert db.get_bucket_stats(_key()) is None, (
        "a bucket_stats write survived a failed batch — this is exactly the "
        "state that causes double-counted learning on the next cycle"
    )
    status = db._conn.execute(
        "SELECT reconciliation_status FROM forecast_snapshots WHERE id = ?", (row_id,)
    ).fetchone()["reconciliation_status"]
    assert status == "pending"


def test_transaction_is_rolled_back_within_the_same_process(db):
    """SQLite only auto-rolls-back an open transaction on the NEXT process
    start. An exception caught inside the same still-running process
    leaves it open indefinitely, holding locks and letting subsequent
    writes join a transaction that was supposed to be abandoned. The
    explicit rollback is what makes this atomic in the case that matters.

    Verified by showing the connection is immediately usable afterwards.
    """
    with pytest.raises(Exception):
        db.apply_reconciliation_batch(
            [(_key(), 0.5, 0.5, 2.0, 1, object())], [], []
        )
    db.insert_forecast_snapshot("ch1", "i", "v", "temperature", 1.0)
    assert db._conn.execute("SELECT COUNT(*) AS n FROM forecast_snapshots").fetchone()["n"] == 1


def test_apply_reconciliation_batch_empty_is_a_noop(db):
    db.apply_reconciliation_batch([], [], [])
    assert db.get_bucket_stats(_key()) is None


def test_same_batch_same_bucket_collapses_to_one_final_write(db):
    """A naive "defer every write, commit once" implementation silently
    drops one of two same-bucket rows within a batch. Since a bucket is
    (hour, season, lead_time, source, measurement) and a batch routinely
    contains many hours of one source's forecast, this is common rather
    than a corner case. The coordinator builds on in-flight state; the
    storage layer must accept the collapsed result."""
    key = _key()
    db.apply_reconciliation_batch(
        [(key, 1.0, 1.0, 1.0, 2, "2026-07-25T13:00:00+00:00")], [], []
    )
    stats = db.get_bucket_stats(key)
    assert stats.sample_count == 2


# ---------------------------------------------------------------------------
# P2-01 — migration detection from table shape
# ---------------------------------------------------------------------------
def test_migration_triggers_from_table_shape_even_without_schema_version_row():
    """The genuinely ambiguous case the old logic got wrong.

    _ensure_schema used to trust the ABSENCE of a schema_version row, on
    its own, to mean "brand new database". That is not the same claim as
    "the tables are in their current shape": a database whose schema_meta
    row was lost while the data tables survived has no version row
    either, and every CREATE TABLE IF NOT EXISTS is a silent no-op
    against a table that already exists regardless of its columns. The
    old code then created the partial index against columns that did not
    exist — failing on exactly the recovery path it was written for.
    """
    path = tempfile.mktemp(suffix=".db")
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
    # Note: NO schema_version row at all.
    raw.commit()
    raw.close()

    database = SwissWeatherDB(path)
    try:
        cols = {
            r["name"]
            for r in database._conn.execute("PRAGMA table_info(forecast_snapshots)")
        }
        assert "reconciliation_status" in cols

        # And the version row must actually have been CREATED. The old
        # write was a bare UPDATE ... WHERE key='schema_version', which
        # matches zero rows — and therefore silently writes nothing —
        # in precisely this scenario.
        row = database._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        assert row is not None and row["value"] == str(SCHEMA_VERSION)
    finally:
        database.close()


def test_fresh_database_is_not_treated_as_needing_migration(db):
    cols = {r["name"] for r in db._conn.execute("PRAGMA table_info(radar_observations)")}
    assert "precip_accum_mm_1h" in cols
    assert "quality" in cols
    predictions = {
        r["name"] for r in db._conn.execute("PRAGMA table_info(storm_predictions)")
    }
    assert "reconciled" in predictions


# ---------------------------------------------------------------------------
# P2-02 — corrupt persisted state must not prevent startup
# ---------------------------------------------------------------------------
def test_corrupted_annual_call_budget_state_returns_none_not_crash(db):
    """The original defect: json.loads() ran directly against whatever
    text was in schema_meta. A truncated write took the owning
    coordinator down at startup — permanently, on every restart."""
    db._set_meta("annual_call_budget:meteonomiqs", '{"year": 2026, "calls_')
    assert db.get_annual_call_budget_state("meteonomiqs") is None


def test_corrupted_state_is_cleared_so_it_does_not_recur(db):
    """The part that matters most: without clearing, the same corrupt
    byte fails again on every subsequent restart forever."""
    db._set_meta("annual_call_budget:meteonomiqs", "not json at all")
    db.get_annual_call_budget_state("meteonomiqs")
    assert db._get_meta("annual_call_budget:meteonomiqs") is None


def test_corrupted_model_b_probability_returns_none_not_crash(db):
    db._set_meta("model_b_previous_probability", "not-a-float")
    assert db.get_model_b_previous_probability() is None


def test_corrupted_bonus_call_tracker_state_returns_none_not_crash(db):
    db._set_meta("bonus_call_tracker:meteoblue", "{{{")
    assert db.get_bonus_call_tracker_state("meteoblue") is None


def test_valid_persisted_state_still_parses_normally(db):
    db.set_annual_call_budget_state("meteonomiqs", 2026, 5)
    assert db.get_annual_call_budget_state("meteonomiqs") == {
        "year": 2026, "calls_used": 5,
    }
    db.set_model_b_previous_probability(0.42)
    assert db.get_model_b_previous_probability() == pytest.approx(0.42)


# ---------------------------------------------------------------------------
# P1-08 / IND-07 — new persisted state
# ---------------------------------------------------------------------------
def test_meteonomiqs_last_successful_call_date_roundtrips(db):
    assert db.get_meteonomiqs_last_successful_call_date() is None
    db.set_meteonomiqs_last_successful_call_date("2026-07-25")
    assert db.get_meteonomiqs_last_successful_call_date() == "2026-07-25"


def test_srf_geolocation_id_is_keyed_by_location(db):
    """Keyed by rounded coordinates so relocating the installation
    naturally invalidates the cache rather than silently reusing the old
    location's ID."""
    db.set_srf_geolocation_id("46.9481_7.4474", "geo-123")
    assert db.get_srf_geolocation_id("46.9481_7.4474") == "geo-123"
    assert db.get_srf_geolocation_id("47.3769_8.5417") is None


# ---------------------------------------------------------------------------
# P2-08 — storm prediction reconciliation queries
# ---------------------------------------------------------------------------
def test_unreconciled_predictions_respect_time_and_probability_floors(db):
    db.insert_storm_prediction("2026-07-25T10:00:00+00:00", 0.9, {})   # old, confident
    db.insert_storm_prediction("2026-07-25T10:00:00+00:00", 0.1, {})   # old, unconfident
    db.insert_storm_prediction("2026-07-25T23:00:00+00:00", 0.9, {})   # too recent

    rows = db.get_unreconciled_storm_predictions("2026-07-25T12:00:00+00:00", 0.5)

    assert len(rows) == 1
    assert rows[0]["probability"] == 0.9
    assert rows[0]["ts"] == "2026-07-25T10:00:00+00:00"


def test_marking_reconciled_prevents_reselection(db):
    db.insert_storm_prediction("2026-07-25T10:00:00+00:00", 0.9, {})
    rows = db.get_unreconciled_storm_predictions("2026-07-25T12:00:00+00:00", 0.5)
    db.mark_storm_predictions_reconciled([rows[0]["id"]])
    assert db.get_unreconciled_storm_predictions("2026-07-25T12:00:00+00:00", 0.5) == []


def test_mark_reconciled_with_empty_list_is_a_noop(db):
    db.mark_storm_predictions_reconciled([])


def test_radar_observations_between_bounds_both_edges(db):
    db.insert_radar_observation("2026-07-25T09:00:00+00:00", 1.0, None, 9)
    db.insert_radar_observation("2026-07-25T10:00:00+00:00", 2.0, None, 9)
    db.insert_radar_observation("2026-07-25T11:00:00+00:00", 3.0, None, 9)

    rows = db.get_radar_observations_between(
        "2026-07-25T09:30:00+00:00", "2026-07-25T10:30:00+00:00"
    )
    assert [r["precip_accum_mm_1h"] for r in rows] == [2.0]


# ---------------------------------------------------------------------------
# IND-06 — storage telemetry
# ---------------------------------------------------------------------------
def test_storage_stats_reports_row_counts_and_file_size(db):
    db.insert_forecast_snapshot("ch1", "i", "v", "temperature", 1.0)
    stats = db.get_storage_stats()
    assert stats["forecast_snapshots_rows"] == 1
    assert stats["file_size_bytes"] is not None and stats["file_size_bytes"] > 0


# ---------------------------------------------------------------------------
# IND-10 — dead code removed
# ---------------------------------------------------------------------------
def test_dead_watermark_accessors_are_gone(db):
    """These implemented the pre-v0.1.23 reconciliation design that
    reconciliation_status replaced. Zero production callers afterwards,
    and their presence implied two competing mechanisms coexisted.
    Asserted as absence so reinstating them is deliberate."""
    assert not hasattr(db, "get_reconciliation_watermark")
    assert not hasattr(db, "set_reconciliation_watermark")
