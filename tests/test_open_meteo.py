import pytest

from swissweather_fusion.clients import open_meteo as om

# Generic placeholder coordinates throughout — never the real deployment
# location, per this project's confidentiality requirement.
TEST_LAT, TEST_LON = 46.9480, 7.4474


def test_build_forecast_url():
    url = om.build_forecast_url(source="ch1", latitude=TEST_LAT, longitude=TEST_LON)
    assert "models=icon_ch1_eps" in url
    assert f"latitude={TEST_LAT}" in url


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
            "surface_pressure": [1013.2, 1013.0],
            "precipitation": [0.0, 0.2],
            "wind_speed_10m": [3.1, 3.4],
        }
    }
    parsed = om.parse_forecast_response(payload)
    assert len(parsed.points) == 10  # 5 variables x 2 timesteps
    temps = [p for p in parsed.points if p.variable == "temperature"]
    assert len(temps) == 2
    assert temps[0].value == 20.1


def test_parse_elevation_response():
    assert om.parse_elevation_response({"elevation": [543.0]}) == 543.0
    assert om.parse_elevation_response({"elevation": []}) is None
