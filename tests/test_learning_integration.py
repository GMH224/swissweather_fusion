"""End-to-end test for the Model A learning reconciliation flow.

Each piece (DB queries, find_nearest_observation, update_bucket_ema,
derive_season/derive_lead_time_bucket) is already unit-tested in
isolation. This test exercises them together the same way
ModelALearningCoordinator._reconcile does, using a real (temp-file)
database and the real model_a functions — no mocking — specifically to
catch integration-level mistakes (wrong parameter order, an off-by-one in
bucket key construction, etc.) that per-piece unit tests wouldn't.

coordinator.py itself isn't imported here (it pulls in Home Assistant),
so this replicates its reconciliation logic inline using the same
building blocks it calls — see coordinator.py's ModelALearningCoordinator
for the production code this mirrors.
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from swissweather_fusion.models import model_a
from swissweather_fusion.storage.db import BucketKey, SwissWeatherDB


@pytest.fixture
def db():
    path = tempfile.mktemp(suffix=".db")
    database = SwissWeatherDB(path)
    yield database
    database.close()
    os.remove(path)


def _reconcile_once(db: SwissWeatherDB, *, since: datetime, now: datetime) -> int:
    """Mirrors ModelALearningCoordinator._reconcile's logic exactly,
    without the Home Assistant coordinator wrapper around it.
    """
    measurements = ("temperature", "humidity", "pressure")
    since_iso = since.isoformat()
    until_iso = now.isoformat()

    rows_to_reconcile = db.get_forecast_snapshots_to_reconcile(
        since_ts=since_iso, until_ts=until_iso, measurements=measurements
    )
    if not rows_to_reconcile:
        db.set_reconciliation_watermark(until_iso)
        return 0

    tolerance = timedelta(minutes=model_a.RECONCILIATION_TOLERANCE_MINUTES)
    station_rows = db.get_station_observations_between(
        (since - tolerance).isoformat(), (now + tolerance).isoformat()
    )
    candidates_by_measurement: dict = {"temperature": [], "humidity": [], "pressure": []}
    for row in station_rows:
        ts = datetime.fromisoformat(row["ts"])
        candidates_by_measurement["temperature"].append((ts, row["temperature"]))
        candidates_by_measurement["humidity"].append((ts, row["humidity"]))
        candidates_by_measurement["pressure"].append((ts, row["pressure"]))

    reconciled = 0
    for fs_row in rows_to_reconcile:
        if fs_row["value"] is None:
            continue
        measurement = fs_row["variable"]
        valid_at = datetime.fromisoformat(fs_row["valid_at"])
        issued_at = datetime.fromisoformat(fs_row["issued_at"])

        actual_value = model_a.find_nearest_observation(
            target=valid_at, candidates=candidates_by_measurement[measurement]
        )
        if actual_value is None:
            continue

        key = BucketKey(
            hour_of_day=valid_at.hour,
            season=model_a.derive_season(valid_at),
            lead_time_bucket=model_a.derive_lead_time_bucket(issued_at, valid_at),
            source=fs_row["source"],
            measurement=measurement,
        )
        existing = db.get_bucket_stats(key)
        if existing is None:
            previous_bias, previous_abs_error, previous_sample_count = 0.0, 0.0, 0
        else:
            previous_bias = existing.ema_bias
            previous_abs_error = existing.ema_abs_error
            previous_sample_count = existing.sample_count

        result = model_a.update_bucket_ema(
            previous_bias=previous_bias,
            previous_abs_error=previous_abs_error,
            previous_sample_count=previous_sample_count,
            forecast_value=fs_row["value"],
            actual_value=actual_value,
            lead_time_bucket=key.lead_time_bucket,
        )
        db.upsert_bucket_stats(
            key,
            ema_bias=result.ema_bias,
            ema_abs_error=result.ema_abs_error,
            ema_weight=result.ema_weight,
            sample_count=result.sample_count,
            last_updated=now.isoformat(),
        )
        reconciled += 1

    db.set_reconciliation_watermark(until_iso)
    return reconciled


def test_reconciliation_populates_bucket_stats_end_to_end(db):
    """The core claim being tested: before this feature existed,
    bucket_stats stayed empty forever. This confirms a forecast + a
    matching station observation now actually produces a real bucket_stats
    row via the full flow, not just via directly-called individual
    functions.
    """
    issued_at = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    valid_at = datetime(2026, 7, 25, 15, 0, tzinfo=timezone.utc)  # 6h lead -> "short"
    now = valid_at + timedelta(minutes=10)

    db.insert_forecast_snapshot(
        "ch1", issued_at.isoformat(), valid_at.isoformat(), "temperature", 22.0
    )
    # Actual station reading close to (but not exactly at) valid_at.
    db.insert_station_observation(
        (valid_at + timedelta(minutes=5)).isoformat(), 20.0, 55.0, 1013.0
    )

    since = issued_at - timedelta(hours=1)
    reconciled = _reconcile_once(db, since=since, now=now)
    assert reconciled == 1

    key = BucketKey(
        hour_of_day=15, season="JJA", lead_time_bucket="short",
        source="ch1", measurement="temperature",
    )
    stats = db.get_bucket_stats(key)
    assert stats is not None
    assert stats.sample_count == 1
    assert stats.ema_bias == 2.0  # forecast (22.0) - actual (20.0)

    # Watermark advanced, so a second identical run (nothing new since)
    # reconciles zero additional rows rather than double-counting.
    assert db.get_reconciliation_watermark() == now.isoformat()
    reconciled_again = _reconcile_once(db, since=now, now=now + timedelta(minutes=5))
    assert reconciled_again == 0


def test_reconciliation_skips_precip_and_wind_speed(db):
    """precip/wind_speed have no station ground truth yet (no rain/wind
    sensors) — forecasts for those measurements should never be
    reconciled, only stored.
    """
    issued_at = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    valid_at = datetime(2026, 7, 25, 15, 0, tzinfo=timezone.utc)
    now = valid_at + timedelta(minutes=10)

    db.insert_forecast_snapshot("ch1", issued_at.isoformat(), valid_at.isoformat(), "precip", 0.5)
    db.insert_forecast_snapshot("ch1", issued_at.isoformat(), valid_at.isoformat(), "wind_speed", 3.0)
    db.insert_station_observation(valid_at.isoformat(), 20.0, 55.0, 1013.0)

    reconciled = _reconcile_once(db, since=issued_at - timedelta(hours=1), now=now)
    assert reconciled == 0  # neither precip nor wind_speed get reconciled

    key = BucketKey(hour_of_day=15, season="JJA", lead_time_bucket="short", source="ch1", measurement="precip")
    assert db.get_bucket_stats(key) is None


def test_reconciliation_skips_when_no_station_reading_within_tolerance(db):
    issued_at = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    valid_at = datetime(2026, 7, 25, 15, 0, tzinfo=timezone.utc)
    now = valid_at + timedelta(minutes=10)

    db.insert_forecast_snapshot("ch1", issued_at.isoformat(), valid_at.isoformat(), "temperature", 22.0)
    # Station reading exists, but an hour away — outside the 30-min tolerance.
    db.insert_station_observation((valid_at + timedelta(hours=1)).isoformat(), 20.0, 55.0, 1013.0)

    reconciled = _reconcile_once(db, since=issued_at - timedelta(hours=1), now=now)
    assert reconciled == 0

    key = BucketKey(hour_of_day=15, season="JJA", lead_time_bucket="short", source="ch1", measurement="temperature")
    assert db.get_bucket_stats(key) is None


def test_reconciliation_second_observation_moves_ema_not_replaces(db):
    """Confirms the EMA behavior (moves toward new observations, doesn't
    jump to them) survives the full round-trip through storage, not just
    when called directly in isolation.
    """
    issued_at = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    valid_at_1 = datetime(2026, 7, 25, 15, 0, tzinfo=timezone.utc)
    valid_at_2 = datetime(2026, 7, 26, 15, 0, tzinfo=timezone.utc)  # same hour_of_day/season, next day
    now = valid_at_2 + timedelta(minutes=10)

    db.insert_forecast_snapshot("ch1", issued_at.isoformat(), valid_at_1.isoformat(), "temperature", 22.0)
    db.insert_station_observation(valid_at_1.isoformat(), 18.0, 55.0, 1013.0)  # bias = 4.0

    db.insert_forecast_snapshot(
        "ch1", (issued_at + timedelta(days=1)).isoformat(), valid_at_2.isoformat(), "temperature", 22.0
    )
    db.insert_station_observation(valid_at_2.isoformat(), 22.0, 55.0, 1013.0)  # bias = 0.0

    _reconcile_once(db, since=issued_at - timedelta(hours=1), now=now)

    key = BucketKey(hour_of_day=15, season="JJA", lead_time_bucket="short", source="ch1", measurement="temperature")
    stats = db.get_bucket_stats(key)
    assert stats.sample_count == 2
    # Moved from 4.0 toward 0.0, but didn't jump straight to 0.0 — genuine
    # EMA smoothing survived the round trip through real storage.
    assert 0.0 < stats.ema_bias < 4.0
