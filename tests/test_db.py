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
    db.insert_radar_observation("2026-07-25T16:55:00Z", 4.2, "rain")
    row = db.get_latest_radar_observation()
    assert row["precip_rate_mmh"] == 4.2
    assert row["precip_type"] == "rain"


def test_purge_touches_only_high_volume_tables(db):
    db.insert_station_observation("2026-07-25T12:00:00Z", 20.0, 50.0, 1013.0)
    db.insert_forecast_snapshot("ch1", "2026-07-25T09:00:00Z", "2026-07-25T15:00:00Z", "temperature", 22.0)
    db.insert_radar_observation("2026-07-25T12:00:00Z", 1.0, None)
    db.insert_storm_prediction("2026-07-25T12:00:00Z", 0.3, {})
    key = BucketKey(hour_of_day=12, season="JJA", lead_time_bucket="short", source="ch1", measurement="temperature")
    db.upsert_bucket_stats(key, 0.1, 0.2, 1.0, 1, "2026-07-25T12:00:00Z")
    db.insert_storm_event("2026-07-25T12:00:00Z", None, 1.0, 1.0, 1.0)

    deleted = db.purge_older_than("2027-01-01T00:00:00Z")

    assert deleted["station_observations"] == 1
    assert deleted["forecast_snapshots"] == 1
    assert deleted["radar_observations"] == 1
    assert deleted["storm_predictions"] == 1
    # Never purged, regardless of cutoff:
    assert db.get_bucket_stats(key) is not None
    assert len(db.get_all_storm_events()) == 1
