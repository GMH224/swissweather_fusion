"""SQLite storage layer for SwissWeather Fusion.

Deliberately a separate file from Home Assistant's own recorder database
(plan doc §5) — this integration owns its schema and its own file, so it
never depends on or interferes with HA's recorder migrations.

All timestamps are stored as UTC ISO-8601 strings (plan doc, timestamp
discussion). This module is pure Python + sqlite3 with no Home Assistant
imports, specifically so it can be unit-tested without a running HA
instance — see tests/test_db.py.

Every public method here is blocking (sqlite3 is a blocking library). The
caller (coordinator.py) is responsible for wrapping calls in
hass.async_add_executor_job() — this module does not do that itself, to
keep it framework-independent and directly testable.

**v0.1.12 fix — the likely root cause of a total, cross-coordinator
freeze found via diagnostics**: a real deployment showed CombiPrecip
(5-min interval), SRF (45-min), Open-Meteo (15-min), and the Model A
learning coordinator (20-min) all succeed once in a tight ~3-second burst
at startup, then go completely silent — every one of them, simultaneously
— for 5+ hours. That pattern (a shared resource works fine once, then
every consumer of it hangs together) pointed at the one thing every
coordinator actually shares: this single SQLite connection, accessed
concurrently from multiple executor-pool threads. `check_same_thread=False`
only disables Python's own same-thread safety check — it does not make
concurrent, simultaneous use of the same Connection object from different
threads safe, and `busy_timeout` governs SQLite-level lock contention
between separate connections, not Python-level thread-safety of a single
shared connection object. A `threading.Lock` now serializes every access
to `self._conn`, so concurrent executor jobs queue safely instead of
racing on the same connection.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

_LOGGER = logging.getLogger(__name__)

# v0.1.23 fix (L-01/L-02, audit finding): schema version bumped 1 -> 2 for
# the reconciliation_status column below. See _migrate_to_v2() for what
# actually happens to existing data on upgrade — this number alone is not
# the migration, just the trigger for it.
# v0.1.24: bumped to 3. See _migrate_to_v3 for what changes and why this
# one is a clean rebuild rather than an additive migration.
SCHEMA_VERSION = 3

# v0.1.23: how far back the v1->v2 migration re-opens forecast_snapshots
# rows for a fresh, correct reconciliation pass. Deliberately reuses the
# exact same value as ModelALearningCoordinator.INITIAL_LOOKBACK (the
# project's own established "how far back is worth reconciling from cold"
# window) rather than inventing a second number that could drift out of
# sync with it.
MIGRATION_REOPEN_WINDOW = timedelta(days=14)

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS station_observations (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    temperature REAL,
    humidity REAL,
    pressure REAL
);

CREATE TABLE IF NOT EXISTS forecast_snapshots (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    valid_at TEXT NOT NULL,
    variable TEXT NOT NULL,
    value REAL,
    trigger_reason TEXT DEFAULT 'scheduled',
    -- v0.1.23 fix (L-01/L-02): durable per-row reconciliation identity,
    -- replacing the old single global watermark. A row is 'pending' until
    -- Model A learning either folds it into bucket_stats ('reconciled')
    -- or gives up on it after RETRY_GIVE_UP_AGE ('skipped'). Because this
    -- is a per-row fact, not a single global cursor position, a row can
    -- never be double-counted (L-01) and a late-arriving row is never
    -- invisible just because it landed after some other row's valid_at
    -- (L-02) — see ModelALearningCoordinator._reconcile().
    reconciliation_status TEXT NOT NULL DEFAULT 'pending'
);

-- v0.1.24 (P1-14): column renamed from precip_rate_mmh. CombiPrecip
-- reports a ONE-HOUR ACCUMULATION in mm (MeteoSwiss product CPC,
-- "Combiprecip 60-minute total"), not an instantaneous rate — that is
-- RZC/PRECIP, a different product in the same collection. quality is
-- MeteoSwiss's own radar quality code (0-9) for the source file.
CREATE TABLE IF NOT EXISTS radar_observations (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    precip_accum_mm_1h REAL,
    precip_type TEXT,
    quality INTEGER
);

CREATE TABLE IF NOT EXISTS bucket_stats (
    hour_of_day INTEGER NOT NULL,
    season TEXT NOT NULL,
    lead_time_bucket TEXT NOT NULL,
    source TEXT NOT NULL,
    measurement TEXT NOT NULL,
    ema_bias REAL NOT NULL DEFAULT 0.0,
    ema_abs_error REAL NOT NULL DEFAULT 0.0,
    ema_weight REAL NOT NULL DEFAULT 0.0,
    sample_count INTEGER NOT NULL DEFAULT 0,
    last_updated TEXT,
    PRIMARY KEY (hour_of_day, season, lead_time_bucket, source, measurement)
);

CREATE TABLE IF NOT EXISTS storm_events (
    id INTEGER PRIMARY KEY,
    start_ts TEXT NOT NULL,
    end_ts TEXT,
    peak_pressure_drop REAL,
    peak_temp_drop REAL,
    peak_precip_rate REAL,
    notes TEXT
);

-- v0.1.24 (P2-08): `reconciled` lets StormEventReconciliationCoordinator
-- check each prediction's outcome exactly once, after its follow-up
-- window has fully elapsed, without ever re-checking it.
CREATE TABLE IF NOT EXISTS storm_predictions (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    probability REAL NOT NULL,
    features TEXT,
    reconciled INTEGER NOT NULL DEFAULT 0
);
"""

# ---------------------------------------------------------------------------
# Indexes — ALL of them, applied only after any migration has run
# ---------------------------------------------------------------------------
# **Every index in this schema lives here, and none live in _TABLE_SQL.**
#
# This is a structural guarantee, not a convention to remember. The reason:
# _TABLE_SQL runs first and unconditionally, and its
# `CREATE TABLE IF NOT EXISTS` statements are silent no-ops against tables
# that already exist — regardless of whether those tables have the columns
# the current schema expects. An index defined alongside those CREATE
# TABLE statements therefore executes against the OLD table shape on every
# upgrading installation, and raises "no such column" before any migration
# can repair it. Setup then fails outright and the integration cannot
# load at all.
#
# History, because this has now bitten twice:
#
#   v0.1.23 hit it with idx_forecast_pending (reconciliation_status) and
#   split that ONE index out, leaving a comment explaining the hazard.
#
#   v0.1.24 hit it again with idx_predictions_reconciled, which was added
#   into the table script a few lines above that very comment, and took
#   down setup on every upgrading installation with
#   `sqlite3.OperationalError: no such column: reconciled`.
#
# A rule that has to be remembered at the moment of writing a new index is
# evidently not enough. So v0.1.25 moved EVERY index here, including the
# ones that were always safe. Now there is no judgement call to get wrong:
# indexes go here, full stop, and tests/test_v0_1_24_storage.py asserts
# that _TABLE_SQL contains no CREATE INDEX statement at all.
_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_station_ts ON station_observations(ts);
CREATE INDEX IF NOT EXISTS idx_forecast_source_ts
    ON forecast_snapshots(source, valid_at);
CREATE INDEX IF NOT EXISTS idx_radar_ts ON radar_observations(ts);
CREATE INDEX IF NOT EXISTS idx_predictions_ts ON storm_predictions(ts);
CREATE INDEX IF NOT EXISTS idx_forecast_pending
    ON forecast_snapshots(reconciliation_status, valid_at)
    WHERE reconciliation_status = 'pending';
CREATE INDEX IF NOT EXISTS idx_predictions_reconciled
    ON storm_predictions(reconciled, ts);
"""


@dataclass(frozen=True)
class BucketKey:
    """Identifies one (hour, season, lead_time, source, measurement) bucket.

    See DEVELOPER.md ("The lead-time bucket bug") for why every one of these
    fields is necessary and what happens if one is dropped.
    """

    hour_of_day: int
    season: str
    lead_time_bucket: str
    source: str
    measurement: str


@dataclass(frozen=True)
class BucketStats:
    ema_bias: float
    ema_abs_error: float
    ema_weight: float
    sample_count: int
    last_updated: Optional[str]


class SwissWeatherDB:
    """Thin, synchronous wrapper around the SQLite file.

    Every method here is a plain blocking call. Callers on the HA event loop
    must use hass.async_add_executor_job(). Tests call these methods
    directly since no event loop is involved.

    **All access to self._conn is serialized via self._lock** (v0.1.12) —
    see the module docstring for why. Every public method acquires the
    lock for its entire body; nothing here holds the lock across an
    executor-job boundary or an await (this class has no async methods at
    all), so there's no risk of the lock itself becoming a new source of
    a hang — just a brief, bounded wait if two callers overlap.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        # v0.1.19 fix: sqlite3.connect() does not create missing parent
        # directories, and this integration's real call site
        # (__init__.py) points at HA's `.storage/` directory, which
        # reliably exists in a normal HA install because core creates it
        # very early during startup — but that's an environmental
        # assumption, not something this class enforced itself. Found via
        # a real Home Assistant test-instance setup run (not by static
        # review): a fresh hass.config.config_dir without a pre-existing
        # `.storage/` directory raised an unhandled
        # `sqlite3.OperationalError: unable to open database file`,
        # skipping the graceful-degradation path __init__.py otherwise
        # goes out of its way to provide for every other failure mode.
        # Creating the directory here (idempotent, exist_ok=True) removes
        # the dependency on that external assumption entirely.
        parent_dir = os.path.dirname(self._db_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._configure_pragmas()
        self._ensure_schema()

    def _configure_pragmas(self) -> None:
        # WAL + NORMAL sync: kinder to SD-card/VM-disk storage under frequent
        # small writes than the default rollback-journal mode. busy_timeout
        # means concurrent access waits briefly instead of raising
        # "database is locked" immediately.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")

    def _ensure_schema(self) -> None:
        """Bring the database to the current schema.

        Strict ordering, and the order is the safety property:

            1. CREATE TABLE statements only (_TABLE_SQL)
            2. inspect the ACTUAL table shape
            3. migrate if the shape is not current
            4. only then, create indexes (_INDEX_SQL)

        Step 4 cannot run before step 3, which is what makes it impossible
        for an index to reference a column a migration has not added yet.
        See the comment above _INDEX_SQL for the two releases that learned
        this the hard way.
        """
        self._conn.executescript(_TABLE_SQL)
        cur = self._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        )
        row = cur.fetchone()
        has_version_row = row is not None

        # v0.1.24 fix (P2-01): migration detection is now based on the
        # ACTUAL SHAPE OF THE TABLES, with the metadata row used only as a
        # secondary signal.
        #
        # The old logic trusted the absence of a schema_version row, on
        # its own, to mean "this is a genuinely fresh database". That is
        # not the same claim as "the tables are actually in their current
        # shape". A database whose schema_meta row was lost or never
        # written, while the data tables survived, has no schema_version
        # row either — and every CREATE TABLE IF NOT EXISTS above is a
        # silent no-op against a table that already exists, regardless of
        # its column shape. The old code would then treat that database as
        # brand new and immediately create the partial index against
        # columns that do not exist, failing on exactly the recovery path
        # the branch was written to handle.
        actual = self._table_shape()
        looks_current = (
            "reconciliation_status" in actual.get("forecast_snapshots", set())
            and "reconciled" in actual.get("storm_predictions", set())
            and "precip_accum_mm_1h" in actual.get("radar_observations", set())
        )
        # Metadata absence is used only as a SECONDARY signal: a database
        # with no data tables and no version row is genuinely new. It can
        # no longer, on its own, cause a populated database to be treated
        # as fresh.
        is_fresh = not actual.get("forecast_snapshots") and not has_version_row

        if is_fresh or looks_current:
            # Either genuinely new (the CREATE TABLE statements above just
            # built everything at the current shape) or already current.
            self._conn.executescript(_INDEX_SQL)
            self._write_schema_version()
            self._conn.commit()
            return

        # Tables exist but are not at the current shape. Migrate on the
        # evidence of the shape itself, not on what the metadata claims.
        self._migrate_to_v3()
        self._conn.executescript(_INDEX_SQL)
        self._write_schema_version()
        self._conn.commit()

    def _table_shape(self) -> dict[str, set[str]]:
        """Actual column names per table, straight from the database.

        The evidence _ensure_schema migrates on (P2-01). A table that does
        not exist is simply absent from the result.
        """
        shape: dict[str, set[str]] = {}
        for table in ("forecast_snapshots", "storm_predictions", "radar_observations"):
            cols = {
                r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")
            }
            if cols:
                shape[table] = cols
        return shape

    def _write_schema_version(self) -> None:
        """Record the current schema version.

        v0.1.24: this was a bare

            UPDATE schema_meta SET value = ? WHERE key = 'schema_version'

        which matches zero rows, and therefore silently writes nothing,
        whenever the row does not already exist — which is precisely the
        ambiguous case the P2-01 fix above exists to handle. A proper
        upsert cannot have that failure mode.
        """
        self._conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )

    def _migrate_to_v3(self) -> None:
        """v0.1.24: rebuild the learning and event tables from scratch.

        **Why a clean rebuild rather than an additive ALTER.** Three of
        this release's fixes change what stored data MEANS, not merely
        what shape it has:

        1. IND-01 changes how Model A's learned weights relate to each
           other. Every ema_weight in bucket_stats was produced on a
           unit-dependent scale that the new blend does not use.
        2. P1-14 changes what the radar value IS — millimetres
           accumulated over an hour, not an instantaneous mm/h rate. Rows
           written under the old interpretation are not convertible,
           because the two quantities genuinely differ.
        3. P0-01 and P0-02 both mean some historical reconciliation
           results and storm predictions were produced by logic now known
           to be wrong (double-counted EMA samples; spurious repeated
           crossings).

        Carrying any of that forward would silently poison the corrected
        models with data generated by the uncorrected ones. There is no
        transformation that recovers the intended meaning, so the honest
        move is to discard and relearn — and the learning loop rebuilds
        bucket_stats automatically from forecast_snapshots, which IS
        preserved, so the cost is a warm-up period rather than lost
        history.

        forecast_snapshots and station_observations are preserved and
        simply re-opened for reconciliation, since raw provider forecasts
        and raw sensor readings are facts, not derived interpretations.
        """
        _LOGGER.warning(
            "Migrating SwissWeather Fusion database to schema v%s: learned "
            "bucket_stats, radar observations and storm predictions are being "
            "rebuilt from scratch (v0.1.24 changed what those values mean — "
            "see DEVELOPER.md). Raw forecasts and station observations are "
            "preserved and will be re-reconciled.",
            SCHEMA_VERSION,
        )

        cols = self._table_shape()

        if "reconciliation_status" not in cols.get("forecast_snapshots", set()):
            self._conn.execute(
                "ALTER TABLE forecast_snapshots "
                "ADD COLUMN reconciliation_status TEXT NOT NULL DEFAULT 'pending'"
            )

        # Re-open recent rows for a fresh, correct reconciliation pass;
        # leave older ones settled rather than replaying years of history.
        cutoff = (datetime.now(timezone.utc) - MIGRATION_REOPEN_WINDOW).isoformat()
        self._conn.execute(
            "UPDATE forecast_snapshots SET reconciliation_status = 'reconciled' "
            "WHERE valid_at < ?",
            (cutoff,),
        )
        self._conn.execute(
            "UPDATE forecast_snapshots SET reconciliation_status = 'pending' "
            "WHERE valid_at >= ?",
            (cutoff,),
        )

        # Derived tables: drop and let _TABLE_SQL's CREATE statements
        # rebuild them at the current shape on the next open. Dropping is
        # what makes the CREATE IF NOT EXISTS statements meaningful again.
        self._conn.execute("DELETE FROM bucket_stats")
        self._conn.execute("DROP TABLE IF EXISTS radar_observations")
        self._conn.execute("DROP TABLE IF EXISTS storm_predictions")
        self._conn.execute("DROP TABLE IF EXISTS storm_events")
        # Recreate the dropped tables at the current shape. Tables only —
        # indexes are applied by _ensure_schema after this returns.
        self._conn.executescript(_TABLE_SQL)
        self._conn.commit()

    def _migrate_to_v2(self) -> None:
        """v0.1.23 fix (L-01/L-02): introduces reconciliation_status.

        This is a real data migration, not just a schema change, because
        the old watermark-based reconciliation design (audit findings
        L-01/L-02) could both double-count and permanently skip rows —
        the external audit's own conclusion was that persisted bucket_stats
        weights are NOT trustworthy under the old logic. Rather than carry
        that uncertainty forward silently, this migration:

        1. Adds the reconciliation_status column (existing rows default to
           'pending' per the column's own DEFAULT, so no separate UPDATE is
           needed for the column value itself).
        2. Marks rows older than MIGRATION_REOPEN_WINDOW as 'reconciled' —
           re-processing years of history under the new logic on the first
           post-upgrade run would be wasteful and is not needed for
           correctness (the fix is forward-looking; nothing about
           correctly-shaped old rows is unsafe to leave alone).
        3. Leaves recent rows (within the window) as 'pending' so they get
           a fresh, correct reconciliation pass under the new code.
        4. Wipes bucket_stats entirely. Every remaining bias/weight in that
           table may have been built from double-counted samples under
           L-01 — there is no way to retroactively separate genuine
           samples from duplicated ones after the fact, so starting clean
           is the only way to make the "PASS" persistence layer trustworthy
           again. Buckets rebuild themselves automatically as the 'pending'
           rows above (and all new data going forward) get reconciled.
        """
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(forecast_snapshots)")}
        if "reconciliation_status" not in cols:
            self._conn.execute(
                "ALTER TABLE forecast_snapshots "
                "ADD COLUMN reconciliation_status TEXT NOT NULL DEFAULT 'pending'"
            )

        cutoff = (datetime.now(timezone.utc) - MIGRATION_REOPEN_WINDOW).isoformat()
        self._conn.execute(
            "UPDATE forecast_snapshots SET reconciliation_status = 'reconciled' "
            "WHERE valid_at < ?",
            (cutoff,),
        )

        bucket_rows_cleared = self._conn.execute("SELECT COUNT(*) AS n FROM bucket_stats").fetchone()["n"]
        self._conn.execute("DELETE FROM bucket_stats")

        # The old global watermark is superseded by per-row status and is
        # no longer read by the reconciliation loop, but it isn't deleted
        # here — it's harmless leftover state and removing schema_meta
        # rows during a migration adds risk for no benefit.
        self._conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('v2_migration_bucket_stats_cleared', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"{bucket_rows_cleared} rows cleared at {datetime.now(timezone.utc).isoformat()}",),
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- station observations -------------------------------------------------

    def insert_station_observation(
        self,
        ts: str,
        temperature: Optional[float],
        humidity: Optional[float],
        pressure: Optional[float],
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO station_observations (ts, temperature, humidity, pressure) "
                "VALUES (?, ?, ?, ?)",
                (ts, temperature, humidity, pressure),
            )
            self._conn.commit()

    def get_station_observations_since(self, since_ts: str) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM station_observations WHERE ts >= ? ORDER BY ts ASC",
                (since_ts,),
            )
            return cur.fetchall()

    def get_station_observations_between(
        self, start_ts: str, end_ts: str
    ) -> list[sqlite3.Row]:
        """Used by the Model A learning reconciliation step (v0.1.7) to
        fetch candidate ground-truth readings around a batch of forecast
        valid_at times in one query, rather than one query per forecast
        row.
        """
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM station_observations WHERE ts >= ? AND ts <= ? ORDER BY ts ASC",
                (start_ts, end_ts),
            )
            return cur.fetchall()

    def get_latest_station_observation(self) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM station_observations ORDER BY ts DESC LIMIT 1"
            )
            return cur.fetchone()

    # -- reconciliation watermark (Model A learning, v0.1.7) ---------------------
    # Reuses schema_meta rather than a new table — this is a single small
    # value (the last valid_at up to which forecast_snapshots have already
    # been compared against actual observations and folded into
    # bucket_stats), not worth a dedicated table for.

    # v0.1.24 cleanup (IND-10): get_reconciliation_watermark() and
    # set_reconciliation_watermark() were removed here. They implemented
    # the pre-v0.1.23 reconciliation design, which the
    # reconciliation_status column replaced wholesale; both had zero
    # production callers after that change and were dead code that
    # actively misled readers into thinking two competing reconciliation
    # mechanisms coexisted. The stored schema_meta key is left in place
    # rather than deleted — it is inert, and removing it would be a data
    # migration for no benefit.

    # -- forecast snapshots -----------------------------------------------------

    def insert_forecast_snapshot(
        self,
        source: str,
        issued_at: str,
        valid_at: str,
        variable: str,
        value: Optional[float],
        trigger_reason: str = "scheduled",
    ) -> None:
        """Single-row insert. Production coordinators always use the bulk
        variant below (one transaction per poll cycle) — this single-row
        form is kept as a test-setup convenience (see tests/test_db.py and
        tests/test_learning_integration.py, which exercise it directly)
        rather than removed as dead production code."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO forecast_snapshots "
                "(source, issued_at, valid_at, variable, value, trigger_reason) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (source, issued_at, valid_at, variable, value, trigger_reason),
            )
            self._conn.commit()

    def insert_forecast_snapshots_bulk(
        self, rows: Iterable[tuple[str, str, str, str, Optional[float], str]]
    ) -> None:
        """Bulk insert to keep one poll cycle to one transaction."""
        with self._lock:
            self._conn.executemany(
                "INSERT INTO forecast_snapshots "
                "(source, issued_at, valid_at, variable, value, trigger_reason) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()

    def get_forecast_values_for_valid_at(
        self, source: str, variable: str, valid_at: str
    ) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM forecast_snapshots "
                "WHERE source = ? AND variable = ? AND valid_at = ? "
                "ORDER BY issued_at DESC",
                (source, variable, valid_at),
            )
            return cur.fetchall()

    def get_pending_forecast_snapshots(
        self, *, until_ts: str, measurements: tuple[str, ...]
    ) -> list[sqlite3.Row]:
        """v0.1.23 fix (L-01/L-02): rows whose valid_at has now passed (i.e.
        we should have a real station observation to compare against) and
        whose reconciliation_status is still 'pending' — used by the
        Model A learning reconciliation step. Restricted to measurements
        the local station can actually confirm (temperature/humidity/
        pressure) — precip/wind_speed have no ground truth yet since the
        station doesn't have rain/wind sensors.

        Replaces the old get_forecast_snapshots_to_reconcile(), which took
        a `since_ts` watermark as its lower bound. That watermark was a
        *global* cursor standing in for "has this been learned yet?" — the
        external ICS audit (L-01) found that capping it to protect a
        still-retryable row made every already-reconciled row after that
        point eligible for re-selection (and thus re-learned) on the very
        next cycle, and (L-02) that a forecast landing in the database
        after the watermark had already passed its valid_at could never be
        selected at all. Filtering on a per-row status instead of a global
        position makes both classes of bug structurally impossible: a row
        already marked 'reconciled' or 'skipped' can never be selected
        again regardless of when it was inserted or where the cursor sits,
        and a 'pending' row is selected exactly once it's actually old
        enough to have a station reading, regardless of insertion order.

        Every matching row is returned individually, not deduplicated by
        (source, valid_at) — a source can have several snapshots for the
        same valid_at from different issued_at times (different lead
        times), and each is a genuinely separate, separately-informative
        data point for its own lead_time_bucket.
        """
        placeholders = ",".join("?" for _ in measurements)
        with self._lock:
            cur = self._conn.execute(
                f"SELECT * FROM forecast_snapshots "
                f"WHERE reconciliation_status = 'pending' AND valid_at <= ? "
                f"AND variable IN ({placeholders}) "
                f"ORDER BY valid_at ASC",
                (until_ts, *measurements),
            )
            return cur.fetchall()

    def mark_forecast_snapshots_status(self, ids: Iterable[int], status: str) -> None:
        """v0.1.23 fix (L-01/L-02): bulk-transitions rows out of 'pending'.

        status must be 'reconciled' (successfully folded into bucket_stats)
        or 'skipped' (gave up retrying — see RETRY_GIVE_UP_AGE). Either way
        this is a one-way, one-time transition: once a row leaves 'pending'
        it is permanently excluded from get_pending_forecast_snapshots(),
        which is exactly the guarantee that prevents re-learning (L-01).

        Uses executemany for one bulk transaction per reconciliation cycle
        rather than one UPDATE per row, matching the project's existing
        bulk-operation convention (v0.1.13) for the same performance reason.
        """
        assert status in ("reconciled", "skipped"), f"invalid status: {status!r}"
        ids = list(ids)
        if not ids:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE forecast_snapshots SET reconciliation_status = ? WHERE id = ?",
                [(status, i) for i in ids],
            )
            self._conn.commit()

    def get_forecast_snapshots_in_window(
        self, *, start_valid_at: str, end_valid_at: str
    ) -> list[sqlite3.Row]:
        """**v0.1.13**: one bulk query replacing what used to be up to
        thousands of individual `get_forecast_values_for_valid_at` calls
        per blend cycle (168 hours × 5 measurements × up to 5 sources,
        each needing its own round trip). Returns every row in the
        window across all sources/measurements; the caller groups by
        (source, variable, valid_at) and keeps the freshest issued_at per
        group in memory, replicating what the old per-call
        `ORDER BY issued_at DESC LIMIT via [0]` pattern did — just once
        for the whole batch instead of once per (hour, measurement,
        source) combination.
        """
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM forecast_snapshots WHERE valid_at >= ? AND valid_at <= ? "
                "ORDER BY source, variable, valid_at, issued_at DESC",
                (start_valid_at, end_valid_at),
            )
            return cur.fetchall()

    def get_all_bucket_stats(self) -> list[sqlite3.Row]:
        """**v0.1.13**: bucket_stats is documented to stay small
        permanently by design (a fixed number of hour/season/lead-time/
        source/measurement combinations, not one row per observation) —
        fetching the whole table in one query and indexing it in memory
        is cheap, and replaces what used to be a separate
        `get_bucket_stats` round trip for every single hour/measurement/
        source combination in a blend cycle.
        """
        with self._lock:
            cur = self._conn.execute("SELECT * FROM bucket_stats")
            return cur.fetchall()

    # -- radar observations (CombiPrecip) ----------------------------------------

    def insert_radar_observation(
        self,
        ts: str,
        precip_accum_mm_1h: Optional[float],
        precip_type: Optional[str],
        quality: Optional[int] = None,
    ) -> None:
        """v0.1.24 (P1-14): parameter renamed alongside the column. The
        value is millimetres accumulated over the preceding hour, which is
        what MeteoSwiss's CPC product actually reports.
        """
        with self._lock:
            self._conn.execute(
                "INSERT INTO radar_observations "
                "(ts, precip_accum_mm_1h, precip_type, quality) VALUES (?, ?, ?, ?)",
                (ts, precip_accum_mm_1h, precip_type, quality),
            )
            self._conn.commit()

    def get_radar_observations_between(
        self, start_ts: str, end_ts: str
    ) -> list[sqlite3.Row]:
        """v0.1.24 (P2-08 / IND-10): radar_observations was written on the
        5-minute radar path and never read by anything. This is its first
        consumer — StormEventReconciliationCoordinator needs the radar
        evidence across a prediction's follow-up window to decide whether
        a predicted storm actually happened.

        Mirrors the existing get_station_observations_between.
        """
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM radar_observations WHERE ts >= ? AND ts <= ? "
                "ORDER BY ts ASC",
                (start_ts, end_ts),
            )
            return cur.fetchall()

    def get_latest_radar_observation(self) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM radar_observations ORDER BY ts DESC LIMIT 1"
            )
            return cur.fetchone()

    # -- bucket stats (Model A) -------------------------------------------------

    def get_bucket_stats(self, key: BucketKey) -> Optional[BucketStats]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT ema_bias, ema_abs_error, ema_weight, sample_count, last_updated "
                "FROM bucket_stats "
                "WHERE hour_of_day = ? AND season = ? AND lead_time_bucket = ? "
                "AND source = ? AND measurement = ?",
                (key.hour_of_day, key.season, key.lead_time_bucket, key.source, key.measurement),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return BucketStats(
            ema_bias=row["ema_bias"],
            ema_abs_error=row["ema_abs_error"],
            ema_weight=row["ema_weight"],
            sample_count=row["sample_count"],
            last_updated=row["last_updated"],
        )

    def upsert_bucket_stats(
        self,
        key: BucketKey,
        ema_bias: float,
        ema_abs_error: float,
        ema_weight: float,
        sample_count: int,
        last_updated: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO bucket_stats "
                "(hour_of_day, season, lead_time_bucket, source, measurement, "
                " ema_bias, ema_abs_error, ema_weight, sample_count, last_updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(hour_of_day, season, lead_time_bucket, source, measurement) "
                "DO UPDATE SET ema_bias=excluded.ema_bias, "
                "ema_abs_error=excluded.ema_abs_error, "
                "ema_weight=excluded.ema_weight, "
                "sample_count=excluded.sample_count, "
                "last_updated=excluded.last_updated",
                (
                    key.hour_of_day,
                    key.season,
                    key.lead_time_bucket,
                    key.source,
                    key.measurement,
                    ema_bias,
                    ema_abs_error,
                    ema_weight,
                    sample_count,
                    last_updated,
                ),
            )
            self._conn.commit()

    def apply_reconciliation_batch(
        self,
        bucket_updates: list[tuple[BucketKey, float, float, float, int, str]],
        reconciled_ids: list[int],
        skipped_ids: list[int],
    ) -> None:
        """Apply one reconciliation cycle's EMA writes and status
        transitions as a single all-or-nothing transaction.

        **v0.1.24 fix (P0-01), CRITICAL.** upsert_bucket_stats() committed
        per row inside the reconciliation loop, while
        mark_forecast_snapshots_status() ran once, in bulk, at the end. A
        crash between those two points left bucket_stats already updated
        for rows still marked 'pending' — so the next cycle re-selected
        those rows and folded them into the EMA a second time. That
        defeats the at-most-once learning guarantee the entire v0.1.23
        reconciliation_status redesign exists to provide, reintroducing
        the prior pass's L-01 double-counting bug through a crash
        boundary instead of through watermark arithmetic.

        An EMA cannot un-absorb a duplicated sample, so "we will notice
        and fix it later" is not available as a recovery strategy. One
        commit or none at all is the only safe shape.

        **On the explicit rollback.** SQLite only auto-rolls-back an open
        transaction on the next process start. An exception caught within
        the same still-running process leaves the transaction open
        indefinitely, holding locks and leaving subsequent writes to join
        a transaction that was supposed to have been abandoned. The
        explicit rollback below is what makes this actually atomic in the
        case that matters most.
        """
        if not bucket_updates and not reconciled_ids and not skipped_ids:
            return

        with self._lock:
            try:
                for key, ema_bias, ema_abs_error, ema_weight, sample_count, last_updated in (
                    bucket_updates
                ):
                    self._conn.execute(
                        "INSERT INTO bucket_stats "
                        "(hour_of_day, season, lead_time_bucket, source, measurement, "
                        " ema_bias, ema_abs_error, ema_weight, sample_count, last_updated) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(hour_of_day, season, lead_time_bucket, source, measurement) "
                        "DO UPDATE SET ema_bias=excluded.ema_bias, "
                        "ema_abs_error=excluded.ema_abs_error, "
                        "ema_weight=excluded.ema_weight, "
                        "sample_count=excluded.sample_count, "
                        "last_updated=excluded.last_updated",
                        (
                            key.hour_of_day,
                            key.season,
                            key.lead_time_bucket,
                            key.source,
                            key.measurement,
                            ema_bias,
                            ema_abs_error,
                            ema_weight,
                            sample_count,
                            last_updated,
                        ),
                    )

                for status, ids in (("reconciled", reconciled_ids), ("skipped", skipped_ids)):
                    if not ids:
                        continue
                    placeholders = ",".join("?" for _ in ids)
                    self._conn.execute(
                        f"UPDATE forecast_snapshots SET reconciliation_status = ? "
                        f"WHERE id IN ({placeholders})",
                        (status, *ids),
                    )

                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # v0.1.23 cleanup: get_all_bucket_stats_for_measurement_hour() was removed
    # here. It predated the v0.1.13 bulk-query rework of the blend path
    # (get_all_bucket_stats() now does one query for the whole blend cycle
    # instead of one per hour/measurement), had no remaining production
    # caller, and — unlike insert_forecast_snapshot()/
    # get_forecast_values_for_valid_at() below, which are kept because
    # tests exercise them directly as documented utilities — had no test
    # coverage of its own either. Renormalization now happens inline in
    # model_a.blend() via the weighted-average's own division by the
    # summed weights, which is what its docstring's claim about
    # renormalization was describing a design that no longer exists.

    # -- storm events (Model B ground truth) -------------------------------------

    def insert_storm_event(
        self,
        start_ts: str,
        end_ts: Optional[str],
        peak_pressure_drop: Optional[float],
        peak_temp_drop: Optional[float],
        peak_precip_rate: Optional[float],
        notes: Optional[str] = None,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO storm_events "
                "(start_ts, end_ts, peak_pressure_drop, peak_temp_drop, peak_precip_rate, notes) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (start_ts, end_ts, peak_pressure_drop, peak_temp_drop, peak_precip_rate, notes),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_all_storm_events(self) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM storm_events ORDER BY start_ts ASC")
            return cur.fetchall()

    # -- storm predictions (Model B live output, for calibration later) ---------

    def insert_storm_prediction(
        self, ts: str, probability: float, features: dict[str, Any]
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO storm_predictions (ts, probability, features) VALUES (?, ?, ?)",
                (ts, probability, json.dumps(features)),
            )
            self._conn.commit()

    def get_storm_predictions_since(self, since_ts: str) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM storm_predictions WHERE ts >= ? ORDER BY ts ASC", (since_ts,)
            )
            return cur.fetchall()

    # -- purge (high-volume tables only — see plan doc §5) -----------------------

    def purge_older_than(self, cutoff_ts: str) -> dict[str, int]:
        """Delete rows older than cutoff_ts from the high-volume tables only.

        bucket_stats stays small permanently by design and is never purged.
        storm_events is Model B's whole training set and is never purged.
        Returns a dict of table -> rows deleted, for logging/audit.

        v0.1.23 fix (L-10): this method existed and was correctly
        implemented, but nothing in production ever called it — the
        configured purge_days setting had no operational effect at all.
        See RetentionCoordinator in coordinator.py for the scheduled
        caller added to fix that.

        v0.1.23 fix (L-10, audit's own stated recommendation — "protect
        unreconciled snapshots"): forecast_snapshots rows with
        reconciliation_status = 'pending' are now excluded from deletion
        regardless of age. Without this, a purge_days window shorter than
        RETRY_GIVE_UP_AGE (48h) could delete a forecast row Model A
        learning was still actively waiting to retry-match against a
        station observation, permanently losing that learning sample
        instead of the row aging out through the normal 'skipped' path.
        """
        deleted: dict[str, int] = {}
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM forecast_snapshots "
                "WHERE valid_at < ? AND reconciliation_status != 'pending'",
                (cutoff_ts,),
            )
            deleted["forecast_snapshots"] = cur.rowcount
            for table, ts_col in (
                ("station_observations", "ts"),
                ("radar_observations", "ts"),
                ("storm_predictions", "ts"),
            ):
                cur = self._conn.execute(
                    f"DELETE FROM {table} WHERE {ts_col} < ?", (cutoff_ts,)
                )
                deleted[table] = cur.rowcount
            self._conn.commit()
        return deleted

    # -- durable runtime state (v0.1.23 fixes L-06/L-05/L-04/L-07/L-08/L-09) -----
    # All of these reuse the schema_meta key/value table, the same pattern
    # already established for reconciliation_watermark above — each is a
    # single small value, not worth a dedicated table for. Restart-safety
    # for these was the audit's core "L-0x resets on restart" complaint;
    # persisting them here (instead of only in coordinator instance
    # attributes) is the actual fix.

    def _get_meta(self, key: str) -> Optional[str]:
        with self._lock:
            cur = self._conn.execute("SELECT value FROM schema_meta WHERE key = ?", (key,))
            row = cur.fetchone()
            return row["value"] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn.commit()

    def get_provider_run_fingerprint(self, source: str) -> Optional[str]:
        """Fixes L-06 (Open-Meteo's dedup fingerprint was memory-only) and
        extends the same durable mechanism to Meteoblue (L-05, which had no
        dedup at all) and SRF (L-04's practical concern) — see
        fingerprint.py and each coordinator's use of it."""
        return self._get_meta(f"run_fingerprint:{source}")

    def set_provider_run_fingerprint(self, source: str, fingerprint: str) -> None:
        self._set_meta(f"run_fingerprint:{source}", fingerprint)

    def _safe_parse_meta(self, key: str, parse: Callable[[str], Any]) -> Optional[Any]:
        """Read and parse a schema_meta value, tolerating corruption.

        **v0.1.24 fix (P2-02).** get_annual_call_budget_state,
        get_bonus_call_tracker_state and get_model_b_previous_probability
        called json.loads() / float() directly against whatever text
        happened to be in schema_meta, with no handling for a truncated
        write (a crash mid-write) or manual tampering. The resulting
        exception propagated out of the coordinator's
        _async_load_persisted_state_if_needed() and prevented that
        coordinator from starting AT ALL — a corrupted byte in a quota
        counter took down the source it belonged to, permanently, on
        every subsequent restart.

        Recovery here has three parts, and the third is the one that
        matters: the corrupt value is CLEARED, so the failure does not
        repeat forever. Returning None puts the caller in the exact state
        it already handles correctly — "nothing has ever been persisted"
        — so no caller needs a new code path.
        """
        raw = self._get_meta(key)
        if raw is None:
            return None
        try:
            return parse(raw)
        except (ValueError, TypeError, json.JSONDecodeError):
            _LOGGER.warning(
                "Discarding corrupted persisted state for %s (value could not be "
                "parsed); treating it as never-persisted and clearing it so this "
                "does not recur on every restart",
                key,
            )
            try:
                self._set_meta(key, "")
                with self._lock:
                    self._conn.execute(
                        "DELETE FROM schema_meta WHERE key = ?", (key,)
                    )
                    self._conn.commit()
            except sqlite3.Error:  # pragma: no cover - defensive
                pass
            return None

    def get_annual_call_budget_state(self, source: str) -> Optional[dict]:
        """Fixes L-07 (Meteonomiqs' annual quota counter reset on restart).

        v0.1.24 (P2-02): parsed defensively — see _safe_parse_meta.
        """
        return self._safe_parse_meta(f"annual_call_budget:{source}", json.loads)

    def set_annual_call_budget_state(self, source: str, year: int, calls_used: int) -> None:
        # v0.1.23 fix: originally `year` and `calls_used` were keyword-only
        # (`*`-separated). Found broken by a coordinator-level test:
        # hass.async_add_executor_job(func, *args) — both the real Home
        # Assistant implementation and this project's own test fakes —
        # only supports positional arguments, so the original signature
        # made this method impossible to call through that path at all
        # despite being written specifically to be called through it.
        # Positional now, matching every other setter here.
        self._set_meta(
            f"annual_call_budget:{source}",
            json.dumps({"year": year, "calls_used": calls_used}),
        )

    def get_bonus_call_tracker_state(self, source: str) -> Optional[dict]:
        """Fixes L-08 (Meteoblue's — and, by the same bug class,
        Meteonomiqs' — same-day bonus-call allowance reset on restart).

        v0.1.24 (P2-02): parsed defensively — see _safe_parse_meta.
        """
        return self._safe_parse_meta(f"bonus_call_tracker:{source}", json.loads)

    def set_bonus_call_tracker_state(self, source: str, state: dict) -> None:
        self._set_meta(f"bonus_call_tracker:{source}", json.dumps(state))

    def get_last_scheduled_call_hour(self, source: str) -> Optional[str]:
        """Fixes L-08's other half (Meteoblue's already-serviced scheduled
        slot forgotten on restart, risking a duplicate provider call for
        the same slot right after a reload)."""
        return self._get_meta(f"last_scheduled_call_hour:{source}")

    def set_last_scheduled_call_hour(self, source: str, iso_ts: str) -> None:
        self._set_meta(f"last_scheduled_call_hour:{source}", iso_ts)

    def get_model_b_previous_probability(self) -> Optional[float]:
        """Fixes L-09 (Model B's previous_probability reset to 0.0 on
        restart, which could misread an already-elevated storm probability
        as a fresh upward crossing and fire an unwarranted bonus call).

        **v0.1.24 (P0-02)**: the value stored here is now Model B's
        UNREFINED base probability, not the Meteonomiqs-refined one. See
        ModelBCoordinator._async_update_data_inner for why storing the
        refined value corrupted crossing detection.

        v0.1.24 (P2-02): parsed defensively — see _safe_parse_meta.
        """
        return self._safe_parse_meta("model_b_previous_probability", float)

    def set_model_b_previous_probability(self, probability: float) -> None:
        self._set_meta("model_b_previous_probability", repr(probability))

    # -- Meteonomiqs daily-call bookkeeping (v0.1.24, P1-08) -------------------

    def get_meteonomiqs_last_successful_call_date(self) -> Optional[str]:
        """The ISO date (YYYY-MM-DD) of the last successful Meteonomiqs call.

        **v0.1.24 fix (P1-08).** MeteonomiqsCoordinator held this in
        memory only, so it reset to None on every restart. The daily gate

            if self._last_successful_call_date == today: return None

        then failed to recognise a day already serviced, and fired an
        unnecessary extra call against a 1000-calls/year budget after any
        same-day restart — which, during setup or troubleshooting, can
        easily be several restarts in one afternoon.

        Stored as a plain string rather than JSON: it is a single scalar,
        and _safe_parse_meta's protection is unnecessary for a value that
        cannot fail to parse.
        """
        return self._get_meta("meteonomiqs_last_successful_call_date")

    def set_meteonomiqs_last_successful_call_date(self, iso_date: str) -> None:
        self._set_meta("meteonomiqs_last_successful_call_date", iso_date)

    # -- SRF geolocation cache (v0.1.24, IND-07) -------------------------------

    def get_srf_geolocation_id(self, coordinate_key: str) -> Optional[str]:
        """SRF's geolocation ID for a rounded coordinate pair.

        **v0.1.24 fix (IND-07).** SrfClient held both its OAuth token and
        its resolved geolocation ID as plain instance attributes, and the
        client is constructed fresh on every setup. Since every options
        change triggers a config-entry reload, each one re-ran the
        geolocation lookup — an avoidable quota-consuming call against
        the one source with a rotating credential. This project already
        persists exactly this class of state for meteoblue and
        Meteonomiqs (the L-07/L-08 fixes); SRF simply never received the
        same treatment.

        Keyed by rounded coordinates so that relocating the installation
        naturally invalidates the cache instead of silently reusing the
        old location's ID.
        """
        return self._get_meta(f"srf_geolocation_id:{coordinate_key}")

    def set_srf_geolocation_id(self, coordinate_key: str, geolocation_id: str) -> None:
        self._set_meta(f"srf_geolocation_id:{coordinate_key}", geolocation_id)

    # -- storm prediction reconciliation (v0.1.24, P2-08) ----------------------

    def get_unreconciled_storm_predictions(
        self, ts_before: str, min_probability: float
    ) -> list[sqlite3.Row]:
        """Predictions whose follow-up window has fully elapsed and which
        were confident enough to be worth checking.

        The probability floor matters: a score that never crossed the
        reporting threshold made no claim, so there is no outcome to
        confirm and marking it either way would pollute the training set.
        """
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM storm_predictions "
                "WHERE reconciled = 0 AND ts < ? AND probability >= ? "
                "ORDER BY ts ASC",
                (ts_before, min_probability),
            )
            return cur.fetchall()

    def mark_storm_predictions_reconciled(self, ids: list[int]) -> None:
        """Mark predictions as checked, whatever the outcome was.

        Confirmed and unconfirmed predictions are both marked, so nothing
        is ever re-checked — a prediction that did not verify is a
        negative training example, not an unfinished job.
        """
        if not ids:
            return
        with self._lock:
            placeholders = ",".join("?" for _ in ids)
            self._conn.execute(
                f"UPDATE storm_predictions SET reconciled = 1 WHERE id IN ({placeholders})",
                tuple(ids),
            )
            self._conn.commit()

    # -- database size telemetry (v0.1.24, IND-06) -----------------------------

    def get_storage_stats(self) -> dict[str, Any]:
        """Row counts per table plus the file size on disk.

        **v0.1.24 (IND-06).** Retention defaulted to "keep forever" and
        nothing anywhere reported how large the database had become, so
        unbounded growth was invisible until the disk filled. Surfaced
        through diagnostics.py and the storage sensor.
        """
        stats: dict[str, Any] = {}
        with self._lock:
            for table in (
                "forecast_snapshots",
                "station_observations",
                "radar_observations",
                "bucket_stats",
                "storm_predictions",
                "storm_events",
            ):
                try:
                    cur = self._conn.execute(f"SELECT COUNT(*) AS n FROM {table}")
                    stats[f"{table}_rows"] = int(cur.fetchone()["n"])
                except sqlite3.Error:
                    stats[f"{table}_rows"] = None
        try:
            stats["file_size_bytes"] = os.path.getsize(self._db_path)
        except OSError:
            stats["file_size_bytes"] = None
        return stats
