"""Regression tests for v0.1.28.

**The theme of this release: fixtures taken from reality, not from
documentation.**

The CombiPrecip outage that prompted it happened because v0.1.24 built a
filename contract out of MeteoSwiss's published naming convention —
written in uppercase — and never checked it against a real response,
where the files are lowercase. Every test passed. Every real poll failed.

So the STAC fixtures below are copied verbatim from a live capture of
``data.geo.admin.ch`` on 2 September 2026, lowercase filenames, real
quality codes, real duplicate product times and all. Where a test needs
a value that could drift, it says so.

Covers SWF-P1-004 (filename casing), SWF-P1-005 (item selection),
SWF-P1-006 (coordinate-pair leak), SWF-P1-007 (accuracy sensor),
SWF-P2-005 (clear-night) and SWF-P2-006 (Meteonomiqs schedule).
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from swissweather_fusion import redaction
from swissweather_fusion.clients import combiprecip as cp
from swissweather_fusion.models import model_a


# ---------------------------------------------------------------------------
# Verbatim capture from https://data.geo.admin.ch/api/stac/v1/collections/
# ch.meteoschweiz.ogd-radar-precip/items — 2 September 2026.
# ---------------------------------------------------------------------------
BASE = "https://data.geo.admin.ch/ch.meteoschweiz.ogd-radar-precip/20260819-ch"
REAL_ITEM = {
    "id": "20260819-ch",
    "collection": "ch.meteoschweiz.ogd-radar-precip",
    "properties": {
        # NOTE: this is 2 September while the item holds 19 August data.
        # Not a typo — it is the defect SWF-P1-005 is about.
        "datetime": "2026-09-02T04:00:21.527277Z",
        "title": "CH at 19.08.2026",
    },
    "assets": {
        "cpc2623100000_00060.001.h5": {"href": f"{BASE}/cpc2623100000_00060.001.h5"},
        "cpc2623122004_00060.001.h5": {"href": f"{BASE}/cpc2623122004_00060.001.h5"},
        "cpc2623122005_00060.001.h5": {"href": f"{BASE}/cpc2623122005_00060.001.h5"},
        "cpc2623123059_00060.001.h5": {"href": f"{BASE}/cpc2623123059_00060.001.h5"},
        "rzc262310000vl.001.h5": {"href": f"{BASE}/rzc262310000vl.001.h5"},
        "rzc262310005vl.001.h5": {"href": f"{BASE}/rzc262310005vl.001.h5"},
    },
}


# ---------------------------------------------------------------------------
# SWF-P1-004 — lowercase filenames
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "href,expected_hour,expected_minute,expected_quality",
    [
        (f"{BASE}/cpc2623100000_00060.001.h5", 0, 0, 0),
        (f"{BASE}/cpc2623122004_00060.001.h5", 22, 0, 4),
        (f"{BASE}/cpc2623122005_00060.001.h5", 22, 0, 5),
        (f"{BASE}/cpc2623123059_00060.001.h5", 23, 5, 9),
    ],
)
def test_real_lowercase_cpc_filenames_parse(
    href, expected_hour, expected_minute, expected_quality
):
    """The regression gate for the outage.

    v0.1.24 encoded MeteoSwiss's documented uppercase convention
    literally into a case-sensitive regex. The API serves lowercase, so
    every genuine CombiPrecip file was rejected and the client raised
    "No CombiPrecip assets found in STAC response" on every poll — 56
    consecutive failures in the reporting installation.
    """
    parsed = cp.parse_cpc_filename(href)
    assert parsed is not None, "a real CombiPrecip filename was rejected"
    product_time, quality, accumulation = parsed
    assert product_time.hour == expected_hour
    assert product_time.minute == expected_minute
    assert quality == expected_quality
    assert accumulation == "00060"


def test_real_lowercase_rzc_files_are_still_rejected():
    """Case-insensitivity must not weaken the product filter: RZC is an
    instantaneous mm/h rate, a different physical quantity from CPC's
    hourly accumulation (SWF/IND-13)."""
    assert cp.parse_cpc_filename(f"{BASE}/rzc262310000vl.001.h5") is None
    assert cp.parse_cpc_filename(f"{BASE}/tzc262310000vl.001.h5") is None


def test_uppercase_filenames_would_also_parse():
    """Case-INsensitive, not case-swapped. If MeteoSwiss ever serves the
    documented uppercase form, that must keep working too."""
    assert cp.parse_cpc_filename("CPC2623123059_00060.001.h5") is not None


def test_real_item_yields_only_cpc_assets_newest_and_best_quality_first():
    assets = cp.parse_stac_items_response({"features": [REAL_ITEM]})

    assert len(assets) == 4, "RZC assets leaked into the CPC selection"
    assert assets[0].href.endswith("cpc2623123059_00060.001.h5")
    assert assets[0].valid_at.hour == 23 and assets[0].valid_at.minute == 5

    # 2200 exists twice with quality 4 and 5 — the documented
    # quality-flag-change case. Better quality must win the tie, and
    # neither may raise.
    at_2200 = [a for a in assets if a.valid_at.hour == 22]
    assert len(at_2200) == 2
    assert at_2200[0].quality == 5


def test_product_time_comes_from_filename_not_properties_datetime():
    """properties.datetime on this real item is 2 September; the data is
    19 August. Anything deriving a timestamp from it is wrong."""
    assets = cp.parse_stac_items_response({"features": [REAL_ITEM]})
    assert all(a.valid_at.month == 8 and a.valid_at.day == 19 for a in assets)


# ---------------------------------------------------------------------------
# SWF-P1-005 — item selection
# ---------------------------------------------------------------------------
def test_item_url_is_addressed_by_date_stamped_id():
    url = cp.stac_item_url_for_date(date(2026, 9, 2))
    assert url.endswith("/items/20260902-ch")
    assert cp.STAC_COLLECTION in url


def test_item_url_zero_pads_single_digit_months_and_days():
    assert cp.stac_item_url_for_date(date(2026, 1, 5)).endswith("/items/20260105-ch")


def test_a_single_item_feature_is_accepted():
    """Requesting one item returns a bare Feature, not a
    FeatureCollection. The fetch path wraps it; this pins that the
    parser handles the wrapped shape."""
    assets = cp.parse_stac_items_response({"features": [REAL_ITEM]})
    assert assets


def test_an_empty_item_yields_no_assets_rather_than_raising():
    """MeteoSwiss pre-creates the next day's item, empty, and it fills
    from midnight. The fetch path falls back to the previous day; the
    parser must simply return nothing rather than raise."""
    empty = dict(REAL_ITEM, assets={})
    assert cp.parse_stac_items_response({"features": [empty]}) == []


# ---------------------------------------------------------------------------
# SWF-P1-006 — coordinate pair leak in diagnostics
# ---------------------------------------------------------------------------
def test_provider_grid_coordinates_are_redacted_even_though_they_differ():
    """The leak found in a real diagnostics export.

    redact_coordinate_strings substitutes the CONFIGURED coordinates, and
    was written specifically to catch SRF's geolocationId. It does — but
    SRF resolves to its OWN nearest grid point and returns that, which is
    a different number roughly a kilometre away. Key-based redaction
    missed it too (the key is "id"), so a household's location was
    written verbatim into a file intended for sharing.
    """
    payload = {
        "detail": "SRF geolocation lookup resolved to id='47.5536,8.9120'",
    }
    result = str(
        redaction.redact_diagnostic_payload(
            payload, latitude=47.5541, longitude=8.9098
        )
    )
    assert "47.5536" not in result
    assert "8.9120" not in result


def test_configured_coordinates_still_get_the_specific_markers():
    """The value-based pass runs first, so where it applies its more
    specific markers survive."""
    payload = {"detail": "id='47.5536,8.9120'"}
    result = str(
        redaction.redact_diagnostic_payload(
            payload, latitude=47.5536, longitude=8.9120
        )
    )
    assert "LAT_REDACTED" in result


@pytest.mark.parametrize(
    "text",
    [
        "temperature 21.5 and humidity 64.0",
        "pressure 1023.5 hPa",
        "duration 91.61889599636197 ms",
    ],
)
def test_ordinary_weather_values_are_not_redacted_as_coordinates(text):
    """The shape rule must not eat legitimate telemetry. A comma-joined
    decimal pair is the signature; individual numbers are not."""
    result = str(
        redaction.redact_diagnostic_payload(
            {"d": text}, latitude=47.5541, longitude=8.9098
        )
    )
    assert "COORDINATE_PAIR_REDACTED" not in result


def test_coordinate_pair_redaction_handles_whitespace_and_signs():
    assert "COORDINATE_PAIR" in redaction.redact_coordinate_pairs("46.94, 7.44")
    assert "COORDINATE_PAIR" in redaction.redact_coordinate_pairs("-46.94,-7.44")


# ---------------------------------------------------------------------------
# SWF-P2-005 — clear-night
# ---------------------------------------------------------------------------
def test_clear_sky_at_night_is_clear_night_not_sunny():
    """Home Assistant treats "sunny" and "clear-night" as distinct
    conditions and performs no automatic substitution — the integration
    must emit the right one. Emitting "sunny" drew a bright sun icon at
    02:00 in the hourly forecast."""
    assert model_a.derive_condition(0.0, 10.0, 40.0, is_daytime=False) == "clear-night"


def test_clear_sky_in_daytime_is_still_sunny():
    assert model_a.derive_condition(0.0, 20.0, 40.0, is_daytime=True) == "sunny"


def test_unknown_daytime_keeps_the_previous_behaviour():
    """None means "caller doesn't know" — a wrong clear-night at noon
    would be more conspicuous than the bug being fixed."""
    assert model_a.derive_condition(0.0, 20.0, 40.0) == "sunny"


@pytest.mark.parametrize(
    "precip,temp,humidity,expected",
    [
        (2.0, 10.0, 90.0, "rainy"),
        (2.0, -3.0, 90.0, "snowy"),
        (0.0, 5.0, 95.0, "cloudy"),
    ],
)
def test_only_the_sunny_branch_gains_a_night_variant(precip, temp, humidity, expected):
    """Rain at night is still "rainy". Home Assistant has no night
    variants for these, and inventing them would claim precision the
    model does not have."""
    assert (
        model_a.derive_condition(precip, temp, humidity, is_daytime=False) == expected
    )


def test_twice_daily_aggregation_marks_its_night_period_as_night():
    """The aggregation already knows which half of the day each period
    covers, so its night row must not report a sun."""
    now = datetime(2026, 9, 2, 0, 0, tzinfo=timezone.utc)
    entries = []
    for hour in range(48):
        entries.append(
            {
                "datetime": (now + timedelta(hours=hour)).isoformat(),
                "native_temperature": 15.0,
                "humidity": 40.0,
                "native_precipitation": 0.0,
            }
        )
    periods = model_a.aggregate_twice_daily_forecast(entries, local_tz=timezone.utc)
    night = [p for p in periods if not p["is_daytime"]]
    assert night, "no night period produced"
    assert all(p["condition"] == "clear-night" for p in night)


# ---------------------------------------------------------------------------
# SWF-P2-006 — Meteonomiqs noon schedule
# ---------------------------------------------------------------------------
def test_meteonomiqs_checks_hourly_so_the_noon_call_lands_near_noon():
    """const.py states the seasonal call happens "at local noon", and the
    hour was a meteorological choice. Noon was only ever a GATE, while
    the coordinator woke every 6 hours from Home Assistant start-up — so
    the real call time was "first check after noon", drifting to as late
    as ~18:00 and changing on every restart. A reporting installation
    started at 08:14 and called at 14:14.

    Hourly checking bounds the error to under an hour. The daily gate
    means the other 23 checks return without touching the network, and
    since v0.1.27 without reserving quota either.
    """
    from swissweather_fusion.coordinator import MeteonomiqsCoordinator

    assert MeteonomiqsCoordinator.CHECK_INTERVAL <= timedelta(hours=1)


def test_hourly_checks_still_guarantee_at_least_one_post_noon_check_daily():
    """Whatever time Home Assistant starts, at least one check must land
    in the noon-to-midnight window."""
    interval_hours = 1
    for start_hour in range(24):
        checks = [(start_hour + n * interval_hours) % 24 for n in range(24)]
        assert any(h >= 12 for h in checks), f"no post-noon check for start {start_hour}"


# ---------------------------------------------------------------------------
# SWF-P1-007 — forecast accuracy sensor
# ---------------------------------------------------------------------------
# The old test for this asserted only "None when nothing is learned".
# That is indistinguishable from "None because it crashed", which is
# exactly what was happening — so the test passed for four releases
# against a sensor that could never produce a number. These assert a
# real computed value from seeded buckets.
def _learning_coordinator(db):
    import asyncio

    from swissweather_fusion import coordinator as coord

    class Hass:
        def __init__(self):
            self.data = {}

        async def async_add_executor_job(self, func, *args):
            return func(*args)

    c = coord.ModelALearningCoordinator(Hass(), db, reconcile_lock=asyncio.Lock())
    return c


def _seed_bucket(db, *, source, measurement, ema_abs_error, sample_count, hour=12):
    from swissweather_fusion.storage.db import BucketKey

    key = BucketKey(
        hour_of_day=hour,
        season="summer",
        lead_time_bucket="short",
        source=source,
        measurement=measurement,
    )
    db.apply_reconciliation_batch(
        [(key, 0.0, ema_abs_error, 1.0, sample_count, "2026-09-02T12:00:00+00:00")],
        [],
        [],
    )


def test_temperature_mae_is_a_real_sample_weighted_number(tmp_path):
    """Two buckets with known errors and known weights, and the exact
    expected average — not merely "not None"."""
    from swissweather_fusion.storage.db import SwissWeatherDB

    db = SwissWeatherDB(str(tmp_path / "mae.db"))
    try:
        _seed_bucket(db, source="ch1", measurement="temperature",
                     ema_abs_error=1.0, sample_count=100, hour=12)
        _seed_bucket(db, source="ch2", measurement="temperature",
                     ema_abs_error=2.0, sample_count=300, hour=13)

        result = _learning_coordinator(db)._compute_temperature_mae()

        # (1.0*100 + 2.0*300) / 400 = 1.75
        assert result["value"] == pytest.approx(1.75)
        assert result["bucket_count"] == 2
        assert result["sample_count"] == 400
    finally:
        db.close()


def test_temperature_mae_ignores_non_temperature_buckets(tmp_path):
    from swissweather_fusion.storage.db import SwissWeatherDB

    db = SwissWeatherDB(str(tmp_path / "mae2.db"))
    try:
        _seed_bucket(db, source="ch1", measurement="temperature",
                     ema_abs_error=1.0, sample_count=10)
        _seed_bucket(db, source="ch1", measurement="pressure",
                     ema_abs_error=99.0, sample_count=1000, hour=13)

        result = _learning_coordinator(db)._compute_temperature_mae()
        assert result["value"] == pytest.approx(1.0)
        assert result["bucket_count"] == 1
    finally:
        db.close()


def test_temperature_mae_is_none_only_when_nothing_is_learned(tmp_path):
    from swissweather_fusion.storage.db import SwissWeatherDB

    db = SwissWeatherDB(str(tmp_path / "mae3.db"))
    try:
        assert _learning_coordinator(db)._compute_temperature_mae() is None
    finally:
        db.close()


def test_accuracy_sensor_reads_the_cache_and_never_touches_the_database():
    """The other half of the defect: native_value queried SQLite from a
    property, which Home Assistant polls on the event loop every ~30s —
    the same blocking-I/O class as the manifest read v0.1.25 shipped."""
    import inspect

    from swissweather_fusion.sensor import ForecastAccuracySensor

    # Inspect the CODE, not the docstring — the class docstring
    # deliberately describes the old bug, so a naive substring search
    # over the whole source matches its own explanation.
    code = "".join(
        line
        for line in inspect.getsource(ForecastAccuracySensor).splitlines(True)
        if not line.strip().startswith("#")
    )
    body = code.split('"""')[-1]
    assert "self._db" not in body, "the sensor still holds a database handle"
    assert "except Exception" not in body, (
        "a blanket except would hide the next breakage exactly as it hid this one"
    )

    sensor = object.__new__(ForecastAccuracySensor)
    sensor._runtime = {
        "learning_coordinator": type(
            "C", (), {"temperature_mae": {"value": 1.75, "bucket_count": 2,
                                          "sample_count": 400}}
        )()
    }
    assert ForecastAccuracySensor.native_value.fget(sensor) == pytest.approx(1.75)
    attrs = ForecastAccuracySensor.extra_state_attributes.fget(sensor)
    assert attrs["total_sample_count"] == 400


def test_accuracy_sensor_is_blank_but_stable_before_the_first_learning_run():
    from swissweather_fusion.sensor import ForecastAccuracySensor

    sensor = object.__new__(ForecastAccuracySensor)
    sensor._runtime = {}
    assert ForecastAccuracySensor.native_value.fget(sensor) is None
    assert ForecastAccuracySensor.extra_state_attributes.fget(sensor)[
        "temperature_bucket_count"
    ] == 0
