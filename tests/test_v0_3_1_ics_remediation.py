"""Tests for v0.3.1 — remediation of the external ICS defect report.

Every test here follows the project standard: **if I break the fix, does
this test fail?** Each was confirmed failing against the pre-fix code
before the fix was restored.

The findings are grouped by what they protect:

* **measurement integrity** — SWF-ICS-051, 050, 049, 046
* **radar integrity** — SWF-ICS-032/065/074, 011, 010/021/057/066/071,
  033/060/073
* **physical model integrity** — SWF-ICS-001, 003/064/059
* **attack surface** — SWF-ICS-009, 008/026/070, 058
* **structural** — SWF-ICS-045/047/075, 028, 017, 043, 069/076
"""
import asyncio
import math
from datetime import datetime, timedelta, timezone

import pytest
import voluptuous as vol

from swissweather_fusion import config_flow, provider_validation
from swissweather_fusion import coordinator as coord
from swissweather_fusion.clients import combiprecip
from swissweather_fusion.const import (
    LEAD_TIME_BASIS_POLL,
    LEAD_TIME_BASIS_RUN,
    MAX_DIAGNOSTIC_DETAIL_CHARS,
    MAX_RADAR_DOWNLOAD_BYTES,
    RADAR_ACCUM_MAX_MM,
    RADAR_CLOCK_SKEW_TOLERANCE,
    RADAR_FRESHNESS_LIMIT,
)
from swissweather_fusion.models import model_a, model_b
from swissweather_fusion.storage import db as db_module
from swissweather_fusion.storage.db import SwissWeatherDB


@pytest.fixture
def db(tmp_path):
    database = SwissWeatherDB(str(tmp_path / "v031.db"))
    yield database
    database.close()


# ---------------------------------------------------------------------------
# SWF-ICS-051 — past-hour rows were graded as short-lead forecasts
# ---------------------------------------------------------------------------
_ISSUED = "2026-09-10T15:00:00+00:00"


def _row(valid_at, value=20.0, source="ch1"):
    return (source, _ISSUED, valid_at, "temperature", value, "scheduled")


def test_a_row_whose_target_has_already_passed_is_dropped():
    """**The defect that contaminated every short bucket since v0.1.**

    Open-Meteo's hourly series begins at 00:00 UTC today, so a poll at
    15:00 returns fifteen hours that have already happened. Those rows
    were stored, reconciled against observations of hours the model run
    had already ingested, and filed as `short` lead — because
    derive_lead_time_bucket tests `lead_hours < 24` with no lower bound.
    """
    rows = [
        _row("2026-09-10T06:00:00+00:00"),   # nine hours in the past
        _row("2026-09-10T15:00:00+00:00"),   # exactly zero lead
        _row("2026-09-10T18:00:00+00:00"),   # a genuine forecast
    ]
    validated, rejected, dropped = provider_validation.validate_forecast_rows(rows)

    assert dropped == 2
    assert rejected == 0
    assert len(validated) == 1
    assert validated[0][2] == "2026-09-10T18:00:00+00:00"


def test_zero_lead_is_dropped_because_it_is_an_analysis_not_a_forecast():
    """The boundary case, asserted separately because it is the one a
    reimplementation would get wrong. A "forecast" for the hour it was
    issued in is a nowcast of an hour the run already has observations
    for."""
    _, _, dropped = provider_validation.validate_forecast_rows(
        [_row(_ISSUED)]
    )
    assert dropped == 1


def test_an_unparseable_timestamp_is_passed_through_not_dropped():
    """Silently dropping a row because its timestamp could not be read
    would hide a provider regression behind a data-quality filter.
    Storage rejects it loudly instead."""
    rows = [("ch1", "not-a-time", "also-not", "temperature", 20.0, "scheduled")]
    validated, _, dropped = provider_validation.validate_forecast_rows(rows)
    assert dropped == 0
    assert validated == rows


def test_the_guard_covers_every_provider_not_just_open_meteo():
    """Applied in the shared barrier rather than in the Open-Meteo
    parser, so the next provider to return past hours does not
    rediscover this."""
    import inspect

    source = inspect.getsource(provider_validation.validate_forecast_rows)
    assert "_is_not_a_forecast" in source
    for shape in (6, 8):
        row = _row("2026-09-10T06:00:00+00:00")
        if shape == 8:
            row = row + ("2026-09-10T12:00:00+00:00", LEAD_TIME_BASIS_RUN)
        _, _, dropped = provider_validation.validate_forecast_rows([row])
        assert dropped == 1, f"{shape}-tuple form not covered"


def test_negative_lead_would_still_bucket_as_short():
    """Documents WHY the fix is at the storage barrier rather than in
    derive_lead_time_bucket.

    The bucket function is unchanged and still classifies a negative lead
    as `short`. That is acceptable only because such a row can no longer
    reach it. If someone later removes the barrier guard, this test
    stays green — which is exactly why the barrier test above exists and
    this one is documentation.
    """
    issued = datetime(2026, 9, 10, 15, tzinfo=timezone.utc)
    valid = datetime(2026, 9, 10, 6, tzinfo=timezone.utc)
    assert model_a.derive_lead_time_bucket(issued, valid) == "short"


def test_the_v5_migration_clears_contaminated_learning(tmp_path):
    """A v0.3.0 database passes every column check and still needs this,
    so the migration is dispatched on its own marker rather than on the
    table shape."""
    path = str(tmp_path / "v030.db")
    first = SwissWeatherDB(path)
    try:
        first.insert_forecast_snapshots_bulk([
            # A past-hour row of the kind SWF-ICS-051 stored.
            ("ch1", _ISSUED, "2026-09-10T06:00:00+00:00",
             "temperature", 20.0, "scheduled"),
            # A genuine forecast, which must survive.
            ("ch1", _ISSUED, "2026-09-10T18:00:00+00:00",
             "temperature", 21.0, "scheduled"),
        ])
        from swissweather_fusion.storage.db import BucketKey

        first.apply_reconciliation_batch(
            [(BucketKey(hour_of_day=12, season="SON", lead_time_bucket="short",
                        source="ch1", measurement="temperature"),
              0.4, 0.6, 1.2, 40, _ISSUED)], [], [],
        )
        assert first.get_storage_stats()["bucket_stats_rows"] == 1
        # Force the marker off, simulating a real v0.3.0 database.
        first._set_meta(db_module._V5_DATA_MIGRATION_KEY, "")
    finally:
        first.close()

    second = SwissWeatherDB(path)
    try:
        stats = second.get_storage_stats()
        assert stats["bucket_stats_rows"] == 0, "contaminated buckets kept"
        assert stats["forecast_snapshots_rows"] == 1, "forward history lost"
    finally:
        second.close()


def test_the_v5_migration_does_not_run_twice(tmp_path):
    path = str(tmp_path / "twice.db")
    SwissWeatherDB(path).close()
    database = SwissWeatherDB(path)
    try:
        database.insert_forecast_snapshots_bulk([
            ("ch1", _ISSUED, "2026-09-10T18:00:00+00:00",
             "temperature", 21.0, "scheduled"),
        ])
    finally:
        database.close()
    third = SwissWeatherDB(path)
    try:
        assert third.get_storage_stats()["forecast_snapshots_rows"] == 1
    finally:
        third.close()


def test_every_writer_of_learnable_rows_goes_through_the_barrier():
    """**The "will we have to reset again" test.**

    SWF-ICS-051's guard lives in provider_validation, which is only
    reached by callers that actually call it. MeteonomiqsCoordinator
    wrote forecast_snapshots directly and bypassed it entirely — so the
    051 fix was incomplete on its first pass, and the gap was found by
    asking this question rather than by reading the finding list.

    Asserting the wiring, not the behaviour, because the behaviour is
    already covered above and the wiring is what silently rots: a new
    provider added in six months gets a green suite either way unless
    something checks that it validates.
    """
    import inspect
    import re

    from swissweather_fusion import coordinator as c

    source = inspect.getsource(c)
    # Every call to the bulk writer must have a validation call in the
    # same enclosing function.
    for match in re.finditer(r"insert_forecast_snapshots_bulk", source):
        start = source.rfind("\n    async def ", 0, match.start())
        if start == -1:
            start = source.rfind("\n    def ", 0, match.start())
        body = source[start:match.start()]
        # The blend's own self-verification rows are generated internally
        # from already-validated inputs and have no provider behind them.
        if "_record_blend_verification" in body or "SOURCE_BLEND" in body:
            continue
        assert "validate_forecast_rows" in body, (
            "a writer reaches forecast_snapshots without the shared "
            "validation barrier"
        )


def test_a_non_finite_learned_statistic_is_refused_at_the_boundary(db):
    """bucket_stats is the one table whose corruption cannot be repaired
    forward — an EMA cannot un-absorb a sample, so the only remedy is
    another full wipe. This guard sits at the boundary of the
    irreversible table so it holds whatever happens upstream."""
    from swissweather_fusion.storage.db import BucketKey

    key = BucketKey(
        hour_of_day=12, season="SON", lead_time_bucket="short",
        source="ch1", measurement="temperature",
    )
    db.apply_reconciliation_batch(
        [(key, float("nan"), 0.5, 1.0, 10, _ISSUED)], [], []
    )
    assert db.get_storage_stats()["bucket_stats_rows"] == 0

    db.apply_reconciliation_batch(
        [(key, 0.3, float("inf"), 1.0, 10, _ISSUED)], [], []
    )
    assert db.get_storage_stats()["bucket_stats_rows"] == 0

    # A sound update still lands.
    db.apply_reconciliation_batch([(key, 0.3, 0.5, 1.0, 10, _ISSUED)], [], [])
    assert db.get_storage_stats()["bucket_stats_rows"] == 1


# ---------------------------------------------------------------------------
# SWF-ICS-050 — a stale run time survived a metadata outage
# ---------------------------------------------------------------------------
class _FakeClient:
    def __init__(self, sequence):
        self._sequence = list(sequence)
        self.calls = 0

    async def async_fetch_model_metadata(self, source):
        self.calls += 1
        return self._sequence.pop(0) if self._sequence else None


class FakeHass:
    def __init__(self):
        self.data = {}

        class States:
            def get(self, entity_id):
                return None

        self.states = States()

    async def async_add_executor_job(self, func, *args):
        return func(*args)


def _metadata(hours_ago=1):
    from swissweather_fusion.clients.open_meteo import ModelMetadata

    now = datetime.now(timezone.utc)
    return ModelMetadata(
        model="meteoswiss_icon_ch1",
        run_initialised_at=now - timedelta(hours=hours_ago),
        run_available_at=now,
        update_interval=timedelta(hours=3),
        fetched_at=now,
    )


def test_a_failed_metadata_fetch_clears_the_cached_run_time():
    """**The v0.3.0 defect this release fixes.**

    The method updated its fetch timestamp on every attempt but only
    overwrote the cached value on success, so a failing endpoint kept
    returning the previous run's initialisation time indefinitely —
    refreshing its own staleness clock hourly and never clearing. Its
    docstring claimed it returned None when the run time was unknown.
    """
    c = coord.OpenMeteoCoordinator.__new__(coord.OpenMeteoCoordinator)
    c._model_metadata = {}
    c._model_metadata_fetched = {}
    c._client = _FakeClient([_metadata(), None])

    first = asyncio.run(c._get_model_metadata("ch1"))
    assert first is not None

    # Expire the refresh window so the second call really fetches.
    c._model_metadata_fetched["ch1"] -= timedelta(hours=2)
    second = asyncio.run(c._get_model_metadata("ch1"))

    assert second is None, "a stale run time survived the outage"
    assert "ch1" not in c._model_metadata


def test_metadata_is_not_refetched_inside_the_refresh_window():
    c = coord.OpenMeteoCoordinator.__new__(coord.OpenMeteoCoordinator)
    c._model_metadata = {}
    c._model_metadata_fetched = {}
    c._client = _FakeClient([_metadata()])

    asyncio.run(c._get_model_metadata("ch1"))
    asyncio.run(c._get_model_metadata("ch1"))
    assert c._client.calls == 1


# ---------------------------------------------------------------------------
# SWF-ICS-049 — provenance was inferred from provider class
# ---------------------------------------------------------------------------
def test_the_comparison_basis_reflects_recorded_rows_not_source_names():
    c = coord.ModelABlendCoordinator(FakeHass(), None)
    assert c._comparison_basis(
        [LEAD_TIME_BASIS_RUN, LEAD_TIME_BASIS_RUN]
    ) == LEAD_TIME_BASIS_RUN
    assert c._comparison_basis(
        [LEAD_TIME_BASIS_RUN, LEAD_TIME_BASIS_POLL]
    ) == LEAD_TIME_BASIS_POLL
    assert c._comparison_basis([]) == LEAD_TIME_BASIS_POLL


def test_a_comparison_row_cannot_claim_run_basis_when_the_fetch_failed(db):
    """The v0.3.0 bug in one assertion: ch1 is an Open-Meteo source and
    COULD have a run time, but this poll did not obtain one. The row must
    say so."""
    import json

    c = coord.ModelABlendCoordinator(FakeHass(), db)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    target_iso = (now + timedelta(hours=1)).isoformat()
    latest = {("ch1", "temperature", target_iso): (20.0, now)}

    c._record_blend_comparison(
        now=now, latest_forecast=latest, bucket_lookup={},
        basis_by_source={("ch1", "temperature", target_iso): LEAD_TIME_BASIS_POLL},
    )
    rows = db.get_pending_blend_comparisons((now + timedelta(days=7)).isoformat())
    row = next(r for r in rows if r["lead_hours"] == 1)
    assert row["lead_time_basis"] == LEAD_TIME_BASIS_POLL
    assert json.loads(row["contributors"])["ch1"]["basis"] == LEAD_TIME_BASIS_POLL


# ---------------------------------------------------------------------------
# SWF-ICS-045/046/047/075 — transaction ownership
# ---------------------------------------------------------------------------
def test_an_exception_inside_a_transaction_rolls_back(db):
    """**Distinct from crash recovery.** SQLite rolls back when the
    PROCESS dies. It does not roll back when an exception is raised and
    caught inside a live process — there the transaction stays open, and
    the next successful commit writes the earlier partial work."""
    with pytest.raises(RuntimeError):
        with db._transaction() as conn:
            conn.execute(
                "INSERT INTO blend_comparison "
                "(valid_at, measurement, lead_hours, issued_at, "
                " lead_time_basis, blend_value, contributors) "
                "VALUES ('2026-09-01T00:00:00+00:00','temperature',1,"
                "'2026-09-01T00:00:00+00:00','poll',1.0,'{}')"
            )
            raise RuntimeError("boom")

    assert db.get_storage_stats()["blend_comparison_rows"] == 0

    # And a later successful write must not carry the discarded row in.
    db.insert_blend_comparisons_bulk([
        ("2026-09-02T00:00:00+00:00", "temperature", 1,
         "2026-09-01T00:00:00+00:00", "poll", 2.0, "{}"),
    ])
    assert db.get_storage_stats()["blend_comparison_rows"] == 1


def test_a_failed_bulk_insert_leaves_nothing_behind(db):
    good = ("ch1", _ISSUED, "2026-09-10T18:00:00+00:00",
            "temperature", 20.0, "scheduled")
    with pytest.raises(Exception):
        db.insert_forecast_snapshots_bulk([good, ("bad", "row")])
    assert db.get_storage_stats()["forecast_snapshots_rows"] == 0


def test_every_multi_statement_writer_owns_its_transaction():
    """Reachability over the fix itself: a future method that commits
    without rollback is the defect returning."""
    import inspect

    source = inspect.getsource(db_module.SwissWeatherDB)
    # _transaction() is the only place a bare commit is legitimate.
    bodies = source.split("    def ")
    offenders = [
        b.split("(")[0] for b in bodies
        if "self._conn.commit()" in b
        and "with self._transaction()" not in b
        # These three own their transaction with an explicit
        # try/except/rollback written before _transaction() existed.
        # _ensure_schema and the structural migrations run before any
        # concurrent access is possible.
        and not b.startswith((
            "_transaction", "_write_schema_version", "_ensure_schema",
            "_migrate_to_v2", "_migrate_to_v3", "_migrate_to_v4", "close",
            "apply_reconciliation_batch", "apply_blend_comparison_batch",
            "reset_all_learning",
        ))
    ]
    assert not offenders, f"methods committing without rollback: {offenders}"


# ---------------------------------------------------------------------------
# Radar integrity
# ---------------------------------------------------------------------------
def test_a_future_dated_radar_reading_is_not_fresh():
    """SWF-ICS-011. `age > LIMIT` alone let a future timestamp through,
    because a negative age is trivially below any positive limit — so the
    more wrong the timestamp, the fresher the product looked."""
    now = datetime.now(timezone.utc)
    point = model_b.RadarPointReading(
        label="local", precip_accum_mm_1h=5.0,
        valid_at=now + timedelta(hours=2), quality=10,
    )
    assert model_b._radar_point_is_usable(point, now=now) is False


def test_small_clock_skew_is_still_tolerated():
    now = datetime.now(timezone.utc)
    point = model_b.RadarPointReading(
        label="local", precip_accum_mm_1h=5.0,
        valid_at=now + RADAR_CLOCK_SKEW_TOLERANCE / 2, quality=10,
    )
    assert model_b._radar_point_is_usable(point, now=now) is True


def test_a_genuinely_stale_reading_is_still_rejected():
    now = datetime.now(timezone.utc)
    point = model_b.RadarPointReading(
        label="local", precip_accum_mm_1h=5.0,
        valid_at=now - RADAR_FRESHNESS_LIMIT - timedelta(minutes=1), quality=10,
    )
    assert model_b._radar_point_is_usable(point, now=now) is False


def test_a_non_finite_calibration_attribute_falls_back_to_the_odim_default():
    """SWF-ICS-057/066/071. NaN gain turns every pixel into NaN, and NaN
    compares False against both the nodata and undetect sentinels — so it
    would flow straight through to storage looking like a measurement."""
    assert combiprecip._finite_attr(float("nan"), "gain", default=1.0) == 1.0
    assert combiprecip._finite_attr(float("inf"), "offset", default=0.0) == 0.0
    assert combiprecip._finite_attr("not a number", "gain", default=1.0) == 1.0
    assert combiprecip._finite_attr(2.5, "gain", default=1.0) == 2.5


def test_the_accumulation_bound_is_a_decode_check_not_a_forecast_of_weather():
    """400 mm in an hour is an order of magnitude above the Swiss record.
    The bound exists to catch decode failures, and the test says so, so
    nobody later "corrects" it to a meteorologically tight value and
    starts discarding real cloudbursts."""
    assert RADAR_ACCUM_MAX_MM >= 200.0


# ---------------------------------------------------------------------------
# SWF-ICS-001 / 003 / 064 / 059 — the physical model
# ---------------------------------------------------------------------------
def test_reconfigure_offers_no_elevation_default_when_there_is_no_override():
    """**The silent 3 degC defect.**

    The field defaulted to 0.0 whenever the entry had no override, and
    reconfigure has no "clear override" control. An operator changing only
    the coordinates would submit unchanged and persist an explicit
    SEA-LEVEL override — a systematic lapse-rate error, applied to every
    temperature and then learned from.
    """
    import inspect

    source = inspect.getsource(config_flow)
    marker = source[source.index("async def async_step_reconfigure"):]
    marker = marker[:marker.index("async def ") + 1] if "async def " in marker[20:] else marker
    assert "else 0.0" not in marker.split("current_override")[-1][:400], (
        "reconfigure still defaults an absent elevation override to 0.0"
    )


def test_all_three_elevation_entry_points_use_the_same_validator():
    """SWF-ICS-003/064/059. Three ways to set one physical parameter, and
    the options flow used a bare Coerce(float) that accepts inf and nan —
    the only reachable path to the barometric overflow, since elevation
    feeds math.exp()."""
    import inspect

    source = inspect.getsource(config_flow)
    assert source.count("CONF_ELEVATION_OVERRIDE,\n                    default=current_elevation_override") == 1
    options_block = source[source.index("current_elevation_override"):]
    assert "): vol.Coerce(float)," not in options_block[:3000]


def test_every_config_flow_schema_can_be_rendered_by_home_assistant():
    """**SWF-031-004. The test that should have existed since v0.1.24.**

    Home Assistant does not hand a voluptuous schema to the frontend; it
    serialises it to JSON with `voluptuous_serialize`, which understands a
    fixed set of constructs and raises on anything else — including an
    ordinary Python function inside `vol.All`.

    From v0.1.24 the user and reconfigure steps could not be rendered at
    all. Every unit test passed, because a unit test calls the validator
    directly and never renders the form. It went unnoticed for eighteen
    releases because the only form anyone opens after installation is the
    options flow, which still used bare `vol.Coerce(float)` — until
    v0.3.1 applied the same validator there and broke it too.

    This test renders every schema the way HA does. It is the reason the
    validator is now a `vol.Coerce` subclass rather than a function.
    """
    import voluptuous_serialize

    # Every named validator in the module, found by inspection rather
    # than listed — a new validator added later is covered automatically,
    # which is the whole point. Listing them by hand is how the last one
    # got missed.
    import inspect

    validators = {
        name: obj for name, obj in vars(config_flow).items()
        if name.endswith("_VALIDATOR") or name in ("_finite_float", "_non_empty_str")
    }
    assert len(validators) >= 5, "validator discovery found suspiciously few"
    for name, validator in validators.items():
        try:
            voluptuous_serialize.convert(
                vol.Schema({vol.Optional("field"): validator})
            )
        except Exception as err:  # noqa: BLE001
            pytest.fail(
                f"{name} cannot be serialised — any form using it returns "
                f"500 to the frontend: {err}"
            )

    schemas = {
        "user/reconfigure coordinates": vol.Schema({
            vol.Required("latitude", default=47.5): config_flow._LATITUDE_VALIDATOR,
            vol.Required("longitude", default=8.9): config_flow._LONGITUDE_VALIDATOR,
            vol.Optional("elevation_override"): config_flow._ELEVATION_VALIDATOR,
        }),
        "options elevation with default": vol.Schema({
            vol.Optional("elevation_override", default=0.0):
                config_flow._ELEVATION_VALIDATOR,
        }),
        "purge days": vol.Schema({
            vol.Optional("purge_days", default=90): config_flow._PURGE_DAYS_VALIDATOR,
        }),
    }
    for label, schema in schemas.items():
        try:
            voluptuous_serialize.convert(schema)
        except Exception as err:  # noqa: BLE001
            pytest.fail(
                f"{label} cannot be rendered by Home Assistant — the form "
                f"would return 500: {err}"
            )


def test_the_finite_validator_still_rejects_what_it_was_written_for():
    """Making it serialisable must not have made it permissive. P1-27 was
    about "nan" and "inf" arriving as STRINGS from a form field."""
    for bad in ("nan", "inf", "-inf", float("nan"), float("inf")):
        with pytest.raises(vol.Invalid):
            config_flow._LATITUDE_VALIDATOR(bad)
    assert config_flow._LATITUDE_VALIDATOR("47.5") == 47.5


@pytest.mark.parametrize("bad", [float("inf"), float("nan"), 50000.0, -9999.0])
def test_the_elevation_validator_rejects_impossible_values(bad):
    with pytest.raises(vol.Invalid):
        config_flow._ELEVATION_VALIDATOR(bad)


# ---------------------------------------------------------------------------
# Attack surface
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("href", [
    "http://data.geo.admin.ch/file.h5",          # not TLS
    "https://evil.example.com/file.h5",          # wrong host
    "https://data.geo.admin.ch.evil.com/x.h5",   # trusted name as a prefix
    # Found by mutation testing: an endswith() implementation accepts
    # this one. Registering evil-data.geo.admin.ch is not possible, but
    # the check should not depend on that being true.
    "https://evil-data.geo.admin.ch/x.h5",
    "file:///etc/passwd",
    "",
])
def test_an_asset_href_outside_the_allowlist_is_refused(href):
    """SWF-ICS-009. The href is read out of a provider-supplied STAC
    document, so without this the provider chooses which host this
    integration contacts. An exact host match, because
    `data.geo.admin.ch.evil.com` ends with the trusted string."""
    with pytest.raises(ValueError):
        combiprecip._assert_trusted_asset_origin(href)


def test_the_real_meteoswiss_origin_is_accepted():
    combiprecip._assert_trusted_asset_origin(
        "https://data.geo.admin.ch/ch.meteoschweiz.ogd-radar-precip/x.h5"
    )


def test_the_download_path_actually_calls_the_origin_check():
    """Mutation testing found that deleting the CALL left every test
    green — the checks above exercise the function, not the code path
    that is supposed to use it. A guard nothing invokes is not a guard."""
    import inspect

    source = inspect.getsource(
        combiprecip.CombiPrecipClient.async_fetch_latest_bytes
    )
    code_only = "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "_assert_trusted_asset_origin(" in code_only


def test_the_radar_download_is_bounded():
    """SWF-ICS-008/026/070. This is the one client downloading a binary
    file; resp.read() allocates whatever arrives."""
    assert 0 < MAX_RADAR_DOWNLOAD_BYTES <= 128 * 1024 * 1024
    import inspect

    source = inspect.getsource(combiprecip.CombiPrecipClient.async_fetch_latest_bytes)
    assert "iter_chunked" in source, "still reading the whole body at once"
    assert "MAX_RADAR_DOWNLOAD_BYTES" in source


def test_an_enormous_diagnostic_detail_is_truncated():
    """SWF-ICS-058. The deque bounds the event COUNT; nothing bounded the
    length of one detail string."""
    from swissweather_fusion.diagnostics_recorder import DiagnosticsRecorder

    recorder = DiagnosticsRecorder(max_events=5)
    recorder.set_enabled(True)
    recorder.record(source="ch1", event_type="poll_failure", detail="x" * 500_000)
    stored = recorder.get_events()[0]["detail"]
    assert len(stored) < MAX_DIAGNOSTIC_DETAIL_CHARS + 100
    assert "truncated" in stored


# ---------------------------------------------------------------------------
# Structural
# ---------------------------------------------------------------------------
def test_every_constructed_coordinator_appears_in_a_cleanup_set():
    """SWF-ICS-028, and the reason it happened.

    The comment above that tuple has claimed "EVERY already-constructed
    coordinator" since v0.1.24 and was true when written. v0.2.6 added
    wetteralarm_coordinator and did not update the tuple, silently
    falsifying it. A comment asserting completeness is not a mechanism.
    """
    import inspect

    import swissweather_fusion

    source = inspect.getsource(swissweather_fusion.async_setup_entry)
    constructed = set(re_findall_assignments(source))
    cleanup_block = source[source.index("source_coordinators = ("):]
    cleanup_block = cleanup_block[:cleanup_block.index("results = await")]
    # **Comments are stripped before the check.**
    #
    # Found by mutation testing: removing wetteralarm_coordinator from the
    # tuple left this test green, because the v0.3.1 comment explaining
    # SWF-ICS-028 sits inside the same block and contains the name. The
    # test was reading the explanation of the bug as evidence the bug was
    # fixed — a reachability test defeated by its own documentation.
    code_only = "\n".join(
        line for line in cleanup_block.splitlines()
        if not line.lstrip().startswith("#")
    )
    missing = [c for c in sorted(constructed) if c not in code_only]
    assert not missing, f"coordinators never shut down on setup failure: {missing}"


def re_findall_assignments(source):
    import re

    return {
        m.group(1)
        for m in re.finditer(r"^\s{4,}(\w+_coordinator) = \w+Coordinator\(",
                             source, re.MULTILINE)
    }


def test_reconciliation_status_is_validated_without_assert(db):
    """SWF-ICS-017. `python -O` strips assert entirely. Nobody runs Home
    Assistant that way, so this was never live — but a validation whose
    existence depends on an interpreter flag is not a validation."""
    import inspect

    source = inspect.getsource(db_module.SwissWeatherDB)
    assert "assert status in" not in source
    with pytest.raises(ValueError):
        db.mark_forecast_snapshots_status(ids=[1], status="nonsense")


def test_a_future_station_observation_is_never_ground_truth():
    """SWF-ICS-043. The query window deliberately extends past `now`, so
    a clock-skewed row can land inside it. Model B guarded this; Model A
    did not."""
    import inspect

    source = inspect.getsource(coord.ModelALearningCoordinator._reconcile)
    marker = source[source.index("for row in station_rows:"):]
    assert "if ts > now:" in marker[:1400]


def test_the_blend_never_emits_a_non_finite_value():
    """SWF-ICS-069/076. A randomized campaign found finite inputs
    producing -inf: values near 1e306 times a large learned weight
    overflow during accumulation. Input validation cannot catch it —
    every input was finite."""
    contributions = [
        model_a.SourceContribution(
            source="ch1", raw_value=1.8e306, ema_bias=0.0,
            ema_weight=1.8e306, sample_count=99,
        ),
        model_a.SourceContribution(
            source="ch2", raw_value=1.8e306, ema_bias=0.0,
            ema_weight=1.8e306, sample_count=99,
        ),
    ]
    result = model_a.blend(contributions)
    assert result is None or math.isfinite(result)


def test_the_final_gate_catches_an_overflow_the_staged_guards_miss():
    """Mutation testing found the final `isfinite(result)` check was
    untested: every fixture was already stopped by an earlier stage.

    Here each individual term is finite, and it is their ACCUMULATION
    that overflows — which is precisely the case the staged guards
    cannot see, because they inspect one contribution at a time.
    """
    big = 1e308
    contributions = [
        model_a.SourceContribution(
            source=f"s{i}", raw_value=big, ema_bias=0.0,
            ema_weight=1.0, sample_count=99,
        )
        for i in range(8)
    ]
    result = model_a.blend(contributions)
    assert result is None or math.isfinite(result)


@pytest.mark.parametrize("bias,weight", [
    (float("nan"), 1.0), (float("inf"), 1.0), (0.0, float("nan")),
    (0.0, float("inf")), (0.0, -1.0),
])
def test_corrupted_learned_state_cannot_poison_the_blend(bias, weight):
    """The realistic path: ema_bias and ema_weight are read back from
    SQLite and have never been range-checked on load."""
    good = model_a.SourceContribution(
        source="ch1", raw_value=20.0, ema_bias=0.0, ema_weight=1.0,
        sample_count=99,
    )
    bad = model_a.SourceContribution(
        source="ch2", raw_value=21.0, ema_bias=bias, ema_weight=weight,
        sample_count=99,
    )
    result = model_a.blend([good, bad])
    assert result is None or math.isfinite(result)
