"""Tests for v0.2.4 — learning-model improvements.

Three changes, each answering a question the previous design could not:

* **SWF-024-001** — is fusion actually better than its best input?
* **SWF-024-002** — is the station itself trustworthy, for every
  measurement rather than only pressure?
* **SWF-024-003** — should a stale model run count as much as a fresh one?
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from swissweather_fusion import coordinator as coord
from swissweather_fusion.const import (
    BLEND_VERIFICATION_LEAD_HOURS,
    FRESHNESS_MAX_BOOST,
    FRESHNESS_MIN_FACTOR,
    FRESHNESS_OVERDUE_FLOOR,
    FRESHNESS_OVERDUE_MULTIPLE,
    SOURCE_BLEND,
    SOURCE_UPDATE_CADENCE,
    STATION_REFERENCE_TOLERANCES,
    ALL_FORECAST_SOURCES,
)
from swissweather_fusion.models.model_a import freshness_factor
from swissweather_fusion.storage.db import SwissWeatherDB

_KW = dict(
    max_boost=FRESHNESS_MAX_BOOST,
    min_factor=FRESHNESS_MIN_FACTOR,
    overdue_multiple=FRESHNESS_OVERDUE_MULTIPLE,
    overdue_floor=FRESHNESS_OVERDUE_FLOOR,
)


class FakeHass:
    def __init__(self, states=None):
        self.data = {}
        self._states = states or {}
        outer = self

        class States:
            def get(self, entity_id):
                return outer._states.get(entity_id)

        self.states = States()

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class FakeState:
    def __init__(self, state, unit=None):
        self.state = state
        self.attributes = {"unit_of_measurement": unit} if unit else {}


@pytest.fixture
def db(tmp_path):
    database = SwissWeatherDB(str(tmp_path / "v024.db"))
    yield database
    database.close()


# ---------------------------------------------------------------------------
# SWF-024-003 — freshness weighting
# ---------------------------------------------------------------------------
def test_factor_is_one_at_the_mean_run_age():
    """The whole design rests on this.

    ema_abs_error is learned from samples spread across the source's
    cadence, so the AVERAGE staleness penalty is already inside the
    learned weight. A curve that only ever reduced the weight would
    penalise the same staleness twice. Centring on the mean age means
    only DEVIATIONS from normal staleness are corrected.
    """
    cadence = timedelta(hours=3)
    assert freshness_factor(timedelta(hours=1.5), cadence, **_KW) == pytest.approx(1.0)


def test_fresher_than_average_is_boosted_and_staler_is_penalised():
    cadence = timedelta(hours=3)
    fresh = freshness_factor(timedelta(hours=0), cadence, **_KW)
    stale = freshness_factor(timedelta(hours=3), cadence, **_KW)
    assert fresh > 1.0
    assert stale < 1.0
    assert fresh <= FRESHNESS_MAX_BOOST
    assert stale >= FRESHNESS_MIN_FACTOR


def test_mean_factor_over_a_cycle_is_close_to_one():
    """The no-double-counting property, stated numerically: averaged over
    a full cadence the adjustment is neutral, so the learned weight keeps
    its meaning."""
    cadence = timedelta(hours=6)
    samples = [
        freshness_factor(timedelta(hours=h * 0.1), cadence, **_KW)
        for h in range(61)
    ]
    assert sum(samples) / len(samples) == pytest.approx(1.0, abs=0.05)


def test_an_overdue_source_decays_below_the_symmetric_floor():
    """Past a couple of cadences a source is not ageing, it is failing,
    and the historical average does not cover that case."""
    cadence = timedelta(hours=3)
    overdue = freshness_factor(timedelta(hours=9), cadence, **_KW)
    assert overdue < FRESHNESS_MIN_FACTOR
    assert overdue >= FRESHNESS_OVERDUE_FLOOR


def test_unknown_age_or_cadence_makes_no_adjustment():
    """A missing signal must never silently reweight anything."""
    assert freshness_factor(None, timedelta(hours=3), **_KW) == 1.0
    assert freshness_factor(timedelta(hours=1), None, **_KW) == 1.0


def test_every_blend_source_has_a_declared_cadence():
    """A source without a cadence gets no adjustment at all, which would
    be a silent exemption rather than a decision."""
    missing = set(ALL_FORECAST_SOURCES) - set(SOURCE_UPDATE_CADENCE)
    assert not missing, f"no declared cadence for: {sorted(missing)}"


def test_meteoblue_cadence_reflects_our_polling_not_its_model():
    """meteoblue computes twice daily, but OUR staleness is set by the
    3-calls-per-day credit budget. The honest denominator is the age of
    the data we actually hold."""
    assert SOURCE_UPDATE_CADENCE["meteoblue"] >= timedelta(hours=8)


def test_freshness_is_applied_to_learned_weights_only():
    """A cold-start source has no learned weight to modulate, and its
    neutral weight is defined relative to the trusted set (IND-01).
    Scaling it would break that relationship."""
    import inspect

    source = inspect.getsource(coord.ModelABlendCoordinator._blend_at)
    cold_start = source[source.index("if bucket is None"):source.index("else:")]
    assert "freshness" not in cold_start


# ---------------------------------------------------------------------------
# SWF-024-001 — is fusion better than its best input?
# ---------------------------------------------------------------------------
def test_blend_is_not_one_of_its_own_inputs():
    """Including the blend in ALL_FORECAST_SOURCES would feed it its own
    output and make the fusion self-referential."""
    assert SOURCE_BLEND not in ALL_FORECAST_SOURCES


def test_blend_records_itself_for_verification(db):
    """Without this, bucket_stats measures how wrong each PROVIDER is and
    nothing measures the blended answer the user actually sees."""
    hass = FakeHass()
    c = coord.ModelABlendCoordinator(hass, db)

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    for source in ("ch1", "ch2"):
        for lead in BLEND_VERIFICATION_LEAD_HOURS:
            db.insert_forecast_snapshot(
                source, now.isoformat(),
                (now + timedelta(hours=lead)).isoformat(),
                "temperature", 20.0,
            )

    asyncio.run(c._async_update_data())

    rows = db._conn.execute(
        "SELECT * FROM forecast_snapshots WHERE source = ?", (SOURCE_BLEND,)
    ).fetchall()
    assert rows, "the blend never recorded its own output"
    assert {r["trigger_reason"] for r in rows} == {"blend_verification"}


def test_blend_rows_are_reconcilable_like_any_other_source(db):
    """They must be picked up by the normal learning loop — a pseudo-
    source that is written but never reconciled would answer nothing."""
    now = datetime.now(timezone.utc) - timedelta(hours=1)
    db.insert_forecast_snapshot(
        SOURCE_BLEND, now.isoformat(), now.isoformat(), "temperature", 20.0
    )
    pending = db.get_pending_forecast_snapshots(
        until_ts=datetime.now(timezone.utc).isoformat(),
        measurements=("temperature", "humidity", "pressure"),
    )
    assert any(r["source"] == SOURCE_BLEND for r in pending)


def test_blend_output_is_never_fed_back_into_the_blend(db):
    """The self-reference guard, asserted behaviourally."""
    hass = FakeHass()
    c = coord.ModelABlendCoordinator(hass, db)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    target_iso = now.isoformat()
    latest = {
        (SOURCE_BLEND, "temperature", target_iso): (99.0, now),
        ("ch1", "temperature", target_iso): (20.0, now),
    }
    result = c._blend_at(
        "temperature", now, latest_forecast=latest, bucket_lookup={}
    )
    assert result == pytest.approx(20.0), "the blend consumed its own output"


def test_accuracy_reports_whether_the_blend_beats_the_best_source(db):
    """The falsifiability scoreboard. If blend_mae does not undercut
    best_source_mae, the learned bias correction is not earning its
    complexity — and that is worth knowing."""
    from swissweather_fusion.storage.db import BucketKey

    def seed(source, err):
        key = BucketKey(
            hour_of_day=12, season="summer", lead_time_bucket="short",
            source=source, measurement="temperature",
        )
        db.apply_reconciliation_batch(
            [(key, 0.0, err, 1.0, 50, "2026-09-02T12:00:00+00:00")], [], []
        )

    seed("ch1", 1.2)
    seed("ch2", 0.9)
    seed(SOURCE_BLEND, 0.6)

    hass = FakeHass()
    learning = coord.ModelALearningCoordinator(hass, db, reconcile_lock=asyncio.Lock())
    mae = learning._compute_temperature_mae()

    assert mae["blend_mae"] == pytest.approx(0.6)
    assert mae["best_source"] == "ch2"
    assert mae["best_source_mae"] == pytest.approx(0.9)
    assert mae["blend_beats_best_source"] is True


def test_accuracy_reports_honestly_when_the_blend_loses(db):
    """The result must be reported either way. A scoreboard that can only
    show a win is not a scoreboard."""
    from swissweather_fusion.storage.db import BucketKey

    for source, err in (("ch1", 0.5), (SOURCE_BLEND, 1.4)):
        key = BucketKey(
            hour_of_day=12, season="summer", lead_time_bucket="short",
            source=source, measurement="temperature",
        )
        db.apply_reconciliation_batch(
            [(key, 0.0, err, 1.0, 50, "2026-09-02T12:00:00+00:00")], [], []
        )

    learning = coord.ModelALearningCoordinator(
        FakeHass(), db, reconcile_lock=asyncio.Lock()
    )
    assert learning._compute_temperature_mae()["blend_beats_best_source"] is False


# ---------------------------------------------------------------------------
# SWF-024-002 — station cross-check for every measurement
# ---------------------------------------------------------------------------
def _seed_reference(db, variable, value):
    hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    for source in ("ch1", "ch2", "icon_d2"):
        db.insert_forecast_snapshot(
            source, "i", f"{hour}:00:00+00:00", variable, value
        )


def test_every_learned_measurement_has_a_tolerance():
    """Pressure got a cross-check in v0.2.3 only because it was the
    measurement that happened to break first. Temperature and humidity
    are learned from the same single station with the same total trust."""
    assert set(STATION_REFERENCE_TOLERANCES) == {
        "temperature", "humidity", "pressure"
    }


def test_grossly_wrong_temperature_is_rejected(db):
    """An undeclared Fahrenheit sensor reading 68 against a forecast of
    20 is a 48 K disagreement — a configuration error, not weather."""
    _seed_reference(db, "temperature", 20.0)
    hass = FakeHass({
        "sensor.t": FakeState("68.0"),
        "sensor.h": FakeState("50.0", "%"),
        "sensor.p": FakeState("1013.0", "hPa"),
    })
    c = coord.StationCoordinator(
        hass, db, "sensor.t", "sensor.h", "sensor.p",
        pressure_is_sea_level=True, elevation_m=540.0,
    )
    result = asyncio.run(c._async_update_data())
    assert result["temperature"] is None
    assert result["humidity"] == pytest.approx(50.0), "other measurements survived"


def test_genuine_microclimate_difference_is_kept(db):
    """A thermometer above a patio legitimately reads several degrees
    above a 1 km grid cell. That difference is real signal the learning
    SHOULD absorb — rejecting it would destroy the thing Model A exists
    to learn."""
    _seed_reference(db, "temperature", 20.0)
    hass = FakeHass({
        "sensor.t": FakeState("26.0"),
        "sensor.h": FakeState("50.0", "%"),
        "sensor.p": FakeState("1013.0", "hPa"),
    })
    c = coord.StationCoordinator(
        hass, db, "sensor.t", "sensor.h", "sensor.p",
        pressure_is_sea_level=True, elevation_m=540.0,
    )
    result = asyncio.run(c._async_update_data())
    assert result["temperature"] == pytest.approx(26.0)
    assert c.reference_deltas["temperature"] == pytest.approx(6.0)


def test_stuck_humidity_element_is_rejected(db):
    _seed_reference(db, "humidity", 60.0)
    hass = FakeHass({
        "sensor.t": FakeState("20.0"),
        "sensor.h": FakeState("0.0", "%"),
        "sensor.p": FakeState("1013.0", "hPa"),
    })
    c = coord.StationCoordinator(
        hass, db, "sensor.t", "sensor.h", "sensor.p",
        pressure_is_sea_level=True, elevation_m=540.0,
    )
    assert asyncio.run(c._async_update_data())["humidity"] is None


def test_deltas_are_recorded_even_when_within_tolerance(db):
    """Reject only the implausible, but EXPOSE always — so slow drift
    stays visible even though it is not rejected."""
    _seed_reference(db, "temperature", 20.0)
    _seed_reference(db, "humidity", 55.0)
    hass = FakeHass({
        "sensor.t": FakeState("22.0"),
        "sensor.h": FakeState("58.0", "%"),
        "sensor.p": FakeState("1013.0", "hPa"),
    })
    c = coord.StationCoordinator(
        hass, db, "sensor.t", "sensor.h", "sensor.p",
        pressure_is_sea_level=True, elevation_m=540.0,
    )
    asyncio.run(c._async_update_data())
    assert c.reference_deltas["temperature"] == pytest.approx(2.0)
    assert c.reference_deltas["humidity"] == pytest.approx(3.0)


def test_reference_excludes_the_blend_pseudo_source(db):
    """The blend is derived from the providers, so including it would
    weight their consensus toward itself."""
    hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    db.insert_forecast_snapshot("ch1", "i", f"{hour}:00:00+00:00", "temperature", 20.0)
    db.insert_forecast_snapshot(
        SOURCE_BLEND, "i", f"{hour}:00:00+00:00", "temperature", 99.0
    )
    assert db.get_reference_value("temperature", hour) == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# SWF-024-004 — regression found while writing the tests above
# ---------------------------------------------------------------------------
def test_aggregation_survives_an_hour_with_no_precipitation_value():
    """A production crash, found by the blend-verification test.

    v0.2.1 rewrote the hourly forecast builder to strip keys whose value
    is None, so optional parameters do not surface as nulls in the
    Forecast dict. That silently made the aggregations' direct
    `e["native_precipitation"]` lookups unsafe: an hour with no
    precipitation value has no such KEY, and the lookup raised KeyError —
    taking down the whole blend cycle rather than that one hour.

    Nothing caught it because every existing aggregation test builds
    complete entries.
    """
    from swissweather_fusion.models import model_a

    base = datetime(2026, 9, 2, tzinfo=timezone.utc)
    entries = [
        # A realistic sparse entry: the builder omitted the keys entirely.
        {"datetime": (base + timedelta(hours=h)).isoformat(),
         "native_temperature": 18.0}
        for h in range(24)
    ]
    days = model_a.aggregate_daily_forecast(entries, local_tz=timezone.utc)
    assert days, "aggregation produced nothing"

    periods = model_a.aggregate_twice_daily_forecast(entries, local_tz=timezone.utc)
    assert periods


def test_aggregation_survives_entries_missing_temperature_too():
    from swissweather_fusion.models import model_a

    base = datetime(2026, 9, 2, tzinfo=timezone.utc)
    entries = [
        {"datetime": (base + timedelta(hours=h)).isoformat()} for h in range(24)
    ]
    assert model_a.aggregate_daily_forecast(entries, local_tz=timezone.utc) is not None


# ---------------------------------------------------------------------------
# SWF-024-005 — learning progress must be chartable, not just inspectable
# ---------------------------------------------------------------------------
def _seed(db, source, measurement, err, samples):
    from swissweather_fusion.storage.db import BucketKey

    key = BucketKey(
        hour_of_day=12, season="summer", lead_time_bucket="short",
        source=source, measurement=measurement,
    )
    db.apply_reconciliation_batch(
        [(key, 0.0, err, 1.0, samples, "2026-09-02T12:00:00+00:00")], [], []
    )


def _learning(db):
    return coord.ModelALearningCoordinator(
        FakeHass(), db, reconcile_lock=asyncio.Lock()
    )


def test_learning_metrics_are_sensor_states_not_attributes():
    """Home Assistant records long-term statistics for a sensor's STATE
    and never for its attributes.

    v0.2.4 first put blend_mae and the sample counts on
    ForecastAccuracySensor as attributes — where today's value is visible
    but the TREND, which is the whole question, cannot be charted. Each
    now has its own state.
    """
    from swissweather_fusion.sensor import (
        BestSourceAccuracySensor,
        BlendAccuracySensor,
        LearningProgressSensor,
        TrustedBucketCountSensor,
    )

    for cls in (
        BlendAccuracySensor, BestSourceAccuracySensor,
        LearningProgressSensor, TrustedBucketCountSensor,
    ):
        assert cls._attr_state_class == "measurement", (
            f"{cls.__name__} is not chartable in long-term statistics"
        )


def test_progress_counts_only_buckets_past_the_trust_threshold(db):
    """Below MIN_SAMPLES_TO_TRUST_BUCKET a bucket contributes at the
    cold-start weight, so bias correction is doing nothing for it. It is
    not progress."""
    from swissweather_fusion.const import MIN_SAMPLES_TO_TRUST_BUCKET

    _seed(db, "ch1", "temperature", 1.0, MIN_SAMPLES_TO_TRUST_BUCKET)
    _seed(db, "ch2", "temperature", 1.0, MIN_SAMPLES_TO_TRUST_BUCKET - 1)

    progress = _learning(db)._compute_learning_progress()
    assert progress["buckets_total"] == 2
    assert progress["buckets_trusted"] == 1
    assert progress["trusted_pct"] == pytest.approx(50.0)


def test_progress_is_zero_and_safe_on_a_fresh_database(db):
    progress = _learning(db)._compute_learning_progress()
    assert progress["buckets_total"] == 0
    assert progress["trusted_pct"] == 0.0


def test_blend_and_best_source_are_separate_chartable_states(db):
    """Two lines on one chart answer "is fusion worth it" at a glance."""
    from swissweather_fusion.sensor import (
        BestSourceAccuracySensor,
        BlendAccuracySensor,
    )

    _seed(db, "ch1", "temperature", 1.2, 50)
    _seed(db, SOURCE_BLEND, "temperature", 0.6, 50)

    learning = _learning(db)
    learning.temperature_mae = learning._compute_temperature_mae()
    runtime = {"learning_coordinator": learning}

    blend = object.__new__(BlendAccuracySensor)
    blend._runtime = runtime
    best = object.__new__(BestSourceAccuracySensor)
    best._runtime = runtime

    assert BlendAccuracySensor.native_value.fget(blend) == pytest.approx(0.6)
    assert BestSourceAccuracySensor.native_value.fget(best) == pytest.approx(1.2)


def test_learning_sensors_are_blank_before_the_first_reconciliation():
    """They must not fabricate a value — a blank sensor is the honest
    state before anything has been learned."""
    from swissweather_fusion.sensor import BlendAccuracySensor, LearningProgressSensor

    for cls in (BlendAccuracySensor, LearningProgressSensor):
        sensor = object.__new__(cls)
        sensor._runtime = {}
        assert cls.native_value.fget(sensor) is None
