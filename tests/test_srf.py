import base64
from datetime import datetime, timedelta, timezone

import pytest

from swissweather_fusion.clients import srf


def test_build_basic_auth_header():
    header = srf.build_basic_auth_header("mykey", "mysecret")
    assert header.startswith("Basic ")
    decoded = base64.b64decode(header.split(" ")[1]).decode()
    assert decoded == "mykey:mysecret"


def test_build_forecast_url_uses_path_parameter_not_query_string():
    """v0.1.3 regression test: production hit a 404 because the
    geolocationId was passed as a query parameter (?geolocationId=X) and
    the URL included an incorrect /v2/ path segment. Confirmed correct
    structure from SRG-SSR's own docs and a real working example.
    """
    url = srf.build_forecast_url("47.5536,8.9120")
    assert url == "https://api.srgssr.ch/srf-meteo/forecast/47.5536,8.9120"
    assert "?geolocationId=" not in url
    assert "/v2/" not in url


def test_geolocation_url_has_no_v2_segment():
    assert "/v2/" not in srf.GEOLOCATION_URL


def test_parse_token_response():
    assert srf.parse_token_response({"access_token": "abc123"}) == "abc123"
    with pytest.raises(ValueError):
        srf.parse_token_response({})


def test_token_expiry_proactive_refresh_margin():
    fresh = srf.CachedToken(access_token="x", obtained_at=datetime.now(timezone.utc))
    assert not fresh.is_expired()

    old = srf.CachedToken(access_token="x", obtained_at=datetime.now(timezone.utc) - timedelta(days=8))
    assert old.is_expired()

    # Inside the 1-day refresh margin of the 7-day lifetime -> already expired
    almost_old = srf.CachedToken(
        access_token="x", obtained_at=datetime.now(timezone.utc) - timedelta(days=6, hours=1)
    )
    assert almost_old.is_expired()


def test_parse_geolocation_response():
    assert srf.parse_geolocation_response({"geolocations": [{"geolocationId": "GL123"}]}) == "GL123"
    assert srf.parse_geolocation_response({"results": [{"id": "GL456"}]}) == "GL456"
    assert srf.parse_geolocation_response({}) is None


def test_parse_geolocation_response_bare_list_v0_1_1_fix():
    """v0.1.1: production crashed with 'list' object has no attribute
    'get' — the actual SRF response is very likely a bare top-level array,
    not the dict-wrapped shape originally guessed from documentation
    alone. This is the fix, confirmed against the specific failure mode.
    """
    assert srf.parse_geolocation_response([{"geolocationId": "GL789"}]) == "GL789"
    assert srf.parse_geolocation_response([]) is None


def test_parse_forecast_response():
    payload = {
        "forecast": [
            {
                "localDateTime": "2026-07-25T12:00:00",
                "temperature": 21.0,
                "relativeHumidity": 55,
                "meanSeaLevelPressure": 1013.5,
            },
            {
                "localDateTime": "2026-07-25T13:00:00",
                "temperature": 22.0,
                "relativeHumidity": 52,
                "meanSeaLevelPressure": 1013.2,
            },
        ]
    }
    points = srf.parse_forecast_response(payload)
    assert len(points) == 6  # 3 fields x 2 timesteps
    temps = [p for p in points if p.variable == "temperature"]
    assert temps[0].value == 21.0


def test_parse_forecast_response_bare_list_v0_1_1_fix():
    """Same defensive fix as the geolocation parser, same reason."""
    payload = [
        {"localDateTime": "2026-07-25T12:00:00", "temperature": 21.0},
    ]
    points = srf.parse_forecast_response(payload)
    assert len(points) == 1
    assert points[0].value == 21.0
