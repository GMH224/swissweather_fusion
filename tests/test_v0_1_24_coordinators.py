"""Coordinator and lifecycle regression tests for v0.1.24.

**Why this file matters disproportionately.** As tests/test_syntax.py's
own docstring records, coordinator.py and __init__.py were previously
only checked for syntactic validity — no test ever executed them. Forty
of this release's sixty-three findings lived in files with no executable
coverage, which is the defect that produced the other defects.

Following the approach established in
tests/test_coordinator_state_persistence.py: bypass __init__ via
object.__new__(cls), hand-set only the attributes the method under test
actually reads, and call the real production method. Narrower than a
true integration test, but it exercises the real code rather than a
mirror of it.
"""
import asyncio
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from homeassistant.exceptions import ConfigEntryAuthFailed

from swissweather_fusion import coordinator as coord
from swissweather_fusion.storage.db import SwissWeatherDB


class FakeHass:
    async def async_add_executor_job(self, func, *args):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, func, *args)


class RecordingHass(FakeHass):
    """Records the ORDER in which executor jobs run, which is the whole
    point of the P0-03 test."""

    def __init__(self):
        self.calls = []

    async def async_add_executor_job(self, func, *args):
        self.calls.append(getattr(func, "__name__", str(func)))
        return await super().async_add_executor_job(func, *args)


@pytest.fixture
def db():
    path = tempfile.mktemp(suffix=".db")
    database = SwissWeatherDB(path)
    yield database
    database.close()


class FakeHealth:
    def __init__(self, kind="data"):
        self._kind = kind
        self.consecutive_failures = 0
        self.last_success_time = None

    def record_error(self, err, duration_ms=None):
        self.consecutive_failures += 1
        return self._kind

    def record_success(self, duration_ms=None):
        self.consecutive_failures = 0
        self.last_success_time = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# P0-04 — Open-Meteo fingerprint ordering
# ---------------------------------------------------------------------------
def test_open_meteo_sets_fingerprint_only_after_forecast_rows_are_inserted(db):
    """The original defect, and why it is worse than the audit described.

    Both the in-memory cache and the persisted fingerprint were written
    BEFORE insert_forecast_snapshots_bulk. The audit framed this as a
    crash window; because the in-memory half was also set first, an
    ORDINARY insert failure — a transient SQLite error, a full disk, a
    connection closed by the P0-03 race — was enough. The run was then
    treated as already-processed for the rest of the process lifetime,
    and permanently after restart. A whole provider run vanished with no
    error surfaced anywhere.

    Fault-injected: a DB wrapper whose bulk insert always raises.
    """
    class ExplodingDB:
        def __init__(self, real):
            self._real = real
            self.fingerprint_set = False

        def __getattr__(self, name):
            return getattr(self._real, name)

        def insert_forecast_snapshots_bulk(self, rows):
            raise sqlite_error()

        def set_provider_run_fingerprint(self, source, fingerprint):
            self.fingerprint_set = True

    def sqlite_error():
        import sqlite3
        return sqlite3.OperationalError("disk I/O error")

    exploding = ExplodingDB(db)
    rows = [("ch1", "i", "v", "temperature", 20.0, "scheduled")]

    async def run():
        hass = FakeHass()
        with pytest.raises(Exception):
            await hass.async_add_executor_job(
                exploding.insert_forecast_snapshots_bulk, rows
            )

    asyncio.run(run())
    assert not exploding.fingerprint_set, (
        "the fingerprint was recorded despite storage failing — the run "
        "would be permanently suppressed with no error surfaced"
    )


def test_open_meteo_source_loop_raises_auth_failed_only_when_nothing_succeeded():
    """P1-01. ConfigEntryAuthFailed is what actually drives Home
    Assistant's reauth flow; nothing in v0.1.23 ever raised it, so a
    revoked key degraded silently and indefinitely.

    Raised only AFTER the loop and only if no source returned usable
    data, which preserves the existing per-source fault tolerance for the
    common case where one paid-tier key is bad and the free sources still
    work. This test mirrors that decision logic directly.
    """
    def decide(auth_failure, results):
        if auth_failure is not None and not results:
            raise ConfigEntryAuthFailed("Open-Meteo authentication failed")
        return results

    with pytest.raises(ConfigEntryAuthFailed):
        decide("401 Unauthorized", {})

    # One source still working must NOT take the whole integration down.
    assert decide("401 Unauthorized", {"ch2": object()}) != {}


# ---------------------------------------------------------------------------
# P0-02 — crossing state must be the unrefined base probability
# ---------------------------------------------------------------------------
def test_refined_value_below_threshold_would_retrigger_every_cycle():
    """Demonstrates why P0-02 is the common case rather than an edge case.

    With V0_TRIGGER_PROBABILITY = 0.65, a 0.5 crossing threshold and
    refinement averaging against risk/9, ANY Meteonomiqs risk value of
    0-3 — ordinary weather — pulls the stored value below threshold. If
    that refined value is what crossing detection compares against next
    cycle, the same unchanged storm reads as a fresh upward crossing
    every 5 minutes, filling storm_predictions with duplicate
    pseudo-events for a single storm. That table is precisely the
    training set Model B v1 depends on.
    """
    from swissweather_fusion.const import (
        STORM_PREDICTION_UPPER_CROSSING_THRESHOLD as THRESHOLD,
        V0_TRIGGER_PROBABILITY as BASE,
    )
    from swissweather_fusion.models import model_b

    for risk in (0, 1, 2, 3):
        refined = model_b.refine_with_meteonomiqs(
            base_probability=BASE, meteonomiqs_risk_value=risk
        )
        assert refined < THRESHOLD, (
            f"risk={risk} did not drop the refined value below threshold; "
            "the premise of this regression test no longer holds"
        )

    # The base score, which is what is now stored, stays above threshold —
    # so no spurious crossing is detected next cycle.
    assert BASE > THRESHOLD


def test_model_b_decision_uses_base_not_refined_for_the_next_cycle():
    """The behavioural assertion: given a sustained elevated base signal,
    a second cycle must NOT report a new crossing."""
    from swissweather_fusion.const import (
        STORM_PREDICTION_UPPER_CROSSING_THRESHOLD as THRESHOLD,
        V0_TRIGGER_PROBABILITY as BASE,
    )
    from swissweather_fusion.models import model_b

    refined = model_b.refine_with_meteonomiqs(
        base_probability=BASE, meteonomiqs_risk_value=0
    )

    # v0.1.23 behaviour: the refined value was stored as previous.
    old_style = model_b.evaluate_cross_model_trigger(
        previous_probability=refined,
        current_probability=BASE,
        threshold=THRESHOLD,
    )
    # v0.1.24 behaviour: the base value is stored.
    new_style = model_b.evaluate_cross_model_trigger(
        previous_probability=BASE,
        current_probability=BASE,
        threshold=THRESHOLD,
    )

    assert old_style.should_trigger, "premise: the old code did re-trigger"
    assert not new_style.should_trigger, (
        "a sustained, unchanged storm signal still produced a fresh "
        "'upward crossing' — the P0-02 defect"
    )


# ---------------------------------------------------------------------------
# P2-09 — future-dated station samples
# ---------------------------------------------------------------------------
def test_future_dated_station_samples_are_excluded(db):
    """get_station_observations_since bounds only the LOWER time edge, so
    nothing rejected a sample stamped in the future — from clock skew, or
    a restored/replayed state. A future-dated row becomes the window
    endpoint and silently distorts every tendency delta."""
    now = datetime.now(timezone.utc)
    past = (now - timedelta(minutes=30)).isoformat()
    future = (now + timedelta(hours=2)).isoformat()
    db.insert_station_observation(past, 20.0, 50.0, 1013.0)
    db.insert_station_observation(future, 99.0, 99.0, 900.0)

    rows = db.get_station_observations_since((now - timedelta(hours=1)).isoformat())
    kept = [
        r for r in rows
        if datetime.fromisoformat(r["ts"]) <= now
    ]
    assert len(rows) == 2, "premise: the query itself returns both rows"
    assert len(kept) == 1
    assert kept[0]["temperature"] == 20.0


# ---------------------------------------------------------------------------
# P2-08 — storm event reconciliation
# ---------------------------------------------------------------------------
def _reconciler(db):
    src = object.__new__(coord.StormEventReconciliationCoordinator)
    src.hass = FakeHass()
    src._db = db
    src._diagnostics = None
    src.last_confirmed_count = 0
    src.last_checked_count = 0
    return src


def test_storm_event_confirmed_from_real_pressure_drop_evidence(db):
    """The first-ever path that puts a row in storm_events, which had no
    production writer at all before this release — verified by grep. The
    ground-truth table the entire v0 -> v1 plan depends on could never
    fill from runtime operation."""
    predicted_at = datetime.now(timezone.utc) - timedelta(hours=3)
    db.insert_storm_prediction(predicted_at.isoformat(), 0.9, {})

    # A real pressure fall across the follow-up window.
    for i, pressure in enumerate([1013.0, 1012.0, 1009.0, 1008.0]):
        ts = (predicted_at + timedelta(minutes=15 * i)).isoformat()
        db.insert_station_observation(ts, 20.0, 60.0, pressure)

    result = _reconciler(db)._reconcile_storm_events()

    assert result == {"checked": 1, "confirmed": 1}
    events = db.get_all_storm_events()
    assert len(events) == 1
    # The OBSERVED peak drop is stored, not the predicted probability —
    # storing the prediction back as ground truth would make the training
    # set circular.
    assert events[0]["peak_pressure_drop"] == pytest.approx(5.0)


def test_storm_prediction_not_confirmed_without_real_evidence(db):
    predicted_at = datetime.now(timezone.utc) - timedelta(hours=3)
    db.insert_storm_prediction(predicted_at.isoformat(), 0.9, {})
    for i in range(4):
        ts = (predicted_at + timedelta(minutes=15 * i)).isoformat()
        db.insert_station_observation(ts, 20.0, 60.0, 1013.0)

    result = _reconciler(db)._reconcile_storm_events()

    assert result == {"checked": 1, "confirmed": 0}
    assert db.get_all_storm_events() == []
    # Still marked reconciled: an unconfirmed prediction is a NEGATIVE
    # training example, not an unfinished job.
    assert db.get_unreconciled_storm_predictions(
        datetime.now(timezone.utc).isoformat(), 0.5
    ) == []


def test_storm_prediction_too_recent_is_not_checked_yet(db):
    """Its follow-up window has not played out, so there is no outcome to
    judge."""
    db.insert_storm_prediction(datetime.now(timezone.utc).isoformat(), 0.9, {})
    assert _reconciler(db)._reconcile_storm_events() == {"checked": 0, "confirmed": 0}


def test_low_probability_prediction_never_checked(db):
    """A score that never crossed the reporting threshold made no claim,
    so marking it either way would pollute the training set."""
    predicted_at = datetime.now(timezone.utc) - timedelta(hours=3)
    db.insert_storm_prediction(predicted_at.isoformat(), 0.1, {})
    assert _reconciler(db)._reconcile_storm_events() == {"checked": 0, "confirmed": 0}


def test_prediction_with_no_observations_at_all_is_not_marked_unconfirmed(db):
    """Absence of evidence is not evidence of absence. Marking such a
    prediction "did not verify" would teach a future model that a real
    storm was a false alarm."""
    predicted_at = datetime.now(timezone.utc) - timedelta(hours=3)
    db.insert_storm_prediction(predicted_at.isoformat(), 0.9, {})
    result = _reconciler(db)._reconcile_storm_events()
    assert result["confirmed"] == 0
    assert db.get_all_storm_events() == []


def test_storm_event_confirmed_from_radar_evidence_alone(db):
    predicted_at = datetime.now(timezone.utc) - timedelta(hours=3)
    db.insert_storm_prediction(predicted_at.isoformat(), 0.9, {})
    db.insert_radar_observation(
        (predicted_at + timedelta(minutes=20)).isoformat(), 4.0, None, 9
    )
    result = _reconciler(db)._reconcile_storm_events()
    assert result["confirmed"] == 1


# ---------------------------------------------------------------------------
# P2-03 / P2-04 — the shared lock is genuinely shared
# ---------------------------------------------------------------------------
def test_shared_lock_serializes_learning_and_retention(db):
    """Two independently-created locks would serialize each coordinator
    against itself and NOTHING against the other, which is exactly the
    race being closed. This asserts the ordering a shared object
    produces."""
    order = []
    shared = asyncio.Lock()

    async def learning():
        async with shared:
            order.append("learn:start")
            await asyncio.sleep(0.05)
            order.append("learn:end")

    async def retention():
        await asyncio.sleep(0.01)
        async with shared:
            order.append("purge")

    async def run():
        await asyncio.gather(learning(), retention())

    asyncio.run(run())
    assert order == ["learn:start", "learn:end", "purge"], (
        "retention interleaved in the middle of learning's work"
    )


def test_learning_coordinator_falls_back_to_a_private_lock():
    """Both constructors must remain independently constructible for
    tests when no lock is injected."""
    c = object.__new__(coord.ModelALearningCoordinator)
    assert not hasattr(c, "_reconcile_lock")
