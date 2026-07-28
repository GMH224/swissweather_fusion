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
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA_VERSION = 1

_SCHEMA_SQL = """
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
CREATE INDEX IF NOT EXISTS idx_station_ts ON station_observations(ts);

CREATE TABLE IF NOT EXISTS forecast_snapshots (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    valid_at TEXT NOT NULL,
    variable TEXT NOT NULL,
    value REAL,
    trigger_reason TEXT DEFAULT 'scheduled'
);
CREATE INDEX IF NOT EXISTS idx_forecast_source_ts ON forecast_snapshots(source, valid_at);

CREATE TABLE IF NOT EXISTS radar_observations (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    precip_rate_mmh REAL,
    precip_type TEXT
);
CREATE INDEX IF NOT EXISTS idx_radar_ts ON radar_observations(ts);

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

CREATE TABLE IF NOT EXISTS storm_predictions (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    probability REAL NOT NULL,
    features TEXT
);
CREATE INDEX IF NOT EXISTS idx_predictions_ts ON storm_predictions(ts);
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
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
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
        self._conn.executescript(_SCHEMA_SQL)
        cur = self._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        )
        row = cur.fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- station observations -------------------------------------------------

    def insert_station_observation(
        self,
        ts: str,
        temperature: Optional[float],
        humidity: Optional[float],
        pressure: Optional[float],
    ) -> None:
        self._conn.execute(
            "INSERT INTO station_observations (ts, temperature, humidity, pressure) "
            "VALUES (?, ?, ?, ?)",
            (ts, temperature, humidity, pressure),
        )
        self._conn.commit()

    def get_station_observations_since(self, since_ts: str) -> list[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM station_observations WHERE ts >= ? ORDER BY ts ASC",
            (since_ts,),
        )
        return cur.fetchall()

    def get_latest_station_observation(self) -> Optional[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM station_observations ORDER BY ts DESC LIMIT 1"
        )
        return cur.fetchone()

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
        cur = self._conn.execute(
            "SELECT * FROM forecast_snapshots "
            "WHERE source = ? AND variable = ? AND valid_at = ? "
            "ORDER BY issued_at DESC",
            (source, variable, valid_at),
        )
        return cur.fetchall()

    # -- radar observations (CombiPrecip) ----------------------------------------

    def insert_radar_observation(
        self, ts: str, precip_rate_mmh: Optional[float], precip_type: Optional[str]
    ) -> None:
        self._conn.execute(
            "INSERT INTO radar_observations (ts, precip_rate_mmh, precip_type) "
            "VALUES (?, ?, ?)",
            (ts, precip_rate_mmh, precip_type),
        )
        self._conn.commit()

    def get_latest_radar_observation(self) -> Optional[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM radar_observations ORDER BY ts DESC LIMIT 1"
        )
        return cur.fetchone()

    # -- bucket stats (Model A) -------------------------------------------------

    def get_bucket_stats(self, key: BucketKey) -> Optional[BucketStats]:
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

    def get_all_bucket_stats_for_measurement_hour(
        self, hour_of_day: int, season: str, lead_time_bucket: str, measurement: str
    ) -> list[sqlite3.Row]:
        """All sources' stats for one (hour, season, lead_time, measurement) —
        this is exactly the set the blend needs to renormalize weights over.
        """
        cur = self._conn.execute(
            "SELECT * FROM bucket_stats WHERE hour_of_day = ? AND season = ? "
            "AND lead_time_bucket = ? AND measurement = ?",
            (hour_of_day, season, lead_time_bucket, measurement),
        )
        return cur.fetchall()

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
        cur = self._conn.execute(
            "INSERT INTO storm_events "
            "(start_ts, end_ts, peak_pressure_drop, peak_temp_drop, peak_precip_rate, notes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (start_ts, end_ts, peak_pressure_drop, peak_temp_drop, peak_precip_rate, notes),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_all_storm_events(self) -> list[sqlite3.Row]:
        cur = self._conn.execute("SELECT * FROM storm_events ORDER BY start_ts ASC")
        return cur.fetchall()

    # -- storm predictions (Model B live output, for calibration later) ---------

    def insert_storm_prediction(
        self, ts: str, probability: float, features: dict[str, Any]
    ) -> None:
        self._conn.execute(
            "INSERT INTO storm_predictions (ts, probability, features) VALUES (?, ?, ?)",
            (ts, probability, json.dumps(features)),
        )
        self._conn.commit()

    def get_storm_predictions_since(self, since_ts: str) -> list[sqlite3.Row]:
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
        """
        deleted: dict[str, int] = {}
        for table, ts_col in (
            ("station_observations", "ts"),
            ("forecast_snapshots", "valid_at"),
            ("radar_observations", "ts"),
            ("storm_predictions", "ts"),
        ):
            cur = self._conn.execute(
                f"DELETE FROM {table} WHERE {ts_col} < ?", (cutoff_ts,)
            )
            deleted[table] = cur.rowcount
        self._conn.commit()
        return deleted
