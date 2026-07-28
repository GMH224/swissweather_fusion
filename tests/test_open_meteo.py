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


def test_parse_elevation_response():
    assert om.parse_elevation_response({"elevation": [543.0]}) == 543.0
    assert om.parse_elevation_response({"elevation": []}) is None
