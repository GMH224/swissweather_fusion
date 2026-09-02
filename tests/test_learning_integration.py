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

v0.1.23 fix (L-01/L-02, external ICS audit): this file's mirror of
_reconcile() was rewritten alongside the real one, replacing the
watermark-capping logic with the per-row reconciliation_status approach.
The watermark-specific regression tests below were replaced with direct
tests of the two failure modes the audit found (re-learning an
already-reconciled row; permanently missing a late-arriving row) — see
test_reconciliation_never_relearns_an_already_reconciled_row and
test_reconciliation_finds_a_late_arriving_row_even_after_later_rows_are_reconciled.
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


RETRY_GIVE_UP_AGE = timedelta(hours=48)
INITIAL_LOOKBACK = timedelta(days=14)


def _reconcile_once(db: SwissWeatherDB, *, now: datetime) -> int:
    """Mirrors ModelALearningCoordinator._reconcile's logic exactly
    (v0.1.23: status-based, no watermark) — see coordinator.py's
    _reconcile for the production code this mirrors and the storage
    layer's get_pending_forecast_snapshots()/mark_forecast_snapshots_status()
    docstrings for why this structurally prevents both L-01 (re-learning)
    and L-02 (permanently missed late arrivals).
    """
    measurements = ("temperature", "humidity", "pressure")
    until_iso = now.isoformat()

    pending_rows = db.get_pending_forecast_snapshots(
        until_ts=until_iso, measurements=measurements
    )
    if not pending_rows:
        return 0

    earliest_pending_valid_at = min(
        datetime.fromisoformat(r["valid_at"]) for r in pending_rows
    )
    tolerance = timedelta(minutes=model_a.RECONCILIATION_TOLERANCE_MINUTES)
    lookback_floor = now - INITIAL_LOOKBACK - tolerance
    station_query_start = max(earliest_pending_valid_at - tolerance, lookback_floor)
    station_rows = db.get_station_observations_between(
        station_query_start.isoformat(), (now + tolerance).isoformat()
    )
    candidates_by_measurement: dict = {"temperature": [], "humidity": [], "pressure": []}
    for row in station_rows:
        ts = datetime.fromisoformat(row["ts"])
        candidates_by_measurement["temperature"].append((ts, row["temperature"]))
        candidates_by_measurement["humidity"].append((ts, row["humidity"]))
        candidates_by_measurement["pressure"].append((ts, row["pressure"]))

    reconciled = 0
    reconciled_ids: list = []
    skipped_ids: list = []
    for fs_row in pending_rows:
        if fs_row["value"] is None:
            skipped_ids.append(fs_row["id"])
            continue
        measurement = fs_row["variable"]
        valid_at = datetime.fromisoformat(fs_row["valid_at"])
        issued_at = datetime.fromisoformat(fs_row["issued_at"])

        actual_value = model_a.find_nearest_observation(
            target=valid_at, candidates=candidates_by_measurement[measurement]
        )
        if actual_value is None:
            if (now - valid_at) >= RETRY_GIVE_UP_AGE:
                skipped_ids.append(fs_row["id"])
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
        reconciled_ids.append(fs_row["id"])

    db.mark_forecast_snapshots_status(reconciled_ids, "reconciled")
    db.mark_forecast_snapshots_status(skipped_ids, "skipped")
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

    reconciled = _reconcile_once(db, now=now)
    assert reconciled == 1

    key = BucketKey(
        hour_of_day=15, season="JJA", lead_time_bucket="short",
        source="ch1", measurement="temperature",
    )
    stats = db.get_bucket_stats(key)
    assert stats is not None
    assert stats.sample_count == 1
    assert stats.ema_bias == 2.0  # forecast (22.0) - actual (20.0)

    # The row is now 'reconciled', so a second identical run reconciles
    # zero additional rows rather than double-counting — this is the
    # per-row guarantee that replaces the old watermark's job.
    reconciled_again = _reconcile_once(db, now=now + timedelta(minutes=5))
    assert reconciled_again == 0
    stats_after = db.get_bucket_stats(key)
    assert stats_after.sample_count == 1  # unchanged — NOT re-learned


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

    reconciled = _reconcile_once(db, now=now)
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

    reconciled = _reconcile_once(db, now=now)
    assert reconciled == 0

    key = BucketKey(hour_of_day=15, season="JJA", lead_time_bucket="short", source="ch1", measurement="temperature")
    assert db.get_bucket_stats(key) is None


def test_reconciliation_retries_unmatched_row_on_next_pass(db):
    """A forecast row with no matching station observation yet must still
    be returned by get_pending_forecast_snapshots on the VERY NEXT pass —
    it stays 'pending' (not 'skipped') as long as it's younger than
    RETRY_GIVE_UP_AGE, and 'pending' rows are always reachable regardless
    of insertion order or how many other rows have since been reconciled.
    """
    issued_at = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    valid_at = datetime(2026, 7, 25, 15, 0, tzinfo=timezone.utc)
    db.insert_forecast_snapshot(
        "ch1", issued_at.isoformat(), valid_at.isoformat(), "temperature", 22.0
    )
    # No station observation yet — this row cannot be reconciled on pass 1.

    now_1 = valid_at + timedelta(minutes=10)
    reconciled_1 = _reconcile_once(db, now=now_1)
    assert reconciled_1 == 0

    # Pass 2: still no station data. The row must be returned again.
    now_2 = now_1 + timedelta(minutes=20)
    rows_pass_2 = db.get_pending_forecast_snapshots(
        until_ts=now_2.isoformat(),
        measurements=("temperature", "humidity", "pressure"),
    )
    assert len(rows_pass_2) == 1
    assert rows_pass_2[0]["valid_at"] == valid_at.isoformat()

    # Now the station observation finally arrives, and pass 2 actually
    # reconciles it — proving the retry path is not just "returned by the
    # query" but genuinely usable end to end.
    db.insert_station_observation(valid_at.isoformat(), 20.0, 55.0, 1013.0)
    reconciled_2 = _reconcile_once(db, now=now_2)
    assert reconciled_2 == 1

    key = BucketKey(
        hour_of_day=15, season="JJA", lead_time_bucket="short",
        source="ch1", measurement="temperature",
    )
    stats = db.get_bucket_stats(key)
    assert stats is not None
    assert stats.sample_count == 1


def test_reconciliation_gives_up_on_retry_after_max_age(db):
    """A row that never finds a matching station observation must
    eventually stop being retried once it's older than RETRY_GIVE_UP_AGE
    — otherwise a permanent station outage would leave it 'pending'
    forever, endlessly re-querying it every cycle for nothing.
    """
    issued_at = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    valid_at = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    db.insert_forecast_snapshot(
        "ch1", issued_at.isoformat(), valid_at.isoformat(), "temperature", 22.0
    )
    # No matching station observation ever arrives for this row.

    now_far_future = valid_at + RETRY_GIVE_UP_AGE + timedelta(hours=1)
    reconciled = _reconcile_once(db, now=now_far_future)
    assert reconciled == 0

    # Old enough now — the row is marked 'skipped' and stops being
    # selected, rather than remaining 'pending' forever.
    rows = db.get_pending_forecast_snapshots(
        until_ts=now_far_future.isoformat(),
        measurements=("temperature", "humidity", "pressure"),
    )
    assert rows == []


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

    _reconcile_once(db, now=now)

    key = BucketKey(hour_of_day=15, season="JJA", lead_time_bucket="short", source="ch1", measurement="temperature")
    stats = db.get_bucket_stats(key)
    assert stats.sample_count == 2
    # Moved from 4.0 toward 0.0, but didn't jump straight to 0.0 — genuine
    # EMA smoothing survived the round trip through real storage.
    assert 0.0 < stats.ema_bias < 4.0


# -- v0.1.23: direct regression tests for the audit's L-01 and L-02 -----


def test_reconciliation_never_relearns_an_already_reconciled_row(db):
    """Direct regression test for L-01 (CRITICAL): reproduces the exact
    scenario the audit described — a batch containing both a row that CAN
    be reconciled immediately and a row that CANNOT yet (still retryable)
    — and confirms the reconciled row is never re-selected or re-learned
    on a later pass, no matter how many times a neighboring row is still
    pending. This is precisely the mixed-batch shape the OLD watermark
    logic mishandled (capping the watermark to protect the retryable row
    made the already-reconciled row eligible again); none of the tests
    above exercise a MIXED batch like this one does, which is exactly how
    the original bug shipped unnoticed.
    """
    issued_at = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    valid_at_ok = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)  # will reconcile fine
    valid_at_retry = datetime(2026, 7, 25, 15, 0, tzinfo=timezone.utc)  # no station data yet

    db.insert_forecast_snapshot("ch1", issued_at.isoformat(), valid_at_ok.isoformat(), "temperature", 22.0)
    db.insert_station_observation(valid_at_ok.isoformat(), 20.0, 55.0, 1013.0)
    db.insert_forecast_snapshot("ch1", issued_at.isoformat(), valid_at_retry.isoformat(), "temperature", 21.0)
    # No station observation for valid_at_retry yet.

    now = valid_at_retry + timedelta(minutes=10)
    reconciled_1 = _reconcile_once(db, now=now)
    assert reconciled_1 == 1  # only the "ok" row

    key = BucketKey(hour_of_day=12, season="JJA", lead_time_bucket="short", source="ch1", measurement="temperature")
    stats_after_pass_1 = db.get_bucket_stats(key)
    assert stats_after_pass_1.sample_count == 1

    # Several more passes go by. The retryable row is still unmatched
    # every time, but MUST NOT cause the already-reconciled row to be
    # re-selected and folded into bucket_stats again.
    for i in range(1, 6):
        later_now = now + timedelta(minutes=20 * i)
        _reconcile_once(db, now=later_now)

    stats_final = db.get_bucket_stats(key)
    assert stats_final.sample_count == 1  # STILL 1 — never re-learned


def test_reconciliation_finds_a_late_arriving_row_even_after_later_rows_are_reconciled(db):
    """Direct regression test for L-02 (HIGH): a forecast snapshot for an
    EARLIER valid_at, inserted into the database only AFTER a LATER
    valid_at row has already been reconciled, must still be found and
    reconciled on the next pass — not silently and permanently invisible.
    This is exactly the late-ingestion scenario the old strict
    `valid_at > watermark` query could never satisfy once the watermark
    had advanced past the late row's valid_at.
    """
    issued_at = datetime(2026, 7, 25, 6, 0, tzinfo=timezone.utc)
    valid_at_early = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    valid_at_later = datetime(2026, 7, 25, 15, 0, tzinfo=timezone.utc)

    # Only the LATER row exists and is reconciled first.
    db.insert_forecast_snapshot("ch1", issued_at.isoformat(), valid_at_later.isoformat(), "temperature", 22.0)
    db.insert_station_observation(valid_at_later.isoformat(), 20.0, 55.0, 1013.0)
    now_1 = valid_at_later + timedelta(minutes=10)
    reconciled_1 = _reconcile_once(db, now=now_1)
    assert reconciled_1 == 1

    # NOW the earlier-valid_at row arrives late (e.g. a delayed ingest
    # path), well after the later row was already reconciled.
    db.insert_forecast_snapshot("ch1", issued_at.isoformat(), valid_at_early.isoformat(), "temperature", 19.0)
    db.insert_station_observation(valid_at_early.isoformat(), 18.0, 55.0, 1013.0)

    now_2 = now_1 + timedelta(minutes=20)
    reconciled_2 = _reconcile_once(db, now=now_2)
    assert reconciled_2 == 1  # the late-arriving early row IS found and reconciled

    key_early = BucketKey(hour_of_day=9, season="JJA", lead_time_bucket="short", source="ch1", measurement="temperature")
    assert db.get_bucket_stats(key_early) is not None
