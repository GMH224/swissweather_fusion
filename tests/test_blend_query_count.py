"""Proves the actual claim behind the v0.1.13 fix: the blend computation
now takes a small, fixed number of database queries regardless of how
many hours/sources/measurements are involved, instead of one query per
individual (hour, measurement, source) combination.

Doesn't import coordinator.py directly (it pulls in Home Assistant) —
instead this counts real `sqlite3.Connection.execute` calls made by the
actual storage methods the blend coordinator now uses
(get_forecast_snapshots_in_window, get_all_bucket_stats), confirming each
is genuinely one query regardless of data volume — which is the property
that makes the coordinator's own per-cycle query count small and fixed
rather than scaling with the 168-hour × 5-measurement × 5-source space it
used to.
"""
import os
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


def _count_real_queries(db: SwissWeatherDB, fn) -> int:
    """Counts actual SQL statements executed against the connection while
    fn() runs, using sqlite3's own supported trace mechanism
    (set_trace_callback) rather than monkey-patching the connection
    object directly — sqlite3.Connection's execute/executemany attributes
    are read-only C-level attributes and can't be patched.
    """
    count = 0

    def trace(_statement: str) -> None:
        nonlocal count
        count += 1

    db._conn.set_trace_callback(trace)
    try:
        fn()
    finally:
        db._conn.set_trace_callback(None)
    return count


def test_forecast_window_bulk_fetch_is_one_query_regardless_of_row_count(db):
    """The core claim: whether there are 10 rows or 1000 rows in the
    168-hour window, get_forecast_snapshots_in_window is exactly one
    query — not one query per (hour, measurement, source) combination the
    way the coordinator used to do it (previously up to ~8,400 per
    cycle).
    """
    # Simulate a realistic, sizeable dataset: 5 sources x 5 measurements x
    # 168 hours = 4,200 rows, comparable to real accumulated data.
    sources = ("ch1", "ch2", "icon_d2", "srf", "meteoblue")
    measurements = ("temperature", "humidity", "pressure", "precip", "wind_speed")
    rows = []
    for source in sources:
        for measurement in measurements:
            for hour in range(168):
                rows.append((
                    source, "2026-07-25T00:00:00+00:00",
                    f"2026-07-{25 + hour // 24:02d}T{hour % 24:02d}:00:00+00:00",
                    measurement, 20.0, "scheduled",
                ))
    db.insert_forecast_snapshots_bulk(rows)  # one bulk insert, not part of what's timed below

    query_count = _count_real_queries(
        db,
        lambda: db.get_forecast_snapshots_in_window(
            start_valid_at="2026-07-25T00:00:00+00:00",
            end_valid_at="2026-08-01T00:00:00+00:00",
        ),
    )
    assert query_count == 1


def test_bucket_stats_bulk_fetch_is_one_query_regardless_of_row_count(db):
    """Same claim for the other half of the old per-call pattern —
    get_all_bucket_stats replaces what used to be one get_bucket_stats
    call per (hour, season, lead_time, source, measurement) combination.
    """
    for hour in range(24):
        for source in ("ch1", "ch2"):
            key = BucketKey(
                hour_of_day=hour, season="JJA", lead_time_bucket="short",
                source=source, measurement="temperature",
            )
            db.upsert_bucket_stats(key, 0.1, 0.2, 1.0, 5, "2026-07-25T12:00:00+00:00")

    query_count = _count_real_queries(db, lambda: db.get_all_bucket_stats())
    assert query_count == 1
