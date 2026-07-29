import base64
from datetime import datetime, timedelta, timezone

import pytest

from swissweather_fusion.clients import srf


def test_client_timeout_configured():
    """v0.1.6: confirms the timeout helper actually produces a bounded
    timeout, added after SRF's polling appeared to silently stop for
    several hours in production with no failure recorded — consistent
    with a hung request rather than an error.
    """
    timeout = srf._client_timeout()
    assert timeout.total == srf.REQUEST_TIMEOUT_SECONDS
    assert timeout.total is not None and timeout.total > 0


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


def test_parse_geolocation_response_string_entry_v0_1_4_fix():
    """v0.1.4: a third distinct SRF crash — 'str' object has no attribute
    'get' — meaning a list entry can apparently be a bare string (the
    coordinate itself) rather than an object with a geolocationId/id
    field. If it's already a string, treat it as already usable.
    """
    assert srf.parse_geolocation_response(["47.5536,8.9120"]) == "47.5536,8.9120"
    assert srf.parse_geolocation_response({"geolocations": ["47.5536,8.9120"]}) == "47.5536,8.9120"


def test_parse_geolocation_response_unexpected_top_level_types():
    """Neither a list nor a dict at the top level shouldn't crash either."""
    assert srf.parse_geolocation_response("just a string") is None
    assert srf.parse_geolocation_response(None) is None
    assert srf.parse_geolocation_response(42) is None


def test_parse_forecast_response_matches_confirmed_real_structure():
    """v0.1.8: rewritten against a real captured production response, not
    documentation or further guessing. Confirmed real shape:
    {"forecast": {"day": [...]}} with TX_C/TN_C/RRR_MM/FF_KMH fields (day
    max/min temperature, day precip total, day avg wind) — genuinely
    daily granularity, no humidity or pressure field present at all.
    Mapped to measurement names distinct from the hourly ones
    (temperature_daily_max, not temperature) so these can never be
    silently blended as if they were hourly point values.
    """
    payload = {
        "geolocation": {"id": "47.5536,8.9120", "default_name": "Neuhuuse"},
        "forecast": {
            "day": [
                {
                    "TX_C": 34, "TN_C": 15, "RRR_MM": 0.0, "FF_KMH": 6,
                    "local_date_time": "2026-07-29T00:00:00+02:00",
                },
                {
                    "TX_C": 30, "TN_C": 18, "RRR_MM": 2.0, "FF_KMH": 6,
                    "local_date_time": "2026-08-01T00:00:00+02:00",
                },
            ]
        },
    }
    points = srf.parse_forecast_response(payload)
    assert len(points) == 8  # 4 fields x 2 days

    max_temps = [p for p in points if p.variable == "temperature_daily_max"]
    assert max_temps[0].value == 34
    precip_totals = [p for p in points if p.variable == "precip_daily_total"]
    assert precip_totals[1].value == 2.0
    # None of the hourly measurement names appear at all — this is the
    # whole point of the fix.
    assert not any(p.variable in ("temperature", "humidity", "pressure") for p in points)


def test_parse_forecast_response_skips_entries_missing_local_date_time():
    payload = {"forecast": {"day": [{"TX_C": 30, "TN_C": 18}]}}  # no local_date_time
    assert srf.parse_forecast_response(payload) == []


def test_parse_forecast_response_string_entries_v0_1_4_fix():
    """v0.1.4: if entries in the list turn out to be plain strings rather
    than objects, skip them rather than crash — this defensive discipline
    is kept even after the v0.1.8 rebuild against the confirmed real
    structure, since this response family has surprised this project
    multiple times already.
    """
    payload = {"forecast": {"day": ["not a dict", "also not a dict"]}}
    assert srf.parse_forecast_response(payload) == []

    mixed_payload = {
        "forecast": {
            "day": [
                {"TX_C": 30, "local_date_time": "2026-07-29T00:00:00+02:00"},
                "a bare string",
            ]
        }
    }
    points = srf.parse_forecast_response(mixed_payload)
    assert len(points) == 1
    assert points[0].value == 30


def test_parse_forecast_response_unexpected_top_level_types():
    assert srf.parse_forecast_response("just a string") == []
    assert srf.parse_forecast_response(None) == []
    assert srf.parse_forecast_response({"forecast": "not a dict or list"}) == []
    assert srf.parse_forecast_response({"forecast": {"day": "not a list"}}) == []
    # A future/different variant might put the array directly under
    # "forecast" rather than "forecast.day" — handled too.
    direct_list_payload = {
        "forecast": [{"TX_C": 30, "local_date_time": "2026-07-29T00:00:00+02:00"}]
    }
    assert len(srf.parse_forecast_response(direct_list_payload)) == 1
