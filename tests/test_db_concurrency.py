"""Concurrency test for the v0.1.12 threading.Lock fix.

Motivation: a real deployment's diagnostics showed CombiPrecip (5-min
interval), SRF (45-min), Open-Meteo (15-min), and the Model A learning
coordinator (20-min) all succeed once in a ~3-second startup burst, then
go completely silent — every one of them, simultaneously — for 5+ hours.
That pattern (a shared resource works once, then every consumer of it
hangs together) pointed at the one thing every coordinator actually
shares: a single SQLite connection accessed concurrently from multiple
Home Assistant executor-pool threads. This test simulates exactly that —
many threads hammering the same SwissWeatherDB instance simultaneously,
the way multiple coordinators' executor jobs firing close together in
real deployment would.

This can't prove the lock was *the* cause of the real freeze (that would
need reproducing it against the actual failure, which isn't practical
here) — but it does prove concurrent access is now safe rather than a
live hazard, which is the correct fix regardless of the exact mechanism
behind what was observed.
"""
import os
import tempfile
import threading

import pytest

from swissweather_fusion.storage.db import BucketKey, SwissWeatherDB


@pytest.fixture
def db():
    path = tempfile.mktemp(suffix=".db")
    database = SwissWeatherDB(path)
    yield database
    database.close()
    os.remove(path)


def test_concurrent_writes_from_many_threads_all_complete(db):
    """The core claim: many threads hitting the same connection
    simultaneously all finish and all data lands correctly — no hang, no
    lost writes, no corruption. Without the lock, this is exactly the
    scenario that risks two threads touching the same sqlite3.Connection
    object at once.
    """
    thread_count = 20
    writes_per_thread = 10
    errors: list[Exception] = []

    def worker(thread_id: int) -> None:
        try:
            for i in range(writes_per_thread):
                db.insert_station_observation(
                    f"2026-07-{(thread_id % 28) + 1:02d}T{i:02d}:00:00+00:00",
                    20.0 + thread_id,
                    50.0,
                    1013.0,
                )
        except Exception as err:  # noqa: BLE001
            errors.append(err)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(thread_count)]
    for t in threads:
        t.start()
    # A generous but bounded join timeout — if the lock were somehow
    # itself broken (e.g. never released), this would time out rather
    # than hang the test suite forever.
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), "a thread did not complete — possible deadlock"

    assert errors == [], f"concurrent writes raised: {errors}"

    all_rows = db.get_station_observations_since("2000-01-01T00:00:00+00:00")
    assert len(all_rows) == thread_count * writes_per_thread


def test_concurrent_mixed_read_write_on_bucket_stats(db):
    """A more realistic mix: some threads reading bucket_stats while
    others upsert into it concurrently — closer to what actually happens
    in production (the blend coordinator reading bucket_stats while the
    learning coordinator writes to it, from different executor threads,
    potentially at the same moment).
    """
    key = BucketKey(
        hour_of_day=12, season="JJA", lead_time_bucket="short",
        source="ch1", measurement="temperature",
    )
    errors: list[Exception] = []
    stop = threading.Event()

    def writer() -> None:
        try:
            for i in range(50):
                db.upsert_bucket_stats(
                    key, ema_bias=float(i), ema_abs_error=1.0, ema_weight=1.0,
                    sample_count=i, last_updated="2026-07-25T12:00:00+00:00",
                )
        except Exception as err:  # noqa: BLE001
            errors.append(err)
        finally:
            stop.set()

    def reader() -> None:
        try:
            while not stop.is_set():
                db.get_bucket_stats(key)  # may return None early on, that's fine
        except Exception as err:  # noqa: BLE001
            errors.append(err)

    writer_thread = threading.Thread(target=writer)
    reader_threads = [threading.Thread(target=reader) for _ in range(5)]

    writer_thread.start()
    for t in reader_threads:
        t.start()

    writer_thread.join(timeout=30)
    assert not writer_thread.is_alive(), "writer thread did not complete — possible deadlock"
    for t in reader_threads:
        t.join(timeout=5)

    assert errors == [], f"concurrent read/write raised: {errors}"
    final = db.get_bucket_stats(key)
    assert final is not None
    assert final.sample_count == 49  # last write's value, nothing corrupted
