from datetime import date, datetime

from swissweather_fusion.clients import meteoblue as mb

TEST_LAT, TEST_LON = 46.9480, 7.4474


def test_build_forecast_url():
    url = mb.build_forecast_url(latitude=TEST_LAT, longitude=TEST_LON, api_key="TESTKEY")
    assert "apikey=TESTKEY" in url


def test_scheduled_hours_seasonal_boundaries():
    assert mb.scheduled_hours_for_month(7) == (12, 16, 20)   # July -> summer
    assert mb.scheduled_hours_for_month(1) == (6, 12, 18)    # January -> winter
    assert mb.scheduled_hours_for_month(3) == (12, 16, 20)   # March -> summer boundary
    assert mb.scheduled_hours_for_month(2) == (6, 12, 18)    # February -> winter boundary
    assert mb.scheduled_hours_for_month(10) == (12, 16, 20)  # October -> summer boundary
    assert mb.scheduled_hours_for_month(11) == (6, 12, 18)   # November -> winter boundary


def test_is_scheduled_poll_time():
    assert mb.is_scheduled_poll_time(local_dt=datetime(2026, 7, 25, 16, 0))
    assert not mb.is_scheduled_poll_time(local_dt=datetime(2026, 7, 25, 15, 0))
    assert not mb.is_scheduled_poll_time(local_dt=datetime(2026, 7, 25, 16, 5))


def test_bonus_call_tracker_daily_allowance():
    tracker = mb.BonusCallTracker()
    today = date(2026, 7, 25)
    assert tracker.can_use_bonus_call(today=today)
    tracker.record_bonus_call_used(today=today)
    assert not tracker.can_use_bonus_call(today=today)

    tomorrow = date(2026, 7, 26)
    assert tracker.can_use_bonus_call(today=tomorrow)


def test_parse_forecast_response():
    payload = {
        "metadata": {
            "modelrun_updatetime_utc": "2026-07-25 03:41",
            "height": 550,
            "latitude": 46.9,
            "longitude": 7.4,
        },
        "data_1h": {
            "time": ["2026-07-25 00:00", "2026-07-25 01:00"],
            "temperature": [16.0, 15.5],
            "relativehumidity": [47, 51],
            "sealevelpressure": [1013.7, 1013.6],
            "precipitation": [0.0, 0.0],
            "windspeed": [0.9, 0.8],
            "predictability": [71, 71],
        },
    }
    parsed = mb.parse_forecast_response(payload)
    assert parsed.grid_elevation_m == 550
    assert len(parsed.points) == 10  # 5 fields x 2 timesteps
    temps = [p for p in parsed.points if p.variable == "temperature"]
    assert temps[0].value == 16.0
    assert parsed.predictability == [71, 71]
