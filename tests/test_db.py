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


def test_reconciliation_watermark_roundtrip(db):
    assert db.get_reconciliation_watermark() is None
    db.set_reconciliation_watermark("2026-07-25T12:00:00+00:00")
    assert db.get_reconciliation_watermark() == "2026-07-25T12:00:00+00:00"
    # Updating again should overwrite, not create a second row/conflict.
    db.set_reconciliation_watermark("2026-07-25T13:00:00+00:00")
    assert db.get_reconciliation_watermark() == "2026-07-25T13:00:00+00:00"


def test_get_forecast_snapshots_to_reconcile_filters_by_measurement_and_window(db):
    db.insert_forecast_snapshot("ch1", "2026-07-25T09:00:00+00:00", "2026-07-25T12:00:00+00:00", "temperature", 22.0)
    db.insert_forecast_snapshot("ch1", "2026-07-25T09:00:00+00:00", "2026-07-25T12:00:00+00:00", "precip", 0.0)
    db.insert_forecast_snapshot("ch2", "2026-07-25T06:00:00+00:00", "2026-07-25T12:30:00+00:00", "humidity", 55.0)
    db.insert_forecast_snapshot("ch1", "2026-07-25T09:00:00+00:00", "2026-07-26T12:00:00+00:00", "temperature", 20.0)

    rows = db.get_forecast_snapshots_to_reconcile(
        since_ts="2026-07-25T00:00:00+00:00",
        until_ts="2026-07-25T13:00:00+00:00",
        measurements=("temperature", "humidity", "pressure"),
    )
    # precip is excluded (no station ground truth for it), and the row
    # valid_at 2026-07-26 is outside the until_ts window.
    variables = sorted(r["variable"] for r in rows)
    assert variables == ["humidity", "temperature"]
