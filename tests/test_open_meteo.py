import pytest

from swissweather_fusion.clients import open_meteo as om

# Generic placeholder coordinates throughout — never the real deployment
# location, per this project's confidentiality requirement.
TEST_LAT, TEST_LON = 46.9480, 7.4474


def test_build_forecast_url():
    url = om.build_forecast_url(source="ch1", latitude=TEST_LAT, longitude=TEST_LON)
    # v0.1.1: was icon_ch1_eps — a plausible-looking guess never checked
    # against Open-Meteo's actual docs, which caused every CH1 request to
    # 400 in the first real deployment. Confirmed correct value below.
    assert "models=meteoswiss_icon_ch1" in url
    assert f"latitude={TEST_LAT}" in url


def test_build_forecast_url_all_three_models_use_confirmed_identifiers():
    ch2_url = om.build_forecast_url(source="ch2", latitude=TEST_LAT, longitude=TEST_LON)
    d2_url = om.build_forecast_url(source="icon_d2", latitude=TEST_LAT, longitude=TEST_LON)
    assert "models=meteoswiss_icon_ch2" in ch2_url
    # dwd_icon_d2 was already correct before v0.1.1 — confirms it wasn't
    # touched by the CH1/CH2 fix.
    assert "models=dwd_icon_d2" in d2_url


def test_extract_error_reason():
    error_payload = {
        "error": True,
        "reason": "Cannot initialize model from invalid String value icon_ch1_eps for key models",
    }
    assert om.extract_error_reason(error_payload) == (
        "Cannot initialize model from invalid String value icon_ch1_eps for key models"
    )
    assert om.extract_error_reason({"error": False}) is None
    assert om.extract_error_reason({}) is None


def test_build_forecast_url_rejects_unknown_source():
    with pytest.raises(ValueError):
        om.build_forecast_url(source="bogus", latitude=0, longitude=0)


def test_build_forecast_url_free_tier_uses_default_host_no_key():
    url = om.build_forecast_url(source="ch1", latitude=TEST_LAT, longitude=TEST_LON)
    assert url.startswith("https://api.open-meteo.com/")
    assert "customer-" not in url
    assert "apikey=" not in url


def test_build_forecast_url_with_api_key_uses_customer_host():
    """v0.1.3: optional paid-tier API key. Confirmed from Open-Meteo's own
    docs that using a key requires the customer- prefixed hostname, not
    just adding the parameter to the regular one.
    """
    url = om.build_forecast_url(
        source="ch1", latitude=TEST_LAT, longitude=TEST_LON, api_key="TESTKEY"
    )
    assert url.startswith("https://customer-api.open-meteo.com/")
    assert "apikey=TESTKEY" in url


def test_build_elevation_url_with_and_without_api_key():
    free_url = om.build_elevation_url(latitude=TEST_LAT, longitude=TEST_LON)
    assert free_url.startswith("https://api.open-meteo.com/")
    assert "apikey=" not in free_url

    paid_url = om.build_elevation_url(latitude=TEST_LAT, longitude=TEST_LON, api_key="TESTKEY")
    assert paid_url.startswith("https://customer-api.open-meteo.com/")
    assert "apikey=TESTKEY" in paid_url


def test_build_elevation_url():
    url = om.build_elevation_url(latitude=TEST_LAT, longitude=TEST_LON)
    assert "elevation" in url


def test_parse_forecast_response():
    payload = {
        "hourly": {
            "time": ["2026-07-25T12:00", "2026-07-25T13:00"],
            "temperature_2m": [20.1, 21.3],
            "relative_humidity_2m": [55, 52],
            "pressure_msl": [1013.2, 1013.0],
            "precipitation": [0.0, 0.2],
            "wind_speed_10m": [3.1, 3.4],
        }
    }
    parsed = om.parse_forecast_response(payload)
    assert len(parsed.points) == 10  # 5 variables x 2 timesteps
    temps = [p for p in parsed.points if p.variable == "temperature"]
    assert len(temps) == 2
    assert temps[0].value == 20.1


def test_parse_forecast_response_extracts_grid_elevation():
    """v0.1.15: confirmed against Open-Meteo's real documented response
    shape (a top-level "elevation" field, not per-hour) — this is what
    makes apply_lapse_rate_precorrection usable at all, since it needs
    the grid cell's own elevation to compare against the configured
    actual elevation. An outside code review found the correction
    function existed but was never wired into anything; this is the
    piece of data that wiring needed.
    """
    payload = {
        "elevation": 44.812,
        "hourly": {"time": ["2026-07-25T12:00"], "temperature_2m": [20.0]},
    }
    parsed = om.parse_forecast_response(payload)
    assert parsed.grid_elevation_m == 44.812


def test_parse_forecast_response_missing_elevation_defaults_none():
    """Should never crash if a response happens to omit this field —
    the coordinator treats None as "skip the correction", not an error.
    """
    payload = {"hourly": {"time": ["2026-07-25T12:00"], "temperature_2m": [20.0]}}
    parsed = om.parse_forecast_response(payload)
    assert parsed.grid_elevation_m is None


def test_pressure_requests_sea_level_not_surface():
    """v0.1.2 regression test: requesting surface_pressure instead of
    pressure_msl silently mixed two different physical quantities across
    sources (surface pressure differs from sea-level pressure by ~12 hPa
    per 100m elevation) — a real deployment showed a suspiciously low
    966.2 hPa blended reading that matched uncorrected surface pressure
    almost exactly. Confirms the fix and guards against it regressing.
    """
    url = om.build_forecast_url(source="ch1", latitude=TEST_LAT, longitude=TEST_LON)
    assert "pressure_msl" in url
    assert "surface_pressure" not in url

    payload = {
        "hourly": {
            "time": ["2026-07-25T12:00"],
            "pressure_msl": [1013.2],
        }
    }
    parsed = om.parse_forecast_response(payload)
    pressures = [p for p in parsed.points if p.variable == "pressure"]
    assert len(pressures) == 1
    assert pressures[0].value == 1013.2


def test_wind_speed_requests_meters_per_second_not_kmh():
    """v0.1.5 regression test: Open-Meteo defaults wind speed to km/h, but
    meteoblue's confirmed test response used values (0.94, 1.85, etc.)
    consistent with m/s — a real cross-source unit mismatch that would
    have become visible the moment wind speed was actually exposed on the
    weather card (it previously flowed into Model A's blend unused).
    """
    url = om.build_forecast_url(source="ch1", latitude=TEST_LAT, longitude=TEST_LON)
    assert "wind_speed_unit=ms" in url


def test_parse_forecast_response_flags_array_length_mismatch():
    """v0.1.19 regression test: before this fix, zip(times, values)
    silently truncated to the shorter array with no signal anywhere —
    a provider regression or malformed/partial response looked exactly
    like a normal, slightly-short forecast. Confirms a length mismatch
    is now recorded on the parsed result.
    """
    payload = {
        "hourly": {
            "time": ["2026-07-25T12:00", "2026-07-25T13:00", "2026-07-25T14:00"],
            "temperature_2m": [20.1, 21.3],  # one short — malformed/partial
            "relative_humidity_2m": [55, 52, 50],  # matches, fine
        }
    }
    parsed = om.parse_forecast_response(payload)
    assert "temperature" in parsed.array_length_mismatches
    assert "humidity" not in parsed.array_length_mismatches
    # Existing truncation behavior is unchanged — still front-aligned
    # pairing, just now with visibility.
    temps = [p for p in parsed.points if p.variable == "temperature"]
    assert len(temps) == 2


def test_parse_forecast_response_no_mismatch_when_arrays_match():
    payload = {
        "hourly": {
            "time": ["2026-07-25T12:00", "2026-07-25T13:00"],
            "temperature_2m": [20.1, 21.3],
        }
    }
    parsed = om.parse_forecast_response(payload)
    assert parsed.array_length_mismatches == ()


def test_run_fingerprint_stable_for_identical_hourly_series():
    """v0.1.19 regression test (DEF-02): the old dedup check compared
    issued_at, which is always datetime.now() and therefore always
    advances — it could never actually detect an unchanged upstream run.
    run_fingerprint is a content hash instead, so two parses of the exact
    same hourly series must produce the same fingerprint even though
    issued_at differs between the two calls.
    """
    payload = {
        "hourly": {
            "time": ["2026-07-25T12:00", "2026-07-25T13:00"],
            "temperature_2m": [20.1, 21.3],
            "relative_humidity_2m": [55, 52],
        }
    }
    first = om.parse_forecast_response(payload)
    second = om.parse_forecast_response(payload)
    assert first.run_fingerprint == second.run_fingerprint
    assert first.run_fingerprint is not None
    # issued_at itself still always advances (unrelated to the fix) —
    # confirms the fingerprint, not issued_at, is what dedup should use.
    assert first.issued_at <= second.issued_at


def test_run_fingerprint_changes_when_hourly_series_changes():
    payload_a = {
        "hourly": {"time": ["2026-07-25T12:00"], "temperature_2m": [20.1]}
    }
    payload_b = {
        "hourly": {"time": ["2026-07-25T12:00"], "temperature_2m": [20.9]}
    }
    parsed_a = om.parse_forecast_response(payload_a)
    parsed_b = om.parse_forecast_response(payload_b)
    assert parsed_a.run_fingerprint != parsed_b.run_fingerprint


def test_parse_elevation_response():
    assert om.parse_elevation_response({"elevation": [543.0]}) == 543.0
    assert om.parse_elevation_response({"elevation": []}) is None
