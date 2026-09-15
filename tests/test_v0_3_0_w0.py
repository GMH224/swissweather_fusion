"""Tests for v0.3.0 (W0) — making the central claim measurable.

v0.3.0 adds no forecasting capability. It repairs the instrument that was
supposed to answer the one question this project exists to answer: does
blending five sources beat simply using the best one?

* **ARC-05** — the old comparison could report a win while the shipped
  forecast was worse than its best input. The first test in this file
  constructs exactly that case.
* **ARC-04** — lead time was measured from when this integration first
  SAW a run, not from when the provider initialised it.
* **v0.3.0 regression** — adding two columns to the forecast row would
  have silently disabled physical-bounds validation for three of five
  sources.

Every fixture below that claims to be a provider response is a verbatim
capture, per DEVELOPER.md §7.3.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from swissweather_fusion import coordinator as coord
from swissweather_fusion import provider_validation
from swissweather_fusion.clients.open_meteo import (
    MODEL_PARAM,
    build_metadata_url,
    parse_metadata_response,
)
from swissweather_fusion.const import (
    ALL_FORECAST_SOURCES,
    BLEND_COMPARISON_LEAD_HOURS,
    LEAD_TIME_BASIS_POLL,
    LEAD_TIME_BASIS_RUN,
    MIN_SAMPLES_TO_TRUST_BUCKET,
    OPEN_METEO_METADATA_MODELS,
    SOURCE_UPDATE_CADENCE,
)
from swissweather_fusion.models import model_a
from swissweather_fusion.models.comparison import build_report, verdict
from swissweather_fusion.storage import db as db_module
from swissweather_fusion.storage.db import SwissWeatherDB

# ---------------------------------------------------------------------------
# Real captures — Open-Meteo metadata endpoint, 2026-09-10
# ---------------------------------------------------------------------------
# Captured from:
#   https://api.open-meteo.com/data/meteoswiss_icon_ch1/static/meta.json
#   https://api.open-meteo.com/data/dwd_icon_d2/static/meta.json
#
# Two different PROVIDERS deliberately. One capture could not distinguish
# "this path is the pattern" from "this path happens to work for
# MeteoSwiss", and inventing a plausible Open-Meteo path without checking
# is the v0.1.1 defect this project already paid for once.
#
# crs_wkt is omitted from both: it is several hundred characters of
# projection definition that no code here reads.
CH1_METADATA = {
    "chunk_time_length": 48,
    "data_end_time": 1789174800,
    "last_run_availability_time": 1789059122,
    "last_run_initialisation_time": 1789052400,
    "last_run_modification_time": 1789059113,
    "temporal_resolution_seconds": 3600,
    "update_interval_seconds": 10800,
}
D2_METADATA = {
    "chunk_time_length": 121,
    "data_end_time": 1789228800,
    "last_run_availability_time": 1789057769,
    "last_run_initialisation_time": 1789052400,
    "last_run_modification_time": 1789057441,
    "temporal_resolution_seconds": 3600,
    "update_interval_seconds": 10800,
}


class FakeHass:
    def __init__(self):
        self.data = {}

        class States:
            def get(self, entity_id):
                return None

        self.states = States()

    async def async_add_executor_job(self, func, *args):
        return func(*args)


@pytest.fixture
def db(tmp_path):
    database = SwissWeatherDB(str(tmp_path / "v030.db"))
    yield database
    database.close()


def _row(valid_at, measurement, lead, blend_value, contributors,
         actual, basis=LEAD_TIME_BASIS_POLL):
    """A comparison row shaped like the sqlite3.Row the report consumes."""
    return {
        "valid_at": valid_at,
        "measurement": measurement,
        "lead_hours": lead,
        "blend_value": blend_value,
        "contributors": json.dumps(contributors),
        "actual_value": actual,
        "lead_time_basis": basis,
    }


# ---------------------------------------------------------------------------
# ARC-05 — the regression this release exists for
# ---------------------------------------------------------------------------
def _legacy_verdict(bucket_stats):
    """v0.2.8's comparison, reproduced exactly.

    `bucket_stats` maps source -> list of (ema_abs_error, sample_count),
    one entry per lead-time bucket, mirroring what
    _compute_temperature_mae read from the table. The logic below is a
    transcription of that method as it stood in v0.2.8: aggregate each
    source over its OWN buckets, weighted by sample count, then compare
    the blend's aggregate against the lowest provider aggregate.

    Reproduced here rather than imported because v0.3.0 deletes it. A
    regression test for removed logic has to carry its own copy of that
    logic, or it is asserting nothing.
    """
    aggregates = {}
    for source, buckets in bucket_stats.items():
        weighted = sum(err * n for err, n in buckets)
        samples = sum(n for _, n in buckets)
        if samples:
            aggregates[source] = weighted / samples
    blend = aggregates.pop("blend", None)
    if blend is None or not aggregates:
        return None
    return blend < min(aggregates.values())


def test_the_old_unpaired_comparison_can_report_a_win_when_the_blend_actually_loses():
    """**The ARC-05 regression. This is the reason v0.3.0 exists.**

    The scenario is not contrived — it is the normal operating state of
    v0.2.8, which is what makes it serious:

    * the blend recorded itself at six lead offsets, the longest 48h, so
      its bucket_stats rows sat only in the `short` and `medium` buckets;
    * a provider recorded every reconcilable hour of every run, so its
      rows spanned `short`, `medium` AND `long`;
    * forecast error grows with lead time, so the provider's aggregate
      carried its long-range error while the blend's did not.

    Below, ch1 is BETTER than the blend at every horizon they share — 0.4
    against 0.5 at short lead. On the identical targets where both
    produced a value, using ch1 alone would have been closer to the
    truth. The old comparison reports the blend winning anyway, because
    it is averaging ch1's 2.0 long-lead error into the number it calls
    "ch1's accuracy" and comparing that against a blend figure measured
    only where forecasting is easy.

    The paired report reaches the opposite, correct conclusion from the
    same underlying forecasts.
    """
    # Old scheme: the blend occupies short+medium only; ch1 spans all
    # three buckets, including the long one it is scored on and the
    # blend never is.
    legacy = _legacy_verdict({
        "blend": [(0.5, 100), (0.8, 100)],
        "ch1": [(0.4, 100), (0.7, 100), (2.0, 100)],
    })
    assert legacy is True, (
        "this test asserts the OLD logic was wrong; if the old logic no "
        "longer reports a win here, the premise needs rechecking"
    )

    # Same forecasts, paired. Every row is one target hour on which both
    # the blend and ch1 spoke, so there is no lead-distribution
    # asymmetry left to hide in.
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(60):
        valid_at = (base + timedelta(hours=i)).isoformat()
        # truth 10.0; blend off by 0.5, ch1 off by 0.4
        rows.append(_row(
            valid_at, "temperature", 1, 10.5,
            {"ch1": {"raw": 10.4, "debiased": 10.4, "samples": 99}},
            10.0,
        ))

    report = build_report(rows, min_samples=30)
    cell = report.cells[0]

    assert cell.sample_count == 60
    assert cell.strongest_source == "ch1"
    assert cell.blend_mae == pytest.approx(0.5)
    assert cell.per_source[0].source_mae == pytest.approx(0.4)
    assert cell.blend_wins is False, (
        "ch1 was closer to the observation on every shared target; the "
        "paired report must say so"
    )
    assert report.overall_blend_wins is False

    # The two verdicts disagree on identical underlying forecasts. That
    # disagreement IS the finding.
    assert legacy != report.overall_blend_wins


def test_each_source_is_graded_only_where_it_and_the_blend_both_spoke():
    """The pairing rule, stated as an assertion.

    ch2 is absent for half the hours. If its MAE were compared against a
    blend MAE computed over ALL hours, the two would again describe
    different sets. The blend MAE reported alongside ch2 must be the
    blend's error on ch2's own rows.
    """
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(80):
        valid_at = (base + timedelta(hours=i)).isoformat()
        contributors = {"ch1": {"raw": 10.2, "debiased": 10.2, "samples": 99}}
        # ch2 only on the second half, and the blend happens to be worse
        # on exactly those hours.
        blend_value = 10.3 if i < 40 else 11.0
        if i >= 40:
            contributors["ch2"] = {"raw": 10.1, "debiased": 10.1, "samples": 99}
        rows.append(_row(valid_at, "temperature", 3, blend_value, contributors, 10.0))

    cell = build_report(rows, min_samples=30).cells[0]
    by_source = {r.source: r for r in cell.per_source}

    # Blend overall: (0.3 * 40 + 1.0 * 40) / 80 = 0.65
    assert cell.blend_mae == pytest.approx(0.65)
    # ...but on ch2's forty rows the blend was off by 1.0 throughout.
    assert by_source["ch2"].blend_mae_on_subset == pytest.approx(1.0)
    assert by_source["ch2"].source_mae == pytest.approx(0.1)
    assert by_source["ch2"].margin == pytest.approx(-0.9)
    # ch1 spoke on every row, so its subset is the whole set.
    assert by_source["ch1"].blend_mae_on_subset == pytest.approx(0.65)


def test_the_verdict_is_against_the_strongest_source_not_the_average_one():
    """The alternative to this project is not "use the average source" —
    it is "use whichever single source turns out to be good here"."""
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(40):
        rows.append(_row(
            (base + timedelta(hours=i)).isoformat(), "temperature", 6, 10.5,
            {
                "ch1": {"raw": 10.4, "debiased": 10.4, "samples": 99},
                "srf": {"raw": 13.0, "debiased": 13.0, "samples": 99},
            },
            10.0,
        ))
    cell = build_report(rows, min_samples=30).cells[0]
    # Mean source error is (0.4 + 3.0) / 2 = 1.7, which the blend beats
    # comfortably. Against the best single source it does not.
    assert cell.strongest_source == "ch1"
    assert cell.blend_wins is False


def test_no_verdict_is_reported_below_the_sample_floor():
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    rows = [
        _row((base + timedelta(hours=i)).isoformat(), "temperature", 1, 10.5,
             {"ch1": {"raw": 10.4, "debiased": 10.4, "samples": 99}}, 10.0)
        for i in range(5)
    ]
    cell = build_report(rows, min_samples=30).cells[0]
    assert cell.blend_wins is None
    assert cell.sample_count == 5, "the count is still reported"


def test_a_thinly_sampled_source_is_excluded_even_in_a_well_sampled_cell():
    """There are two sample floors — one per cell, one per source — and
    the cell-level one masks the source-level one in most fixtures.

    Found by mutation testing: deleting the per-source floor left the
    whole suite green, because every existing fixture had a thin cell
    whenever it had a thin source. A guard no test can distinguish from
    its neighbour is a guard nobody knows is working.
    """
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(60):
        contributors = {"ch1": {"raw": 10.4, "debiased": 10.4, "samples": 99}}
        if i < 3:
            # meteoblue speaks three times, and looks spectacular on them.
            contributors["meteoblue"] = {
                "raw": 10.0, "debiased": 10.0, "samples": 99,
            }
        rows.append(_row(
            (base + timedelta(hours=i)).isoformat(), "temperature", 1,
            10.5, contributors, 10.0,
        ))

    cell = build_report(rows, min_samples=30).cells[0]
    sources = {r.source for r in cell.per_source}
    assert sources == {"ch1"}, (
        "three perfect samples must not be allowed to become the "
        "benchmark the whole blend is judged against"
    )
    assert cell.strongest_source == "ch1"


def test_an_empty_report_is_distinguishable_from_a_broken_one():
    """**SWF-P1-007 avoidance.** An accuracy sensor showed nothing for
    four releases because a crash and an empty result looked identical.
    A None verdict must always arrive with the counts that say which it
    is."""
    empty = build_report([])
    assert empty.overall_blend_wins is None
    assert empty.total_pairs == 0
    assert empty.cells_with_verdict == 0
    # And the dict form carries the same, so the sensor cannot lose it.
    assert empty.as_dict()["total_pairs"] == 0
    assert empty.as_dict()["overall_blend_wins"] is None


def test_verdict_requires_a_majority_of_decided_cells():
    from swissweather_fusion.models.comparison import CellResult

    def cell(wins):
        return CellResult(
            measurement="temperature", lead_hours=1, sample_count=99,
            blend_mae=0.5, blend_wins=wins, strongest_source="ch1",
            strongest_margin=0.1 if wins else -0.1,
        )

    assert verdict([]) is None
    assert verdict([cell(True), cell(True), cell(False)]) is True
    assert verdict([cell(True), cell(False), cell(False)]) is False
    # An exact tie is not a win. A blend that wins half its horizons has
    # a restricted range of validity, not a verdict.
    assert verdict([cell(True), cell(False)]) is False


def test_rows_without_an_observation_are_ignored_not_scored_as_zero():
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    rows = [
        _row(base.isoformat(), "temperature", 1, 10.5,
             {"ch1": {"raw": 10.4, "debiased": 10.4}}, None),
    ]
    assert build_report(rows).total_pairs == 0


def test_a_malformed_contributors_blob_costs_one_row_not_the_report():
    """"Impossible" states that crash a report are how a measurement
    silently stops being taken."""
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    rows = [{
        "valid_at": base.isoformat(), "measurement": "temperature",
        "lead_hours": 1, "blend_value": 10.5, "contributors": "not json",
        "actual_value": 10.0, "lead_time_basis": "poll",
    }]
    report = build_report(rows)
    assert report.total_pairs == 1
    assert report.cells[0].per_source == ()


# ---------------------------------------------------------------------------
# Storage — the paired table
# ---------------------------------------------------------------------------
def test_a_cell_is_recorded_once_however_often_the_blend_runs(db):
    """The blend cycle runs every ten minutes. Without the uniqueness
    constraint this table would take 144 samples of each cell per day, at
    no analytical gain."""
    row = ("2026-09-01T12:00:00+00:00", "temperature", 1,
           "2026-09-01T11:00:00+00:00", "poll", 10.5, "{}")
    assert db.insert_blend_comparisons_bulk([row]) == 1
    assert db.insert_blend_comparisons_bulk([row]) == 0
    assert db.get_storage_stats()["blend_comparison_rows"] == 1


def test_comparison_rows_are_scored_and_then_not_rescored(db):
    db.insert_blend_comparisons_bulk([
        ("2026-09-01T12:00:00+00:00", "temperature", 1,
         "2026-09-01T11:00:00+00:00", "poll", 10.5, "{}"),
    ])
    pending = db.get_pending_blend_comparisons("2026-09-02T00:00:00+00:00")
    assert len(pending) == 1

    db.apply_blend_comparison_batch(
        [(10.0, "2026-09-01T13:00:00+00:00", pending[0]["id"])], []
    )
    assert db.get_pending_blend_comparisons("2026-09-02T00:00:00+00:00") == []
    scored = db.get_reconciled_blend_comparisons()
    assert len(scored) == 1 and scored[0]["actual_value"] == 10.0


def test_the_comparison_table_is_bounded_even_with_retention_disabled(db):
    """purge_days = 0 means "keep forever" and the reporting installation
    runs that way. A measurement table must not be able to grow without
    limit because retention is off."""
    rows = [
        (f"2026-09-{1 + i // 24:02d}T{i % 24:02d}:00:00+00:00",
         "temperature", 1, "2026-09-01T00:00:00+00:00", "poll", 10.0, "{}")
        for i in range(50)
    ]
    db.insert_blend_comparisons_bulk(rows)
    db.trim_blend_comparison(20)
    assert db.get_storage_stats()["blend_comparison_rows"] == 20


def test_purge_never_deletes_a_comparison_still_waiting_for_its_observation(db):
    """Same protection pending forecast rows get (L-10): a purge window
    shorter than the abandon age would delete the measurement instead of
    letting it complete."""
    db.insert_blend_comparisons_bulk([
        ("2020-01-01T00:00:00+00:00", "temperature", 1,
         "2020-01-01T00:00:00+00:00", "poll", 10.0, "{}"),
    ])
    deleted = db.purge_older_than("2026-01-01T00:00:00+00:00")
    assert deleted["blend_comparison"] == 0
    assert db.get_storage_stats()["blend_comparison_rows"] == 1


def test_resetting_learning_also_clears_the_comparison(db):
    """Comparison rows are scored partly on each source's DEBIASED value,
    which is a function of the buckets being cleared. Keeping them would
    mix two learning regimes in one report."""
    db.insert_blend_comparisons_bulk([
        ("2026-09-01T12:00:00+00:00", "temperature", 1,
         "2026-09-01T11:00:00+00:00", "poll", 10.5, "{}"),
    ])
    result = db.reset_all_learning()
    assert result["comparisons_cleared"] == 1
    assert db.get_storage_stats()["blend_comparison_rows"] == 0


# ---------------------------------------------------------------------------
# Schema v4
# ---------------------------------------------------------------------------
def test_table_sql_still_contains_no_create_index():
    """v0.1.23 and v0.1.24 both took down setup on every upgrading
    installation with an index defined beside the CREATE TABLE
    statements. v0.3.0 adds two more indexes, so the rule is re-asserted
    rather than assumed."""
    assert "CREATE INDEX" not in db_module._TABLE_SQL.upper()
    assert "CREATE INDEX" in db_module._INDEX_SQL.upper()
    assert "idx_blend_comparison_pending" in db_module._INDEX_SQL


def test_v4_requirements_are_derived_from_v3_not_restated():
    """A hand-copied requirement set is how a v3 column gets dropped
    while adding a v4 one."""
    for table, columns in db_module._V3_REQUIRED_COLUMNS.items():
        assert columns <= db_module._V4_REQUIRED_COLUMNS[table]
    assert {"run_initialised_at", "lead_time_basis"} <= (
        db_module._V4_REQUIRED_COLUMNS["forecast_snapshots"]
    )


def _build_complete_v3_database(path):
    """The COMPLETE v3 schema, not a convenient subset.

    v0.2.2 (SWF-021-008) found a migration test that built a partial
    fixture and was cited as coverage for a migration that then failed in
    production on the columns the fixture omitted. Every v3 table is
    created here at its real v3 shape.
    """
    import sqlite3

    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE station_observations (
            id INTEGER PRIMARY KEY, ts TEXT NOT NULL,
            temperature REAL, humidity REAL, pressure REAL);
        CREATE TABLE forecast_snapshots (
            id INTEGER PRIMARY KEY, source TEXT NOT NULL,
            issued_at TEXT NOT NULL, valid_at TEXT NOT NULL,
            variable TEXT NOT NULL, value REAL,
            trigger_reason TEXT DEFAULT 'scheduled',
            reconciliation_status TEXT NOT NULL DEFAULT 'pending');
        CREATE TABLE radar_observations (
            id INTEGER PRIMARY KEY, ts TEXT NOT NULL,
            precip_accum_mm_1h REAL, precip_type TEXT, quality INTEGER);
        CREATE TABLE bucket_stats (
            hour_of_day INTEGER NOT NULL, season TEXT NOT NULL,
            lead_time_bucket TEXT NOT NULL, source TEXT NOT NULL,
            measurement TEXT NOT NULL,
            ema_bias REAL NOT NULL DEFAULT 0.0,
            ema_abs_error REAL NOT NULL DEFAULT 0.0,
            ema_weight REAL NOT NULL DEFAULT 0.0,
            sample_count INTEGER NOT NULL DEFAULT 0,
            last_updated TEXT,
            PRIMARY KEY (hour_of_day, season, lead_time_bucket, source, measurement));
        CREATE TABLE storm_events (
            id INTEGER PRIMARY KEY, start_ts TEXT NOT NULL, end_ts TEXT,
            peak_pressure_drop REAL, peak_temp_drop REAL,
            peak_precip_rate REAL, notes TEXT);
        CREATE TABLE storm_predictions (
            id INTEGER PRIMARY KEY, ts TEXT NOT NULL,
            probability REAL NOT NULL, features TEXT,
            reconciled INTEGER NOT NULL DEFAULT 0);
        INSERT INTO schema_meta VALUES ('schema_version', '3');
        INSERT INTO forecast_snapshots
            (source, issued_at, valid_at, variable, value, trigger_reason)
            VALUES ('ch1', '2026-09-01T00:00:00+00:00',
                    '2026-09-01T06:00:00+00:00', 'temperature', 12.0, 'scheduled');
        INSERT INTO station_observations (ts, temperature)
            VALUES ('2026-09-01T06:00:00+00:00', 12.5);
        INSERT INTO radar_observations (ts, precip_accum_mm_1h, quality)
            VALUES ('2026-09-01T06:00:00+00:00', 1.5, 8);
        INSERT INTO storm_events (start_ts) VALUES ('2026-09-01T06:00:00+00:00');
        INSERT INTO bucket_stats
            (hour_of_day, season, lead_time_bucket, source, measurement,
             ema_bias, ema_abs_error, ema_weight, sample_count)
            VALUES (6, 'SON', 'short', 'ch1', 'temperature', 0.3, 0.8, 1.2, 40);
    """)
    conn.commit()
    conn.close()


def test_a_real_v3_database_upgrades_without_raising(tmp_path):
    path = str(tmp_path / "v3.db")
    _build_complete_v3_database(path)
    database = SwissWeatherDB(path)
    try:
        stats = database.get_storage_stats()
        # Raw facts survive.
        assert stats["forecast_snapshots_rows"] == 1
        assert stats["station_observations_rows"] == 1
        # Model B history survives — v0.3.0 changes nothing about it, and
        # chaining the v3 rebuild onto every upgrade would have dropped it.
        assert stats["radar_observations_rows"] == 1
        assert stats["storm_events_rows"] == 1
        # Learned state is cleared, by explicit decision.
        assert stats["bucket_stats_rows"] == 0
        assert stats["blend_comparison_rows"] == 0
    finally:
        database.close()


def test_an_upgraded_v3_database_can_store_the_new_columns(tmp_path):
    """The migration is only done if the thing it enables actually works."""
    path = str(tmp_path / "v3b.db")
    _build_complete_v3_database(path)
    database = SwissWeatherDB(path)
    try:
        database.insert_forecast_snapshots_bulk([(
            "ch1", "2026-09-10T17:00:00+00:00", "2026-09-10T18:00:00+00:00",
            "temperature", 14.0, "scheduled",
            "2026-09-10T15:00:00+00:00", LEAD_TIME_BASIS_RUN,
        )])
        rows = database.get_pending_forecast_snapshots(
            until_ts="2026-09-11T00:00:00+00:00", measurements=("temperature",)
        )
        stored = [r for r in rows if r["run_initialised_at"] is not None]
        assert stored and stored[0]["lead_time_basis"] == LEAD_TIME_BASIS_RUN
    finally:
        database.close()


def test_upgrading_twice_is_a_no_op(tmp_path):
    path = str(tmp_path / "v3c.db")
    _build_complete_v3_database(path)
    SwissWeatherDB(path).close()
    database = SwissWeatherDB(path)
    try:
        assert database.get_storage_stats()["forecast_snapshots_rows"] == 1
    finally:
        database.close()


def test_a_v2_database_still_gets_the_v3_rebuild_before_v4(tmp_path):
    """The first cut of the v0.3.0 dispatch tested one column
    (reconciliation_status) to decide whether the v3 rebuild was needed.
    A v2 database HAS that column and still lacks
    storm_predictions.reconciled, so the rebuild was skipped and index
    creation then failed with "no such column: reconciled" — reproducing
    the exact v0.1.24 setup outage the ordering exists to prevent."""
    import sqlite3

    path = str(tmp_path / "v2.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE station_observations (
            id INTEGER PRIMARY KEY, ts TEXT NOT NULL,
            temperature REAL, humidity REAL, pressure REAL);
        CREATE TABLE forecast_snapshots (
            id INTEGER PRIMARY KEY, source TEXT NOT NULL,
            issued_at TEXT NOT NULL, valid_at TEXT NOT NULL,
            variable TEXT NOT NULL, value REAL,
            trigger_reason TEXT DEFAULT 'scheduled',
            reconciliation_status TEXT NOT NULL DEFAULT 'pending');
        CREATE TABLE radar_observations (
            id INTEGER PRIMARY KEY, ts TEXT NOT NULL, precip_rate_mmh REAL);
        CREATE TABLE bucket_stats (
            hour_of_day INTEGER NOT NULL, season TEXT NOT NULL,
            lead_time_bucket TEXT NOT NULL, source TEXT NOT NULL,
            measurement TEXT NOT NULL,
            ema_bias REAL NOT NULL DEFAULT 0.0,
            ema_abs_error REAL NOT NULL DEFAULT 0.0,
            ema_weight REAL NOT NULL DEFAULT 0.0,
            sample_count INTEGER NOT NULL DEFAULT 0, last_updated TEXT,
            PRIMARY KEY (hour_of_day, season, lead_time_bucket, source, measurement));
        CREATE TABLE storm_events (id INTEGER PRIMARY KEY, start_ts TEXT NOT NULL);
        CREATE TABLE storm_predictions (
            id INTEGER PRIMARY KEY, ts TEXT NOT NULL, probability REAL NOT NULL);
        INSERT INTO schema_meta VALUES ('schema_version', '2');
    """)
    conn.commit()
    conn.close()

    database = SwissWeatherDB(path)
    try:
        assert database.get_storage_stats()["blend_comparison_rows"] == 0
    finally:
        database.close()


# ---------------------------------------------------------------------------
# ARC-04 — run-time attribution
# ---------------------------------------------------------------------------
def test_metadata_parses_from_the_real_captures():
    ch1 = parse_metadata_response("meteoswiss_icon_ch1", CH1_METADATA)
    assert ch1.run_initialised_at == datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)
    assert ch1.update_interval == timedelta(hours=3)
    assert ch1.publication_lag == timedelta(hours=1, minutes=52, seconds=2)

    d2 = parse_metadata_response("dwd_icon_d2", D2_METADATA)
    assert d2.publication_lag == timedelta(hours=1, minutes=29, seconds=29)


def test_the_measured_lag_spread_is_recorded_not_assumed():
    """ARC-04's severity rests on the SPREAD between sources, so the
    number is asserted rather than described in a comment.

    22 minutes between CH1 and D2 is comfortably absorbed by a 24-hour
    lead-time bucket — which is why run-time attribution is not urgent
    for today's blend, and why the release notes say so instead of
    implying a fix to a live problem. It becomes urgent at AROME's
    measured 5h09m.
    """
    ch1 = parse_metadata_response("meteoswiss_icon_ch1", CH1_METADATA)
    d2 = parse_metadata_response("dwd_icon_d2", D2_METADATA)
    spread = abs(ch1.publication_lag - d2.publication_lag)
    assert spread < timedelta(minutes=30)


def test_metadata_url_matches_the_verified_path():
    assert build_metadata_url("meteoswiss_icon_ch1") == (
        "https://api.open-meteo.com/data/meteoswiss_icon_ch1/static/meta.json"
    )
    assert build_metadata_url("dwd_icon_d2") == (
        "https://api.open-meteo.com/data/dwd_icon_d2/static/meta.json"
    )


@pytest.mark.parametrize("payload", [
    {}, {"last_run_initialisation_time": None},
    {"last_run_initialisation_time": "not a number"},
    {"last_run_initialisation_time": 1789052400, "update_interval_seconds": 0},
])
def test_unparseable_metadata_yields_none_rather_than_raising(payload):
    result = parse_metadata_response("m", payload)
    assert result is None or result.update_interval is None


def test_a_metadata_failure_leaves_the_integration_exactly_as_v0_2_8(monkeypatch):
    """**The most important property of the metadata client.**

    Run time is an enhancement to lead-time attribution, not a
    dependency of it. A failure in a free, optional side-channel must
    never be able to stop forecast collection.
    """
    from swissweather_fusion.clients.open_meteo import OpenMeteoClient

    class ExplodingSession:
        def get(self, *args, **kwargs):
            raise RuntimeError("network is down")

    client = OpenMeteoClient(ExplodingSession())
    result = asyncio.run(client.async_fetch_model_metadata("ch1"))
    assert result is None


def test_a_source_with_no_metadata_endpoint_returns_none_without_a_request():
    from swissweather_fusion.clients.open_meteo import OpenMeteoClient

    class ForbiddenSession:
        def get(self, *args, **kwargs):
            raise AssertionError("srf has no metadata endpoint to call")

    client = OpenMeteoClient(ForbiddenSession())
    result = asyncio.run(client.async_fetch_model_metadata("srf"))
    assert result is None


def test_the_comparison_basis_is_all_or_nothing():
    """A row whose lead offset is run-relative for two sources and
    poll-relative for three is not run-relative. Calling it so would
    reintroduce the undeclared mix ARC-04 is about."""
    c = coord.ModelABlendCoordinator(FakeHass(), None)
    # v0.3.1 (SWF-ICS-049): takes the bases actually RECORDED on each
    # contributing row, not source names. Passing names let a failed
    # metadata fetch produce a row claiming 'run' while the forecast row
    # it came from said 'poll'.
    assert c._comparison_basis(
        [LEAD_TIME_BASIS_RUN, LEAD_TIME_BASIS_RUN]
    ) == LEAD_TIME_BASIS_RUN
    assert c._comparison_basis(
        [LEAD_TIME_BASIS_RUN, LEAD_TIME_BASIS_POLL]
    ) == LEAD_TIME_BASIS_POLL
    assert c._comparison_basis([]) == LEAD_TIME_BASIS_POLL


# ---------------------------------------------------------------------------
# Reachability — the "implemented but never reached" defect class (§7.1)
# ---------------------------------------------------------------------------
def test_every_metadata_model_is_a_real_forecast_source():
    assert set(OPEN_METEO_METADATA_MODELS) <= set(ALL_FORECAST_SOURCES)


def test_every_open_meteo_model_has_a_metadata_mapping():
    """Otherwise a source silently keeps poll-relative lead times while
    the release notes claim otherwise."""
    assert set(MODEL_PARAM) == set(OPEN_METEO_METADATA_MODELS)


def test_every_forecast_source_has_an_update_cadence():
    """A missing cadence entry makes freshness_factor return 1.0 — no
    error, no log line, no reweighting. ARC-08."""
    assert set(ALL_FORECAST_SOURCES) <= set(SOURCE_UPDATE_CADENCE)


def test_every_comparison_lead_offset_is_positive_and_ordered():
    assert list(BLEND_COMPARISON_LEAD_HOURS) == sorted(BLEND_COMPARISON_LEAD_HOURS)
    assert all(h > 0 for h in BLEND_COMPARISON_LEAD_HOURS)


# ---------------------------------------------------------------------------
# The regression the new columns nearly introduced
# ---------------------------------------------------------------------------
def test_physical_bounds_validation_still_applies_to_the_wider_row():
    """**Found while building v0.3.0, not by a report.**

    validate_forecast_rows tested `len(row) != 6` and passed anything
    else through untouched. The moment the Open-Meteo coordinator
    appended two columns, bounds validation would have silently stopped
    running for ch1, ch2 and icon_d2 — three of five sources and the
    majority of stored values — with every existing test still green and
    nothing logged.
    """
    wide = [(
        "ch1", "2026-09-10T17:00:00+00:00", "2026-09-10T18:00:00+00:00",
        "temperature", 999.0, "scheduled",
        "2026-09-10T15:00:00+00:00", LEAD_TIME_BASIS_RUN,
    )]
    validated, rejected, dropped = provider_validation.validate_forecast_rows(wide)
    assert rejected == 1
    assert validated[0][4] is None
    # Shape and the trailing columns are preserved exactly.
    assert len(validated[0]) == 8
    assert validated[0][6] == "2026-09-10T15:00:00+00:00"
    assert validated[0][7] == LEAD_TIME_BASIS_RUN


def test_physical_bounds_validation_still_applies_to_the_narrow_row():
    narrow = [(
        "srf", "2026-09-10T17:00:00+00:00", "2026-09-10T18:00:00+00:00",
        "temperature", 999.0, "scheduled",
    )]
    validated, rejected, dropped = provider_validation.validate_forecast_rows(narrow)
    assert rejected == 1
    assert validated[0][4] is None and len(validated[0]) == 6


def test_storage_rejects_a_row_shape_it_does_not_understand(db):
    with pytest.raises(ValueError):
        db.insert_forecast_snapshots_bulk([("ch1", "a", "b", "temperature")])


# ---------------------------------------------------------------------------
# The extractions — one definition, not two
# ---------------------------------------------------------------------------
def test_debiased_value_is_the_rule_blend_actually_uses():
    """If these two ever diverge, the comparison grades sources on
    numbers the blend never saw, and the divergence is invisible in the
    output."""
    trusted = model_a.SourceContribution(
        source="ch1", raw_value=10.0, ema_bias=0.4,
        ema_weight=1.0, sample_count=MIN_SAMPLES_TO_TRUST_BUCKET,
    )
    cold = model_a.SourceContribution(
        source="ch2", raw_value=10.0, ema_bias=0.4,
        ema_weight=1.0, sample_count=MIN_SAMPLES_TO_TRUST_BUCKET - 1,
    )
    assert model_a.debiased_value(trusted) == pytest.approx(9.6)
    # Cold start contributes RAW, not a partially-learned correction.
    assert model_a.debiased_value(cold) == pytest.approx(10.0)
    # A single trusted contributor means blend() == its debiased value,
    # which pins the two definitions together.
    assert model_a.blend([trusted]) == pytest.approx(
        model_a.debiased_value(trusted)
    )
    assert model_a.blend([cold]) == pytest.approx(model_a.debiased_value(cold))


def test_debiased_value_passes_through_a_missing_reading():
    empty = model_a.SourceContribution(
        source="ch1", raw_value=None, ema_bias=0.4,
        ema_weight=1.0, sample_count=99,
    )
    assert model_a.debiased_value(empty) is None


def test_the_refactor_did_not_change_blend_output(db):
    """W0's exit criterion is that NOTHING the user sees changes. The
    contribution loop moved out of _blend_at; the result must not."""
    c = coord.ModelABlendCoordinator(FakeHass(), db)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    target_iso = now.isoformat()
    latest = {
        ("ch1", "temperature", target_iso): (20.0, now),
        ("ch2", "temperature", target_iso): (22.0, now),
        ("srf", "temperature", target_iso): (21.0, now),
    }
    blended = c._blend_at(
        "temperature", now, latest_forecast=latest, bucket_lookup={}
    )
    contributions = c._contributions_at(
        "temperature", now, latest_forecast=latest, bucket_lookup={}
    )
    assert blended == pytest.approx(model_a.blend(contributions))
    # All cold start, so a plain average of raw values — v0.2.8 behaviour.
    assert blended == pytest.approx(21.0)


# ---------------------------------------------------------------------------
# Recording — what goes in the table and what does not
# ---------------------------------------------------------------------------
def test_only_class_a_measurements_are_recorded(db):
    """Class B and C have no local ground truth by definition, so a
    comparison row for them could never be scored. Recording them to be
    abandoned later would make a stalled measurement look busy."""
    c = coord.ModelABlendCoordinator(FakeHass(), db)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    latest = {}
    for lead in BLEND_COMPARISON_LEAD_HOURS:
        target_iso = (now + timedelta(hours=lead)).isoformat()
        for measurement in ("temperature", "humidity", "pressure",
                            "precip", "wind_gust_speed", "weather_code"):
            latest[("ch1", measurement, target_iso)] = (10.0, now)
            latest[("ch2", measurement, target_iso)] = (11.0, now)

    c._record_blend_comparison(now=now, latest_forecast=latest, bucket_lookup={})

    rows = db.get_pending_blend_comparisons(
        (now + timedelta(days=7)).isoformat()
    )
    recorded = {r["measurement"] for r in rows}
    assert recorded == set(coord.ModelABlendCoordinator.LEARNED_MEASUREMENTS)
    assert len(rows) == 3 * len(BLEND_COMPARISON_LEAD_HOURS)


def test_a_recorded_row_carries_every_contributing_source(db):
    c = coord.ModelABlendCoordinator(FakeHass(), db)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    target_iso = (now + timedelta(hours=1)).isoformat()
    latest = {
        ("ch1", "temperature", target_iso): (20.0, now),
        ("srf", "temperature", target_iso): (22.0, now),
    }
    c._record_blend_comparison(now=now, latest_forecast=latest, bucket_lookup={})

    rows = db.get_pending_blend_comparisons((now + timedelta(days=7)).isoformat())
    row = next(r for r in rows if r["lead_hours"] == 1)
    contributors = json.loads(row["contributors"])
    assert set(contributors) == {"ch1", "srf"}
    assert contributors["ch1"]["raw"] == pytest.approx(20.0)
    # Cold start: debiased == raw, and the row says which it was.
    assert contributors["ch1"]["debiased"] == pytest.approx(20.0)
    assert contributors["ch1"]["trusted"] is False
    assert row["blend_value"] == pytest.approx(21.0)
    # srf has no metadata endpoint, so the row is honestly poll-relative.
    assert row["lead_time_basis"] == LEAD_TIME_BASIS_POLL


def test_recording_failure_never_breaks_the_forecast(db, monkeypatch):
    """The measurement is the point of the release, and it is still less
    important than the forecast the user actually sees."""
    c = coord.ModelABlendCoordinator(FakeHass(), db)

    def explode(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "insert_blend_comparisons_bulk", explode)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    latest = {
        ("ch1", "temperature", (now + timedelta(hours=1)).isoformat()): (20.0, now),
    }
    # Must not raise.
    c._record_blend_comparison(now=now, latest_forecast=latest, bucket_lookup={})
