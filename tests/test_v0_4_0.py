"""v0.4.0: learning memory (C2), no season key, error persistence (C3),
derived dew point (C4), gusts by median (C5), and the storage items.

The evidence for each change is in swissweather_fusion_v0.4.0_release_audit.md
(replay of the learning on a real installation's database, 15 Sep - 15 Nov
2026). These tests pin the mechanics: that what was replayed is what runs.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from swissweather_fusion import const
from swissweather_fusion import coordinator as coord
from swissweather_fusion.models import model_a
from swissweather_fusion.storage import db as dbmod
from swissweather_fusion.storage.db import BucketKey, SwissWeatherDB
from tests.test_v0_2_1_fixes import FakeHass

H = datetime(2026, 11, 15, 17, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    database = SwissWeatherDB(str(tmp_path / "v040.db"))
    yield database
    database.close()


def iso(dt: datetime) -> str:
    return dt.isoformat()


# ---------------------------------------------------------------------------
# constants that must agree across modules
# ---------------------------------------------------------------------------


def test_storage_constants_match_const():
    assert dbmod.SEASON_ALL == const.SEASON_ALL
    assert dbmod.BLEND_SOURCE == const.SOURCE_BLEND


def test_bucket_season_is_constant_all_year():
    for month in range(1, 13):
        assert model_a.bucket_season(datetime(2026, month, 15, tzinfo=timezone.utc)) == "ALL"
    # derive_season is still the real season for anything that needs it
    assert model_a.derive_season(datetime(2026, 12, 1, tzinfo=timezone.utc)) == "DJF"


def test_hacs_minimum_is_the_supported_baseline():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    hacs = json.loads((root / "hacs.json").read_text())
    assert hacs["homeassistant"] == "2026.10.0"
    manifest = json.loads((root / "custom_components/swissweather_fusion/manifest.json").read_text())
    assert manifest["version"] == "0.4.0"


# ---------------------------------------------------------------------------
# C2: memory as a half-life in days
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("k", [1, 2, 6, 20])
def test_k_samples_of_one_day_decay_by_the_daily_factor(k):
    a = model_a.alpha_for_samples(k)
    assert (1 - a) ** k == pytest.approx(0.5 ** (1 / const.LEARNING_HALF_LIFE_DAYS))
    # three days of k samples halve the old state
    assert (1 - a) ** (k * 3) == pytest.approx(0.5)


def test_alpha_for_zero_or_negative_counts_is_one_sample():
    assert model_a.alpha_for_samples(0) == model_a.alpha_for_samples(1)
    assert model_a.alpha_for_samples(-3) == model_a.alpha_for_samples(1)


def test_update_bucket_ema_uses_the_given_alpha():
    r = model_a.update_bucket_ema(
        previous_bias=1.0, previous_abs_error=2.0, previous_sample_count=10,
        forecast_value=12.0, actual_value=10.0, lead_time_bucket="short", alpha=0.5,
    )
    assert r.ema_bias == pytest.approx(0.5 * 2.0 + 0.5 * 1.0)
    assert r.ema_abs_error == pytest.approx(0.5 * abs(12.0 - 1.0 - 10.0) + 0.5 * 2.0)


def test_update_bucket_ema_default_alpha_unchanged():
    r = model_a.update_bucket_ema(
        previous_bias=0.0, previous_abs_error=1.0, previous_sample_count=10,
        forecast_value=11.0, actual_value=10.0, lead_time_bucket="short",
    )
    assert r.ema_bias == pytest.approx(const.EMA_ALPHA_BY_LEAD_TIME["short"] * 1.0)


@pytest.mark.parametrize("bad", [0.0, -0.1, 1.5, float("nan")])
def test_update_bucket_ema_rejects_an_invalid_alpha(bad):
    with pytest.raises(ValueError):
        model_a.update_bucket_ema(
            previous_bias=0.0, previous_abs_error=1.0, previous_sample_count=3,
            forecast_value=1.0, actual_value=0.0, lead_time_bucket="short", alpha=bad,
        )


def _learn_once(db, now):
    learner = coord.ModelALearningCoordinator(FakeHass(), db)
    real = model_a.utcnow
    model_a.utcnow = lambda: now
    try:
        learner._reconcile()
    finally:
        model_a.utcnow = real


def test_learning_weights_each_row_by_its_runs_per_target_hour(db):
    """Four runs forecasting one target hour from the same lead bucket
    each get alpha_for_samples(4); a lone row gets alpha_for_samples(1)."""
    target = H - timedelta(hours=2)
    db.insert_station_observation(iso(target), 10.0, 70.0, 1010.0)
    # four ch2 runs, all short lead, for one target hour
    for i, value in enumerate((11.0, 12.0, 13.0, 14.0)):
        db.insert_forecast_snapshots_bulk([
            ("ch2", iso(target - timedelta(hours=6 + i)), iso(target), "temperature", value, "scheduled")
        ])
    # one srf row
    db.insert_forecast_snapshots_bulk([
        ("srf", iso(target - timedelta(hours=5)), iso(target), "temperature", 9.0, "scheduled")
    ])
    _learn_once(db, H)

    def stats(source):
        return db.get_bucket_stats(BucketKey(
            hour_of_day=target.hour, season="ALL", lead_time_bucket="short",
            source=source, measurement="temperature"))

    # srf: a first sample sets bias = error regardless of alpha
    assert stats("srf").ema_bias == pytest.approx(-1.0)
    # ch2: replay the four updates with alpha(4), in the order learned
    a = model_a.alpha_for_samples(4)
    rows = db._conn.execute(
        "SELECT value FROM forecast_snapshots WHERE source='ch2' ORDER BY valid_at, id").fetchall()
    bias, n = 0.0, 0
    for (v,) in rows:
        err = v - 10.0
        bias = err if n == 0 else a * err + (1 - a) * bias
        n += 1
    assert stats("ch2").ema_bias == pytest.approx(bias)
    assert stats("ch2").sample_count == 4
    # nothing was written under a real season
    assert {r[0] for r in db._conn.execute("SELECT season FROM bucket_stats")} == {"ALL"}


def test_learning_and_blend_use_the_same_season_key(db):
    target = H - timedelta(hours=2)
    db.insert_station_observation(iso(target), 10.0, 70.0, 1010.0)
    db.insert_forecast_snapshots_bulk([
        ("ch2", iso(target - timedelta(hours=6)), iso(target), "temperature", 12.0, "scheduled")
    ])
    _learn_once(db, H)
    blend = coord.ModelABlendCoordinator(FakeHass(), db)
    lookup = {
        (r["hour_of_day"], r["season"], r["lead_time_bucket"], r["source"], r["measurement"]): r
        for r in db.get_all_bucket_stats()
    }
    contributions = blend._contributions_at(
        "temperature", target,
        latest_forecast={("ch2", "temperature", iso(target)): (12.0, target - timedelta(hours=6))},
        bucket_lookup={k: dbmod.BucketStats(r["ema_bias"], r["ema_abs_error"], r["ema_weight"],
                                            r["sample_count"], r["last_updated"])
                       for k, r in lookup.items()},
    )
    assert contributions and contributions[0].ema_bias == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# migration v7
# ---------------------------------------------------------------------------


def _v6_database(path):
    """A database as v0.3.6 left it: seasonal buckets, no persistence column."""
    database = SwissWeatherDB(path)
    c = database._conn
    c.execute("DELETE FROM schema_meta WHERE key = ?", (dbmod._V7_SEASON_MERGE_KEY,))
    c.execute("DROP TABLE blend_comparison")
    c.execute(
        "CREATE TABLE blend_comparison (id INTEGER PRIMARY KEY, valid_at TEXT NOT NULL, "
        "measurement TEXT NOT NULL, lead_hours INTEGER NOT NULL, issued_at TEXT NOT NULL, "
        "lead_time_basis TEXT NOT NULL DEFAULT 'poll', blend_value REAL NOT NULL, "
        "contributors TEXT NOT NULL, actual_value REAL, reconciled_at TEXT, "
        "status TEXT NOT NULL DEFAULT 'pending', UNIQUE (valid_at, measurement, lead_hours))")
    c.execute("INSERT INTO blend_comparison (valid_at, measurement, lead_hours, issued_at, "
              "blend_value, contributors) VALUES ('2026-11-01T10:00:00+00:00', 'temperature', 1, "
              "'2026-11-01T09:00:00+00:00', 7.5, '{}')")
    rows = [
        (5, "SON", "short", "ch1", "temperature", 0.3, 0.8, 1.2, 40, "2026-11-10T05:00:00+00:00"),
        (5, "JJA", "short", "ch1", "temperature", 0.9, 1.1, 0.9, 80, "2026-08-31T05:00:00+00:00"),
        (5, "SON", "short", "srf", "temperature", -1.0, 0.5, 2.0, 30, "2026-11-10T05:00:00+00:00"),
        (6, "MAM", "long", "ch2", "humidity", 4.0, 3.0, 0.3, 12, "2026-05-31T06:00:00+00:00"),
    ]
    c.executemany("INSERT INTO bucket_stats VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    c.execute("UPDATE schema_meta SET value = '6' WHERE key = 'schema_version'")
    c.commit()
    database.close()


def test_migration_merges_seasons_keeping_the_most_recent(tmp_path):
    path = str(tmp_path / "v6.db")
    _v6_database(path)
    database = SwissWeatherDB(path)
    try:
        got = {tuple(r)[:5]: tuple(r)[5:9] for r in database._conn.execute(
            "SELECT hour_of_day, season, lead_time_bucket, source, measurement, "
            "ema_bias, ema_abs_error, ema_weight, sample_count FROM bucket_stats")}
        assert got == {
            (5, "ALL", "short", "ch1", "temperature"): (0.3, 0.8, 1.2, 40),  # SON was newer than JJA
            (5, "ALL", "short", "srf", "temperature"): (-1.0, 0.5, 2.0, 30),
            (6, "ALL", "long", "ch2", "humidity"): (4.0, 3.0, 0.3, 12),      # only one: kept
        }
        assert database._get_meta("schema_version") == "7"
        cols = {r[1] for r in database._conn.execute("PRAGMA table_info(blend_comparison)")}
        assert "persistence_delta" in cols
        assert database._conn.execute(
            "SELECT persistence_delta FROM blend_comparison").fetchone()[0] == 0.0
    finally:
        database.close()


def test_migration_runs_once_and_is_idempotent(tmp_path):
    path = str(tmp_path / "v6.db")
    _v6_database(path)
    SwissWeatherDB(path).close()
    database = SwissWeatherDB(path)
    try:
        # a seasonal row written after the merge (e.g. by a downgrade) is
        # not touched by a second open: the merge is marker-guarded
        database._conn.execute(
            "INSERT INTO bucket_stats VALUES (7,'SON','short','ch1','temperature',0,0,0,1,'x')")
        database._conn.commit()
    finally:
        database.close()
    again = SwissWeatherDB(path)
    try:
        assert again._conn.execute(
            "SELECT count(*) FROM bucket_stats WHERE season = 'SON'").fetchone()[0] == 1
    finally:
        again.close()


def test_fresh_database_has_the_persistence_column_and_marker(db):
    cols = {r[1] for r in db._conn.execute("PRAGMA table_info(blend_comparison)")}
    assert "persistence_delta" in cols
    assert db._get_meta(dbmod._V7_SEASON_MERGE_KEY) == "done"


# ---------------------------------------------------------------------------
# newest-run window query
# ---------------------------------------------------------------------------


def test_window_returns_the_newest_value_per_key(db):
    t = iso(H + timedelta(hours=3))
    db.insert_forecast_snapshots_bulk([
        ("ch1", iso(H - timedelta(hours=3)), t, "temperature", 5.0, "scheduled"),
        ("ch1", iso(H - timedelta(hours=1)), t, "temperature", 6.0, "scheduled"),
        ("ch1", iso(H - timedelta(hours=2)), t, "temperature", 7.0, "scheduled"),
    ])
    rows = db.get_forecast_snapshots_in_window(start_valid_at=iso(H), end_valid_at=iso(H + timedelta(hours=5)))
    assert [(r["source"], r["value"]) for r in rows] == [("ch1", 6.0)]


def test_window_skips_a_newer_row_without_value(db):
    t = iso(H + timedelta(hours=3))
    db.insert_forecast_snapshots_bulk([
        ("ch1", iso(H - timedelta(hours=3)), t, "temperature", 5.0, "scheduled")])
    db.insert_forecast_snapshot("ch1", iso(H - timedelta(hours=1)), t, "temperature", None)
    rows = db.get_forecast_snapshots_in_window(start_valid_at=iso(H), end_valid_at=iso(H + timedelta(hours=5)))
    assert [r["value"] for r in rows] == [5.0]


def test_window_breaks_ties_by_the_later_insert_and_excludes_blend_rows(db):
    t = iso(H + timedelta(hours=3))
    same = iso(H - timedelta(hours=1))
    db.insert_forecast_snapshots_bulk([("srf", same, t, "temperature", 5.0, "scheduled")])
    db.insert_forecast_snapshots_bulk([("srf", same, t, "temperature", 5.5, "scheduled")])
    db.insert_forecast_snapshots_bulk([("blend", same, t, "temperature", 9.9, "blend_verification")])
    rows = db.get_forecast_snapshots_in_window(start_valid_at=iso(H), end_valid_at=iso(H + timedelta(hours=5)))
    assert [(r["source"], r["value"]) for r in rows] == [("srf", 5.5)]


def test_window_rows_are_ordered_by_source_variable_valid_at(db):
    rows_in = []
    for src in ("srf", "ch1"):
        for var in ("pressure", "humidity"):
            for h in (4, 2):
                rows_in.append((src, iso(H - timedelta(hours=1)), iso(H + timedelta(hours=h)), var, 1.0, "s"))
    db.insert_forecast_snapshots_bulk(rows_in)
    rows = db.get_forecast_snapshots_in_window(start_valid_at=iso(H), end_valid_at=iso(H + timedelta(hours=5)))
    keys = [(r["source"], r["variable"], r["valid_at"]) for r in rows]
    assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# storage hygiene
# ---------------------------------------------------------------------------


def test_wal_size_limit_is_set(db):
    assert db._conn.execute("PRAGMA journal_size_limit").fetchone()[0] == dbmod.WAL_SIZE_LIMIT_BYTES


def test_checkpoint_truncate_empties_the_log(tmp_path):
    import os
    path = str(tmp_path / "wal.db")
    database = SwissWeatherDB(path)
    try:
        database.insert_forecast_snapshots_bulk([
            ("ch1", iso(H), iso(H + timedelta(hours=i)), "temperature", float(i), "s") for i in range(2000)])
        assert os.path.getsize(path + "-wal") > 0
        database.checkpoint_truncate()
        assert os.path.getsize(path + "-wal") == 0
    finally:
        database.close()


def test_purge_checkpoints_only_when_something_was_deleted(db, monkeypatch):
    calls = []
    monkeypatch.setattr(db, "checkpoint_truncate", lambda: calls.append(1))
    retention = coord.RetentionCoordinator(FakeHass(), db, purge_days=90)
    asyncio.run(retention._async_purge())
    assert calls == []
    old = iso(datetime.now(timezone.utc) - timedelta(days=200))
    db._conn.execute(
        "INSERT INTO forecast_snapshots (source, issued_at, valid_at, variable, value, "
        "reconciliation_status) VALUES ('ch1', ?, ?, 'wind_speed', 1.0, 'not_learned')", (old, old))
    db._conn.commit()
    asyncio.run(retention._async_purge())
    assert calls == [1]


def test_blend_verification_rows_are_written_once_per_hour(db, monkeypatch):
    for src in ("ch1", "ch2", "srf"):
        db.insert_forecast_snapshots_bulk([
            (src, iso(H - timedelta(hours=1)), iso(H + timedelta(hours=h)), var, 10.0 + h, "s")
            for h in range(0, 50) for var in ("temperature", "humidity", "pressure")])
    blend = coord.ModelABlendCoordinator(FakeHass(), db)
    monkeypatch.setattr(model_a, "utcnow", lambda: H + timedelta(minutes=3))
    blend._compute_blend()
    first = db._conn.execute("SELECT count(*) FROM forecast_snapshots WHERE source='blend'").fetchone()[0]
    monkeypatch.setattr(model_a, "utcnow", lambda: H + timedelta(minutes=13))
    blend._compute_blend()
    assert first > 0
    assert db._conn.execute("SELECT count(*) FROM forecast_snapshots WHERE source='blend'").fetchone()[0] == first
    monkeypatch.setattr(model_a, "utcnow", lambda: H + timedelta(minutes=63))
    blend._compute_blend()
    assert db._conn.execute("SELECT count(*) FROM forecast_snapshots WHERE source='blend'").fetchone()[0] == 2 * first


# ---------------------------------------------------------------------------
# C4 dew point, C5 gusts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("t, rh, td", [
    (20.0, 50.0, 9.26), (8.4, 62.8, 1.70), (0.0, 100.0, 0.0), (-10.0, 80.0, -12.79), (30.0, 30.0, 10.53),
])
def test_dew_point_magnus_matches_reference_values(t, rh, td):
    assert model_a.dew_point_magnus(t, rh) == pytest.approx(td, abs=0.02)


@pytest.mark.parametrize("t, rh", [(None, 50.0), (10.0, None), (10.0, 0.0), (10.0, -5.0),
                                   (float("nan"), 50.0), (10.0, float("inf"))])
def test_dew_point_magnus_refuses_unusable_inputs(t, rh):
    assert model_a.dew_point_magnus(t, rh) is None


def test_dew_point_magnus_clips_supersaturation_and_never_exceeds_temperature():
    assert model_a.dew_point_magnus(5.0, 104.0) == pytest.approx(5.0)
    for t in range(-20, 36, 5):
        for rh in range(5, 101, 5):
            assert model_a.dew_point_magnus(float(t), float(rh)) <= t + 1e-9


def test_finalise_values_derives_dew_point_from_published_values(db):
    blend = coord.ModelABlendCoordinator(FakeHass(), db)
    values = {"temperature": 8.4, "humidity": 62.8, "pressure": 1012.0, "dew_point": 4.7}
    blend._finalise_values(values, 0, {})
    assert values["dew_point"] == pytest.approx(1.70, abs=0.02)
    keep = {"temperature": None, "humidity": 60.0, "dew_point": 4.7}
    blend._finalise_values(keep, 0, {})
    assert keep["dew_point"] == 4.7  # fused fallback stays


def test_gusts_fuse_by_median():
    from swissweather_fusion import forecast_parameters as fp
    assert fp.get("wind_gust_speed").fuse_values([4.0, 9.0, 5.0, 4.5]) == pytest.approx(4.75)


# ---------------------------------------------------------------------------
# C3 error persistence
# ---------------------------------------------------------------------------


def test_fit_phi_least_squares_through_origin():
    pairs = [(2.0 * p, p) for p in (1.0, -1.0, 0.5)] * 100
    assert model_a.fit_persistence_phi(pairs) == pytest.approx(const.PERSISTENCE_PHI_MAX)
    pairs = [(0.4 * p, p) for p in (1.0, -2.0, 0.5)] * 100
    assert model_a.fit_persistence_phi(pairs) == pytest.approx(0.4)


def test_fit_phi_is_never_negative_and_needs_enough_pairs():
    assert model_a.fit_persistence_phi([(-p, p) for p in (1.0, 2.0)] * 100) == 0.0
    few = [(0.5, 1.0)] * (const.PERSISTENCE_MIN_PAIRS - 1)
    assert model_a.fit_persistence_phi(few) is None
    assert model_a.fit_persistence_phi(few + [(0.5, 1.0)]) == pytest.approx(0.5)
    assert model_a.fit_persistence_phi([(0.0, 0.0)] * 500) == 0.0
    assert model_a.fit_persistence_phi([(None, 1.0)] * 500) is None


def test_phi_for_lead_interpolates_and_stops_after_six_hours():
    fitted = {1: 0.9, 3: 0.6, 6: 0.3}
    f = model_a.persistence_phi_for_lead
    assert f(0, fitted) == 0.0
    assert f(1, fitted) == 0.9
    assert f(2, fitted) == pytest.approx(0.75)
    assert f(4, fitted) == pytest.approx(0.5)
    assert f(5, fitted) == pytest.approx(0.4)
    assert f(6, fitted) == 0.3
    assert f(7, fitted) == 0.0
    assert f(24, fitted) == 0.0
    assert f(3, {}) == 0.0
    assert f(2, {3: 0.6}) == 0.6
    assert f(5, {1: 0.9, 3: 0.6}) == 0.6


def test_finalise_values_shifts_only_leads_one_to_six(db):
    blend = coord.ModelABlendCoordinator(FakeHass(), db)
    persistence = {"temperature": (1.0, {1: 0.8, 3: 0.6, 6: 0.3}),
                   "humidity": (10.0, {1: 0.8, 3: 0.6, 6: 0.3})}
    out = {}
    for lead in (0, 1, 2, 6, 7, 24):
        v = {"temperature": 10.0, "humidity": 95.0, "pressure": 1000.0}
        blend._finalise_values(v, lead, persistence)
        out[lead] = v
    assert out[0]["temperature"] == 10.0
    assert out[1]["temperature"] == pytest.approx(10.8)
    assert out[2]["temperature"] == pytest.approx(10.7)
    assert out[6]["temperature"] == pytest.approx(10.3)
    assert out[7]["temperature"] == 10.0 and out[24]["temperature"] == 10.0
    assert out[1]["humidity"] == 100.0           # clamped
    assert all(o["pressure"] == 1000.0 for o in out.values())  # no state: no shift
    assert out[1]["dew_point"] == pytest.approx(10.8, abs=0.01)  # from shifted T and RH


def _history(db, measurement, days, phi_true, base=10.0):
    """Reconciled comparison rows for leads 1, 3, 6 whose errors follow
    e_L = phi_true[L] * e_1(issue hour), with a published persistence shift
    of 0.3 * e_1(issue hour) included in blend_value — as the live system
    records it. The fit must remove that shift, or it would find phi - 0.3.
    The 1-hour errors are independent from hour to hour, so a predictor
    taken from the wrong hour fits phi ~ 0."""
    import random
    rng = random.Random(42)
    rows = []
    start = H - timedelta(days=days)
    e1 = {}
    for i in range(-24, days * 24):
        e1[start + timedelta(hours=i)] = rng.uniform(-2.0, 2.0)
    for i in range(days * 24):
        valid = start + timedelta(hours=i)
        for lead in (1, 3, 6):
            issue = valid - timedelta(hours=lead)
            err = e1[valid] if lead == 1 else phi_true[lead] * e1[issue]
            delta = 0.0 if lead == 1 else 0.3 * e1[issue]
            learned = base - err
            rows.append((iso(valid), measurement, lead, iso(issue), "poll", learned + delta, "{}", delta))
    db.insert_blend_comparisons_bulk(rows)
    db._conn.execute("UPDATE blend_comparison SET status='reconciled', actual_value=?", (base,))
    db._conn.commit()


def test_fit_recovers_phi_from_the_learned_blend_not_the_published_one(db):
    _history(db, "temperature", 20, {3: 0.5, 6: 0.2})
    blend = coord.ModelABlendCoordinator(FakeHass(), db)
    fitted = blend._fit_persistence(H)
    # lead 1 is predicted by the PREVIOUS hour's 1-hour error, independent here
    assert fitted["temperature"][1] == pytest.approx(0.0, abs=0.06)
    assert fitted["temperature"][3] == pytest.approx(0.5, abs=0.02)
    assert fitted["temperature"][6] == pytest.approx(0.2, abs=0.02)
    assert "humidity" not in fitted


def test_fit_needs_a_week_of_history(db):
    _history(db, "pressure", 5, {3: 0.5, 6: 0.2})
    blend = coord.ModelABlendCoordinator(FakeHass(), db)
    assert blend._fit_persistence(H) == {}


def test_persistence_state_uses_the_observation_at_the_issue_hour(db, monkeypatch):
    _history(db, "temperature", 20, {3: 0.5, 6: 0.2})
    # the learned 1-hour forecast for H, recorded with a shift of 0.4
    db.insert_blend_comparisons_bulk([(iso(H), "temperature", 1, iso(H - timedelta(hours=1)),
                                       "poll", 12.4, "{}", 0.4)])
    db.insert_station_observation(iso(H - timedelta(minutes=20)), 13.5, 70.0, 1000.0)
    # 17:03 UTC written with a -01:00 offset: in the future at 17:01, yet
    # its text sorts before the query's upper bound — only the explicit
    # "never from the future" check keeps it out (cf. SWF-ICS-043).
    db._conn.execute("INSERT INTO station_observations (ts, temperature, humidity, pressure) "
                     "VALUES ('2026-11-15T16:03:00-01:00', 99.0, 70.0, 1000.0)")
    db._conn.commit()
    monkeypatch.setattr(model_a, "utcnow", lambda: H + timedelta(minutes=1))
    blend = coord.ModelABlendCoordinator(FakeHass(), db)
    state = blend._persistence_state(H)
    error, fitted = state["temperature"]
    assert error == pytest.approx(13.5 - 12.0)   # observed minus LEARNED (12.4 - 0.4)
    assert fitted[3] == pytest.approx(0.5, abs=0.02)


def test_persistence_state_falls_back_to_the_previous_hour(db, monkeypatch):
    _history(db, "temperature", 20, {3: 0.5, 6: 0.2})
    db._conn.execute("DELETE FROM blend_comparison WHERE valid_at = ? AND lead_hours = 1", (iso(H),))
    db.insert_blend_comparisons_bulk([(iso(H - timedelta(hours=1)), "temperature", 1,
                                       iso(H - timedelta(hours=2)), "poll", 10.0, "{}", 0.0)])
    db._conn.execute("UPDATE blend_comparison SET blend_value = 10.0, persistence_delta = 0 "
                     "WHERE valid_at = ? AND lead_hours = 1", (iso(H - timedelta(hours=1)),))
    db._conn.commit()
    db.insert_station_observation(iso(H - timedelta(hours=1, minutes=-2)), 11.0, 70.0, 1000.0)
    monkeypatch.setattr(model_a, "utcnow", lambda: H + timedelta(minutes=4))
    blend = coord.ModelABlendCoordinator(FakeHass(), db)
    error, _ = blend._persistence_state(H)["temperature"]
    assert error == pytest.approx(1.0)


def test_persistence_state_is_empty_without_history_or_observation(db, monkeypatch):
    monkeypatch.setattr(model_a, "utcnow", lambda: H + timedelta(minutes=4))
    blend = coord.ModelABlendCoordinator(FakeHass(), db)
    assert blend._persistence_state(H) == {}
    _history(db, "temperature", 20, {3: 0.5, 6: 0.2})
    blend2 = coord.ModelABlendCoordinator(FakeHass(), db)
    assert blend2._persistence_state(H + timedelta(hours=5)) == {}  # no observation near that hour


def test_persistence_fit_is_cached_per_issue_hour(db, monkeypatch):
    _history(db, "temperature", 20, {3: 0.5, 6: 0.2})
    blend = coord.ModelABlendCoordinator(FakeHass(), db)
    calls = []
    real = blend._fit_persistence
    monkeypatch.setattr(blend, "_fit_persistence", lambda now: calls.append(now) or real(now))
    monkeypatch.setattr(model_a, "utcnow", lambda: H + timedelta(minutes=4))
    blend._persistence_state(H)
    blend._persistence_state(H)
    blend._persistence_state(H + timedelta(hours=1))
    assert calls == [H, H + timedelta(hours=1)]


def test_persistence_failure_never_breaks_the_blend(db, monkeypatch, caplog):
    blend = coord.ModelABlendCoordinator(FakeHass(), db)

    def boom(now):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(blend, "_fit_persistence", boom)
    assert blend._persistence_state(H) == {}
    assert "Error persistence unavailable" in caplog.text


def test_comparison_records_the_published_value_and_its_shift(db, monkeypatch):
    for src in ("ch1", "ch2", "srf"):
        db.insert_forecast_snapshots_bulk([
            (src, iso(H - timedelta(hours=1)), iso(H + timedelta(hours=h)), var, 10.0, "s")
            for h in range(0, 50) for var in ("temperature", "humidity", "pressure")])
    blend = coord.ModelABlendCoordinator(FakeHass(), db)
    monkeypatch.setattr(model_a, "utcnow", lambda: H + timedelta(minutes=3))
    monkeypatch.setattr(blend, "_persistence_state",
                        lambda now: {"temperature": (2.0, {1: 0.5, 3: 0.25, 6: 0.1})})
    result = blend._compute_blend()
    rows = {(r["measurement"], r["lead_hours"]): (r["blend_value"], r["persistence_delta"])
            for r in db._conn.execute("SELECT * FROM blend_comparison")}
    assert rows[("temperature", 1)] == (pytest.approx(11.0), pytest.approx(1.0))
    assert rows[("temperature", 3)] == (pytest.approx(10.5), pytest.approx(0.5))
    assert rows[("temperature", 6)] == (pytest.approx(10.2), pytest.approx(0.2))
    assert rows[("temperature", 24)] == (pytest.approx(10.0), 0.0)
    assert rows[("humidity", 1)] == (pytest.approx(10.0), 0.0)
    hourly = {e["datetime"]: e for e in result["hourly_forecast"]}
    assert hourly[iso(H + timedelta(hours=1))]["native_temperature"] == pytest.approx(11.0)
    assert hourly[iso(H + timedelta(hours=7))]["native_temperature"] == pytest.approx(10.0)
    assert result["current"]["temperature"] == pytest.approx(10.0)
    assert result["persistence"]["temperature"]["error"] == 2.0
    # the verification rows (the blend's own learning) stay unshifted
    v = db._conn.execute("SELECT value FROM forecast_snapshots WHERE source='blend' AND "
                         "variable='temperature' AND valid_at=?", (iso(H + timedelta(hours=1)),)).fetchall()
    assert all(x[0] == pytest.approx(10.0) for x in v)


def test_seven_field_comparison_rows_still_insert_with_zero_shift(db):
    n = db.insert_blend_comparisons_bulk([(iso(H), "temperature", 1, iso(H - timedelta(hours=1)),
                                           "poll", 9.0, "{}")])
    assert n == 1
    assert db._conn.execute("SELECT persistence_delta FROM blend_comparison").fetchone()[0] == 0.0
