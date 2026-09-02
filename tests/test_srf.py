import base64
from datetime import datetime, timedelta, timezone

import pytest

from swissweather_fusion.clients import srf


# v0.1.18: real sample entries confirmed from a live API response (not
# fabricated) — one hours entry, one three_hours entry with an
# overlapping-adjacent timestamp, one days entry, matching exactly what
# was captured from the actual v2/forecastpoint endpoint.
_REAL_HOURS_ENTRY = {
    "TTT_C": 22, "TTL_C": 20.4, "TTH_C": 23.2, "DEWPOINT_C": 14.0,
    "RELHUM_PERCENT": 62, "FRESHSNOW_MM": 0, "PRESSURE_HPA": 1018,
    "SUN_MIN": 0, "IRRADIANCE_WM2": 0, "TTTFEEL_C": 23,
    "cur_color": {"temperature": 22, "background_color": "#fcd804", "text_color": "#000000"},
    "date_time": "2026-07-31T00:00:00+02:00",
    "symbol_code": -1, "symbol24_code": 100,
    "PROBPCP_PERCENT": 1, "RRR_MM": 0.0, "FF_KMH": 4, "FX_KMH": 7, "DD_DEG": 110,
}
_REAL_THREE_HOURS_ENTRY = {
    "TTT_C": 19, "TTL_C": 17.6, "TTH_C": 20.0, "DEWPOINT_C": 14.0,
    "RELHUM_PERCENT": 75, "FRESHSNOW_MM": 0, "PRESSURE_HPA": 1019,
    "SUN_MIN": 0, "IRRADIANCE_WM2": 0, "TTTFEEL_C": 20,
    "cur_color": {"temperature": 19, "background_color": "#fce404", "text_color": "#000000"},
    "date_time": "2026-07-31T02:00:00+02:00",
    "symbol_code": -1, "symbol24_code": 100,
    "PROBPCP_PERCENT": 1, "RRR_MM": 0.0, "FF_KMH": 4, "FX_KMH": 9, "DD_DEG": 160,
}
_REAL_DAYS_ENTRY = {
    "SUNSET": "2026-07-31T21:03:00+02:00", "SUNRISE": "2026-07-31T06:09:00+02:00",
    "SUN_H": 8, "UVI": 6, "TX_C": 31, "TN_C": 16,
    "min_color": {"temperature": 16, "background_color": "#e4e20c", "text_color": "#000000"},
    "max_color": {"temperature": 31, "background_color": "#fc8404", "text_color": "#000000"},
    "date_time": "2026-07-31T00:00:00+02:00",
    "symbol_code": 11, "symbol24_code": 21,
    "PROBPCP_PERCENT": 64, "RRR_MM": 2.0, "FF_KMH": 6, "FX_KMH": 33, "DD_DEG": 280,
}


def test_parse_forecastpoint_response_extracts_core_measurements():
    """The five measurements Model A's blend actually looks up must use
    the exact same variable names every other source uses — this is what
    finally lets SRF participate in the blend instead of being
    permanently excluded from it.
    """
    payload = {"hours": [_REAL_HOURS_ENTRY], "three_hours": [], "days": []}
    points = srf.parse_forecastpoint_response(payload)
    by_variable = {p.variable: p.value for p in points}
    assert by_variable["temperature"] == 22
    assert by_variable["humidity"] == 62
    assert by_variable["pressure"] == 1018
    assert by_variable["precip"] == 0.0


def test_parse_forecastpoint_response_converts_wind_speed_kmh_to_ms():
    """v0.1.18: SRF reports wind in km/h; every other source uses m/s
    (v0.1.5's Open-Meteo fix). Storing the raw km/h value under the same
    "wind_speed" name would silently corrupt Model A's blend.
    """
    payload = {"hours": [_REAL_HOURS_ENTRY], "three_hours": [], "days": []}
    points = srf.parse_forecastpoint_response(payload)
    by_variable = {p.variable: p.value for p in points}
    assert by_variable["wind_speed"] == pytest.approx(4 / 3.6)
    assert by_variable["srf_wind_gust"] == pytest.approx(7 / 3.6)


def test_parse_forecastpoint_response_promotes_common_fields_and_prefixes_the_rest():
    """v0.2.0 changed this contract deliberately.

    Through v0.1.28 every field beyond the core five was prefixed `srf_`
    so it could never be picked up by the blend. That was the right
    default while Model A fused only five measurements — but it meant SRF
    was parsing eleven extra fields, storing them, and having nothing
    ever read them (the IND-10 write-only pattern).

    v0.2.0 promotes the four that have a genuine cross-source equivalent
    into the common vocabulary, so they can be fused. The rest stay
    prefixed, and one of them — FRESHSNOW_MM — stays prefixed
    specifically because it is in millimetres while the common
    `snowfall` parameter is centimetres. Unit reconciliation is a
    deliberate change, not a rename, so it is NOT promoted here.
    """
    payload = {"hours": [_REAL_HOURS_ENTRY], "three_hours": [], "days": []}
    points = srf.parse_forecastpoint_response(payload)
    by_variable = {p.variable: p.value for p in points}

    # Promoted to the common vocabulary (fusable).
    assert by_variable["dew_point"] == 14.0
    assert by_variable["apparent_temperature"] == 23
    assert by_variable["precip_probability"] == 1
    assert by_variable["wind_bearing"] == 110

    # Still namespaced: no cross-source equivalent, or a unit mismatch.
    assert by_variable["srf_temp_low_bound"] == 20.4
    assert by_variable["srf_temp_high_bound"] == 23.2
    assert by_variable["srf_freshsnow"] == 0
    assert by_variable["srf_sun_minutes"] == 0
    assert by_variable["srf_irradiance"] == 0

    # The old names must be gone, so nothing reads them by habit.
    assert "srf_dewpoint" not in by_variable
    assert "srf_precip_probability" not in by_variable
    assert by_variable["srf_symbol_code"] == -1
    assert by_variable["srf_symbol24_code"] == 100
    # cur_color is a nested UI color hint, not weather data — deliberately
    # not stored anywhere; confirms nothing crashes trying to store it as
    # a number, and that it doesn't leak through under any variable name.
    assert not any("color" in v for v in by_variable)


def test_parse_forecastpoint_response_daily_fields():
    payload = {"hours": [], "three_hours": [], "days": [_REAL_DAYS_ENTRY]}
    points = srf.parse_forecastpoint_response(payload)
    by_variable = {p.variable: p.value for p in points}
    assert by_variable["temperature_daily_max"] == 31
    assert by_variable["temperature_daily_min"] == 16
    assert by_variable["precip_daily_total"] == 2.0
    assert by_variable["wind_speed_daily_avg"] == pytest.approx(6 / 3.6)
    assert by_variable["srf_daily_wind_gust"] == pytest.approx(33 / 3.6)
    assert by_variable["srf_daily_uv_index"] == 6
    assert by_variable["srf_daily_sun_hours"] == 8
    assert by_variable["srf_daily_precip_probability"] == 64
    assert by_variable["srf_daily_wind_direction"] == 280
    assert by_variable["srf_daily_symbol_code"] == 11
    assert by_variable["srf_daily_symbol24_code"] == 21
    # SUNRISE/SUNSET are timestamps, not stored as forecast_snapshots
    # values (a REAL/float column) — confirms they don't crash anything
    # and aren't silently coerced into garbage numbers.
    assert "srf_sunrise" not in by_variable
    assert "srf_sunset" not in by_variable


def test_parse_forecastpoint_response_hours_wins_over_three_hours_for_same_timestamp():
    """v0.1.18: hours and three_hours can both cover the same timestamp —
    without deduplication, both would insert a row for the same (source,
    variable, valid_at), and which one a later query picks up would
    depend on insertion order rather than being a deliberate choice.
    hours (finer native granularity) must win.
    """
    same_time = "2026-07-31T00:00:00+02:00"
    hours_entry = {**_REAL_HOURS_ENTRY, "date_time": same_time, "TTT_C": 22}
    three_hours_entry = {**_REAL_THREE_HOURS_ENTRY, "date_time": same_time, "TTT_C": 999}
    payload = {"hours": [hours_entry], "three_hours": [three_hours_entry], "days": []}
    points = srf.parse_forecastpoint_response(payload)
    temps = [p.value for p in points if p.variable == "temperature"]
    assert temps == [22]  # hours' value, not three_hours' 999


def test_parse_forecastpoint_response_merges_per_field_at_shared_timestamp():
    """v0.1.19 regression test: before this fix, when hours and
    three_hours both covered the same timestamp, the merge replaced
    three_hours' ENTIRE point list for that timestamp with hours' list —
    so a field present only in three_hours (e.g. a measurement hours
    happened not to report that hour) was silently lost even though there
    was no real conflict. Simulates exactly that: hours reports
    everything except PRESSURE_HPA at a timestamp; three_hours reports
    PRESSURE_HPA (differently) plus its own TTT_C at the same timestamp.
    Expected: hours' TTT_C wins (real conflict, finer source), but
    three_hours' pressure survives (no conflict, would have been lost
    pre-fix).
    """
    same_time = "2026-07-31T00:00:00+02:00"
    hours_entry = dict(_REAL_HOURS_ENTRY)
    hours_entry["date_time"] = same_time
    hours_entry["TTT_C"] = 22
    del hours_entry["PRESSURE_HPA"]  # hours doesn't report pressure this hour

    three_hours_entry = dict(_REAL_THREE_HOURS_ENTRY)
    three_hours_entry["date_time"] = same_time
    three_hours_entry["TTT_C"] = 999  # would win pre-fix; must lose to hours
    three_hours_entry["PRESSURE_HPA"] = 1021  # only source for pressure at this timestamp

    payload = {"hours": [hours_entry], "three_hours": [three_hours_entry], "days": []}
    points = srf.parse_forecastpoint_response(payload)
    by_variable = {p.variable: p.value for p in points}

    assert by_variable["temperature"] == 22  # hours wins the real conflict
    assert by_variable["pressure"] == 1021  # three_hours-only field survives


def test_parse_forecastpoint_response_three_hours_fills_beyond_hours_coverage():
    """For a timestamp only three_hours covers, that data must still be
    included — it's not redundant, it extends coverage further out.
    """
    payload = {
        "hours": [_REAL_HOURS_ENTRY],  # covers 00:00
        "three_hours": [_REAL_THREE_HOURS_ENTRY],  # covers 02:00, not in hours
        "days": [],
    }
    points = srf.parse_forecastpoint_response(payload)
    temps_by_time = {p.valid_at: p.value for p in points if p.variable == "temperature"}
    assert len(temps_by_time) == 2  # both timestamps present, not merged into one


def test_parse_forecastpoint_response_stores_valid_at_as_utc():
    """The API returns +02:00 (CEST) offsets; this project stores
    everything in UTC throughout, so these must be converted, not stored
    with the original offset attached.
    """
    payload = {"hours": [_REAL_HOURS_ENTRY], "three_hours": [], "days": []}
    points = srf.parse_forecastpoint_response(payload)
    temp_point = next(p for p in points if p.variable == "temperature")
    assert temp_point.valid_at.tzinfo == timezone.utc
    assert temp_point.valid_at == datetime(2026, 7, 30, 22, 0, tzinfo=timezone.utc)  # 00:00 CEST - 2h


def test_parse_forecastpoint_response_defensive_against_missing_fields():
    """Confirms an entry missing some fields doesn't crash — just skips
    what's not present, same defensive philosophy as every other parser
    in this file given SRF's history of response-shape surprises.
    """
    sparse_entry = {"date_time": "2026-07-31T00:00:00+02:00", "TTT_C": 20}
    payload = {"hours": [sparse_entry], "three_hours": [], "days": []}
    points = srf.parse_forecastpoint_response(payload)
    by_variable = {p.variable: p.value for p in points}
    assert by_variable == {"temperature": 20}


def test_parse_forecastpoint_response_not_a_dict_returns_empty():
    assert srf.parse_forecastpoint_response([1, 2, 3]) == []
    assert srf.parse_forecastpoint_response(None) == []


def test_parse_forecastpoint_response_missing_arrays_returns_empty():
    assert srf.parse_forecastpoint_response({}) == []
    assert srf.parse_forecastpoint_response({"geolocation": {"id": "x"}}) == []


def test_build_forecastpoint_url():
    assert (
        srf.build_forecastpoint_url("46.9471,7.4441")
        == "https://api.srgssr.ch/srf-meteo/v2/forecastpoint/46.9471,7.4441"
    )


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
    url = srf.build_forecast_url("46.9480,7.4474")
    assert url == "https://api.srgssr.ch/srf-meteo/forecast/46.9480,7.4474"
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
    assert srf.parse_geolocation_response(["46.9480,7.4474"]) == "46.9480,7.4474"
    assert srf.parse_geolocation_response({"geolocations": ["46.9480,7.4474"]}) == "46.9480,7.4474"


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
        "geolocation": {"id": "46.9480,7.4474", "default_name": "ExampleTown"},
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


def test_parse_forecast_response_normalizes_offset_aware_timestamps_to_utc():
    """v0.1.19 regression test: before this fix, an offset-aware
    local_date_time (e.g. the real "+02:00" CEST the daily endpoint
    returns) kept its original offset instead of being converted to UTC.
    storage/db.py compares/sorts valid_at as exact ISO strings, and every
    other source (plus SRF's own hourly/forecastpoint path via
    _parse_entry_datetime) stores UTC "+00:00" strings — so an
    un-normalized "+02:00" row would silently never match the blend's
    target keys even though it looked present in storage. Confirms the
    daily fallback now produces the same UTC-normalized ISO string a
    hand-converted "+02:00" timestamp should produce.
    """
    payload = {
        "forecast": {
            "day": [
                {
                    "TX_C": 30, "TN_C": 18, "RRR_MM": 0.0, "FF_KMH": 6,
                    "local_date_time": "2026-08-01T00:00:00+02:00",
                },
            ]
        },
    }
    points = srf.parse_forecast_response(payload)
    assert points  # sanity: the entry was actually parsed
    for point in points:
        assert point.valid_at.utcoffset().total_seconds() == 0
        assert point.valid_at.isoformat() == "2026-07-31T22:00:00+00:00"


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


def test_parse_srf_error_detail_real_location_limit_error():
    """v0.1.21: the exact real error confirmed via a live probe script
    against a real account that had exceeded the SRF free plan's
    one-registered-location limit for v2/forecastpoint. Confirms this is
    surfaced verbatim rather than being swallowed by a generic HTTP
    error, since — unlike every other SRF surprise in this project's
    history — this one turned out to not be a parsing/code bug at all,
    so the only thing code CAN usefully do here is make the real reason
    visible immediately instead of costing hours of debugging a
    coordinator/parser that was never broken.
    """
    body = (
        '{"code": "400.01.007", "message": "location mismatch for '
        'developer app", "info": "You have exceeded your location limit"}'
    )
    detail = srf.parse_srf_error_detail(body)
    assert detail == (
        "400.01.007 — location mismatch for developer app — "
        "You have exceeded your location limit"
    )


def test_parse_srf_error_detail_handles_non_json_body():
    assert srf.parse_srf_error_detail("<html>Bad Gateway</html>") is None
    assert srf.parse_srf_error_detail("") is None


def test_parse_srf_error_detail_handles_json_without_expected_shape():
    assert srf.parse_srf_error_detail("[]") is None
    assert srf.parse_srf_error_detail('{"unrelated_field": "value"}') is None


def test_parse_srf_error_detail_handles_partial_shape():
    # message-only (no code) — still useful, still surfaced.
    detail = srf.parse_srf_error_detail('{"message": "Something went wrong"}')
    assert detail == "Something went wrong"


# -- v0.1.23: async client-level tests (L-11, L-12) --------------------------
#
# No async client-level tests existed before this — everything above tests
# pure functions (parsing, URL building) only. The 401-retry and
# permanent-error-classification behaviors below are genuinely new
# integration-level logic that only exists at the async request layer, so
# they need a fake aiohttp session/response to exercise for real.

import asyncio
import json as _json


class _FakeResponse:
    def __init__(self, *, status: int, json_body: dict, text_body: str = None):
        self.status = status
        self._json_body = json_body
        self._text_body = text_body if text_body is not None else _json.dumps(json_body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._json_body

    async def text(self):
        return self._text_body

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class _FakeSession:
    """Replays a scripted sequence of responses per HTTP method, in the
    order they're requested — enough to drive SrfClient's real request
    code without a real network."""

    def __init__(self, *, post_responses=None, get_responses=None):
        self._post_responses = list(post_responses or [])
        self._get_responses = list(get_responses or [])
        self.get_calls = []

    def post(self, url, **kwargs):
        return self._post_responses.pop(0)

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self._get_responses.pop(0)


def _token_response():
    return _FakeResponse(status=200, json_body={"access_token": "TOKEN1", "expires_in": 604800})


def _geolocation_response():
    return _FakeResponse(status=200, json_body=[{"geolocationId": "12345"}])


def test_srf_client_retries_once_on_401_then_succeeds():
    """v0.1.23 direct regression test for L-12: a cached-but-rejected
    token (401) must be cleared and refreshed exactly once, with the
    original request retried and succeeding — not left permanently
    broken until local token expiry."""
    session = _FakeSession(
        post_responses=[_token_response(), _token_response()],  # initial + one forced refresh
        get_responses=[
            _FakeResponse(status=401, json_body={}, text_body="Unauthorized"),
            _geolocation_response(),
        ],
    )
    client = srf.SrfClient(session, "key", "secret")

    async def run():
        return await client._async_ensure_geolocation_id(46.9, 7.4)

    geolocation_id = asyncio.run(run())
    assert geolocation_id == "12345"
    # Confirms a refresh really happened: two POSTs (token) consumed.
    assert session._post_responses == []


def test_srf_client_does_not_loop_forever_on_persistent_401():
    """A 401 that persists even after one refresh-and-retry must surface
    as an error, not retry indefinitely."""
    session = _FakeSession(
        post_responses=[_token_response(), _token_response()],
        get_responses=[
            _FakeResponse(status=401, json_body={}, text_body="Unauthorized"),
            _FakeResponse(status=401, json_body={}, text_body="Unauthorized"),
        ],
    )
    client = srf.SrfClient(session, "key", "secret")

    async def run():
        return await client._async_ensure_geolocation_id(46.9, 7.4)

    with pytest.raises(srf.SrfPermanentError) as exc_info:
        asyncio.run(run())
    assert exc_info.value.status == 401


def test_srf_client_raises_permanent_error_for_400_without_retry_loop():
    """v0.1.23 direct regression test for L-11: a 400 (e.g. the confirmed
    real 'exceeded your location limit' free-plan restriction) must raise
    SrfPermanentError, distinguishable from a transient failure, and must
    NOT trigger a token refresh (a bad request has nothing to do with the
    token being invalid)."""
    session = _FakeSession(
        post_responses=[_token_response()],
        get_responses=[
            _FakeResponse(
                status=400,
                json_body={},
                text_body=_json.dumps({"detail": "you have exceeded your location limit"}),
            ),
        ],
    )
    client = srf.SrfClient(session, "key", "secret")

    async def run():
        return await client._async_ensure_geolocation_id(46.9, 7.4)

    with pytest.raises(srf.SrfPermanentError) as exc_info:
        asyncio.run(run())
    assert exc_info.value.status == 400
    assert "location limit" in str(exc_info.value)
    # Only ONE token POST — no refresh attempted for a non-401 error.
    assert session._post_responses == []


def test_srf_client_5xx_raises_plain_runtime_error_not_permanent_error():
    """A 5xx must remain a plain (fallback-eligible) error, not
    SrfPermanentError — 5xx is the transient case the coordinator's
    existing fallback-to-daily-endpoint behavior is meant to catch."""
    session = _FakeSession(
        post_responses=[_token_response()],
        get_responses=[_FakeResponse(status=503, json_body={}, text_body="Service Unavailable")],
    )
    client = srf.SrfClient(session, "key", "secret")

    async def run():
        return await client._async_ensure_geolocation_id(46.9, 7.4)

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(run())
    assert not isinstance(exc_info.value, srf.SrfPermanentError)
