from datetime import date

from swissweather_fusion.clients import meteonomiqs as mq

TEST_LAT, TEST_LON = 46.9480, 7.4474


def test_build_nowcast_url():
    url = mq.build_nowcast_url(latitude=TEST_LAT, longitude=TEST_LON)
    assert url == f"https://forecast.meteonomiqs.com/v4_0/nowcast/weather/{TEST_LAT}/{TEST_LON}"


def test_build_auth_headers():
    headers = mq.build_auth_headers("TESTKEY")
    assert headers["x-api-key"] == "TESTKEY"


def test_parse_nowcast_response():
    payload = {
        "precipitationRisk": {
            "items": [
                {
                    "from": "2026-07-25T13:35:00.000Z",
                    "to": "2026-07-25T13:30:00.000Z",
                    "precrisk": {"value": 3},
                    "radar": {"amount": {"value": 1.5}},
                }
            ]
        }
    }
    parsed = mq.parse_nowcast_response(payload)
    assert len(parsed.items) == 1
    assert parsed.items[0].precip_risk_value == 3
    assert parsed.items[0].radar_precip_mmh == 1.5


def test_parse_hourly_forecast_extracts_pressure_and_precip():
    payload = {
        "items": [
            {
                "from": "2026-07-25T12:00:00.000Z",
                "to": "2026-07-25T13:00:00.000Z",
                "pressure": 1013,
                "prec": {"sum": 0.5, "probability": 60},
            },
            {
                "from": "2026-07-25T13:00:00.000Z",
                "to": "2026-07-25T14:00:00.000Z",
                "pressure": 1011,
                "prec": {"sum": 1.2, "probability": 75},
            },
        ],
        "forecastDate": "2026-07-25T11:30:00.000Z",
        "source": "UWSV2",
    }
    points = mq.parse_hourly_forecast(payload)
    assert len(points) == 2
    assert points[0].mean_sea_level_pressure == 1013
    assert points[0].precipitation_sum_mm == 0.5
    assert points[0].precipitation_probability == 60
    # Confirms the falling-pressure trend is visible across the two points
    assert points[1].mean_sea_level_pressure < points[0].mean_sea_level_pressure


def test_build_forecast_hourly_url_targets_plain_non_premium_endpoint():
    url = mq.build_forecast_hourly_url(latitude=TEST_LAT, longitude=TEST_LON)
    assert url == f"https://forecast.meteonomiqs.com/v4_0/forecast/{TEST_LAT}/{TEST_LON}/hourly"
    assert "forecast2" not in url


def test_annual_call_budget_rollover():
    budget = mq.AnnualCallBudget(annual_budget=1000)
    d1 = date(2026, 7, 25)
    assert budget.can_call(today=d1)
    for _ in range(1000):
        budget.record_call(today=d1)
    assert not budget.can_call(today=d1)
    assert budget.calls_remaining_this_year == 0

    next_year = date(2027, 1, 1)
    assert budget.can_call(today=next_year)
    assert budget.calls_remaining_this_year == 1000


def test_needs_keepalive_call():
    today = date(2026, 7, 25)
    assert mq.needs_keepalive_call(last_successful_call_date=None, today=today, max_days_between_calls=30)
    assert not mq.needs_keepalive_call(
        last_successful_call_date=date(2026, 7, 24), today=today, max_days_between_calls=30
    )
    assert mq.needs_keepalive_call(
        last_successful_call_date=date(2026, 6, 1), today=today, max_days_between_calls=30
    )
