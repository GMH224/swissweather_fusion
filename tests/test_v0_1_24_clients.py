"""Regression tests for v0.1.24's client-layer fixes.

IND-13 / P1-17 (CombiPrecip product identity), P1-16 (quality code),
P1-18 (out-of-grid), P1-19 (poll cadence — a REFUTED audit finding,
pinned so it cannot be "fixed" again), P1-10 (meteoblue run identity),
P1-24 (array mismatch) and P1-25 (timezone handling).
"""
from datetime import timedelta

import pytest

from swissweather_fusion.clients import combiprecip as cp
from swissweather_fusion.clients import meteoblue as mb
from swissweather_fusion.clients import open_meteo as om
from swissweather_fusion.const import COMBIPRECIP_POLL_INTERVAL


# ---------------------------------------------------------------------------
# IND-13 / P1-17 — product identity
# ---------------------------------------------------------------------------
def _feature(*hrefs, collection=None):
    return {
        "collection": collection if collection is not None else cp.STAC_COLLECTION,
        "properties": {"datetime": "2026-07-25T00:00:00Z"},
        "assets": {f"a{i}": {"href": h} for i, h in enumerate(hrefs)},
    }


def test_only_combiprecip_files_are_selected_from_a_mixed_collection():
    """The defect this test exists for.

    ch.meteoschweiz.ogd-radar-precip carries several products side by
    side: RZC (PRECIP, instantaneous mm/h), TZC (PRECIP-SV, also mm/h)
    and CPC (CombiPrecip, a 1-hour accumulation in mm). The old selector
    accepted ANY href ending in ".h5", so this client could and probably
    did download and interpret a product it was not written for — an
    instantaneous rate scored against a threshold meant for an hourly
    accumulation.
    """
    payload = {
        "features": [
            _feature(
                "https://x/RZC262061205VL.801.h5",
                "https://x/TZC2620612050.801.h5",
                "https://x/CPC2620612009_00060.801.h5",
            )
        ]
    }
    assets = cp.parse_stac_items_response(payload)
    assert len(assets) == 1
    assert "CPC" in assets[0].href


def test_multiple_h5_assets_are_normal_and_must_not_raise():
    """MeteoSwiss documents that when the quality flag changes, the file
    name changes and a SECOND file is produced rather than the first
    being overwritten. Multiple valid .h5 assets per item is therefore
    the documented NORMAL case.

    This is why treating "more than one .h5 asset" as an ambiguity error
    — as was at one point proposed — would have converted a silent
    wrong-product bug into a permanent hard outage of the radar source.
    """
    payload = {
        "features": [
            _feature(
                "https://x/CPC2620612005_00060.801.h5",
                "https://x/CPC2620612009_00060.801.h5",
            )
        ]
    }
    assets = cp.parse_stac_items_response(payload)
    assert len(assets) == 2
    # Better quality code wins the tie at the same product time.
    assert assets[0].quality == 9


def test_product_time_comes_from_the_filename_not_the_stac_datetime():
    """STAC items here are per CALENDAR DATE, so every asset in an item
    shares the same properties.datetime. Sorting on that field
    degenerated to "an arbitrary file from the newest day" rather than
    "the latest scan"."""
    payload = {
        "features": [
            _feature(
                "https://x/CPC2620608309_00060.801.h5",
                "https://x/CPC2620617459_00060.801.h5",
            )
        ]
    }
    assets = cp.parse_stac_items_response(payload)
    assert assets[0].valid_at.hour == 17 and assets[0].valid_at.minute == 45
    assert assets[1].valid_at.hour == 8


def test_wrong_accumulation_window_is_skipped():
    """A CPC file for a different accumulation window is not the product
    this client interprets."""
    payload = {"features": [_feature("https://x/CPC2620612009_00180.801.h5")]}
    assert cp.parse_stac_items_response(payload) == []


def test_features_from_a_different_collection_are_skipped():
    payload = {
        "features": [
            _feature("https://x/CPC2620612009_00060.801.h5", collection="some.other")
        ]
    }
    assert cp.parse_stac_items_response(payload) == []


def test_unrecognised_filenames_are_skipped_not_fatal():
    payload = {"features": [_feature("https://x/whatever.h5")]}
    assert cp.parse_stac_items_response(payload) == []


def test_parse_cpc_filename_extracts_time_quality_and_accumulation():
    parsed = cp.parse_cpc_filename("https://x/CPC2620612057_00060.801.h5")
    assert parsed is not None
    product_time, quality, accumulation = parsed
    assert product_time.year == 2026
    assert product_time.hour == 12 and product_time.minute == 5
    assert quality == 7
    assert accumulation == "00060"


def test_parse_cpc_filename_rejects_other_products():
    assert cp.parse_cpc_filename("https://x/RZC262061205VL.801.h5") is None


def test_parse_cpc_filename_rejects_impossible_values():
    """A malformed day-of-year or clock time must be rejected rather than
    producing a plausible-looking wrong timestamp."""
    assert cp.parse_cpc_filename("https://x/CPC2699912009_00060.801.h5") is None
    assert cp.parse_cpc_filename("https://x/CPC2620699999_00060.801.h5") is None


# ---------------------------------------------------------------------------
# P1-19 — REFUTED audit finding, pinned
# ---------------------------------------------------------------------------
def test_combiprecip_poll_interval_matches_documented_product_cadence():
    """The external audit claimed MeteoSwiss publishes CombiPrecip on a
    ~10-minute cycle and that polling at 5 minutes wasted requests. It
    does not: MeteoSwiss's own open-data documentation lists the update
    frequency for the CombiPrecip 60-minute-total product (CPC) as 5
    MINUTES, the same as PRECIP (RZC) and PRECIP-SV (TZC). The "60
    minute" in the product name is the ACCUMULATION WINDOW, not the
    publication interval — the two appear to have been conflated.

    Pinned here because the proposed "fix" would have halved the radar
    update rate for no benefit AND corrupted RADAR_FRESHNESS_LIMIT, which
    is derived from this value.
    """
    assert COMBIPRECIP_POLL_INTERVAL == timedelta(minutes=5)


# ---------------------------------------------------------------------------
# P1-10 — meteoblue run identity
# ---------------------------------------------------------------------------
def _mb_payload(run=None, temps=(20.0,), times=("2026-07-25 07:00",)):
    payload = {"metadata": {"height": 500}, "data_1h": {"time": list(times),
                                                        "temperature": list(temps)}}
    if run is not None:
        payload["metadata"]["modelrun_updatetime_utc"] = run
    return payload


def test_fingerprint_differs_for_distinct_runs_with_identical_values():
    """The original defect: fingerprint_points() hashes only the returned
    series, so a genuinely NEW model run producing identical values —
    entirely plausible during a stable weather pattern — collided with
    the previous one and was discarded as a duplicate, silently losing a
    real independent training sample."""
    a = mb.parse_forecast_response(_mb_payload(run="2026-07-25T06:00:00Z"))
    b = mb.parse_forecast_response(_mb_payload(run="2026-07-25T12:00:00Z"))
    assert a.run_fingerprint != b.run_fingerprint
    # The content-hash half still agrees, confirming the mechanism is
    # run identity rather than an incidental difference.
    assert a.run_fingerprint.split("|")[1] == b.run_fingerprint.split("|")[1]


def test_fingerprint_matches_for_the_same_run_polled_twice():
    a = mb.parse_forecast_response(_mb_payload(run="2026-07-25T06:00:00Z"))
    b = mb.parse_forecast_response(_mb_payload(run="2026-07-25T06:00:00Z"))
    assert a.run_fingerprint == b.run_fingerprint


def test_fingerprint_falls_back_to_content_hash_when_run_identity_missing():
    """Without a real identifier, issued_at is datetime.now() — which
    changes on every call and would defeat deduplication entirely if
    embedded."""
    a = mb.parse_forecast_response(_mb_payload(run=None))
    b = mb.parse_forecast_response(_mb_payload(run=None))
    assert "|" not in a.run_fingerprint
    assert a.run_fingerprint == b.run_fingerprint


# ---------------------------------------------------------------------------
# P1-24 — array length mismatch
# ---------------------------------------------------------------------------
def test_parse_forecast_response_records_array_length_mismatch():
    """zip() truncates to the shorter sequence with no trace. Open-Meteo
    has tracked this since v0.1.19; meteoblue had no equivalent."""
    parsed = mb.parse_forecast_response(
        _mb_payload(times=("2026-07-25 07:00", "2026-07-25 08:00"), temps=(20.0,))
    )
    assert parsed.array_length_mismatches
    assert "temperature" in parsed.array_length_mismatches[0]


def test_parse_forecast_response_no_mismatch_when_arrays_agree():
    parsed = mb.parse_forecast_response(_mb_payload())
    assert parsed.array_length_mismatches == ()


# ---------------------------------------------------------------------------
# P1-25 — timezone handling
# ---------------------------------------------------------------------------
def test_meteoblue_parses_aware_offset_by_converting_not_relabelling():
    """.replace(tzinfo=utc) is correct only for NAIVE input. On aware
    input it overwrites the label without converting, so "14:00+02:00"
    silently became "14:00+00:00" — the same wall clock, two hours off
    the actual instant."""
    parsed = mb.parse_forecast_response(
        {"metadata": {"modelrun_updatetime_utc": "2026-07-25T14:00:00+02:00"},
         "data_1h": {"time": ["2026-07-25 07:00"], "temperature": [20.0]}}
    )
    assert parsed.issued_at.hour == 12


def test_meteoblue_naive_timestamp_still_labelled_utc():
    """The common case must be unchanged."""
    parsed = mb.parse_forecast_response(_mb_payload(run="2026-07-25T14:00:00"))
    assert parsed.issued_at.hour == 14


def test_open_meteo_parses_aware_offset_by_converting_not_relabelling():
    parsed = om.parse_forecast_response(
        {"hourly": {"time": ["2026-07-25T14:00:00+02:00"], "temperature_2m": [20.0]}}
    )
    assert parsed.points[0].valid_at.hour == 12


def test_open_meteo_naive_timestamp_still_labelled_utc():
    parsed = om.parse_forecast_response(
        {"hourly": {"time": ["2026-07-25T14:00"], "temperature_2m": [20.0]}}
    )
    assert parsed.points[0].valid_at.hour == 14
