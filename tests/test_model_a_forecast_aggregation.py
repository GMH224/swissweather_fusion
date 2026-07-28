from datetime import datetime, timezone

from swissweather_fusion.models import model_a


def _hourly_entry(dt_str: str, temp: float, precip: float) -> dict:
    return {
        "datetime": dt_str,
        "native_temperature": temp,
        "native_precipitation": precip,
    }


def test_aggregate_daily_forecast_groups_by_day_and_sums_precip():
    hourly = [
        _hourly_entry("2026-07-25T00:00:00+00:00", 15.0, 0.0),
        _hourly_entry("2026-07-25T12:00:00+00:00", 28.0, 0.5),
        _hourly_entry("2026-07-25T23:00:00+00:00", 18.0, 0.2),
        _hourly_entry("2026-07-26T00:00:00+00:00", 14.0, 0.0),
        _hourly_entry("2026-07-26T14:00:00+00:00", 22.0, 0.0),
    ]
    daily = model_a.aggregate_daily_forecast(hourly)

    assert len(daily) == 2
    day1, day2 = daily
    assert day1["native_temperature"] == 28.0  # day high
    assert day1["native_templow"] == 15.0  # day low
    assert abs(day1["native_precipitation"] - 0.7) < 1e-9  # summed, not averaged
    assert day1["condition"] == "rainy"  # total > 0.5mm threshold

    assert day2["native_temperature"] == 22.0
    assert day2["native_templow"] == 14.0
    assert day2["native_precipitation"] == 0.0
    assert day2["condition"] == "sunny"


def test_aggregate_daily_forecast_handles_missing_values():
    hourly = [_hourly_entry("2026-07-25T12:00:00+00:00", None, None)]
    daily = model_a.aggregate_daily_forecast(hourly)
    assert daily[0]["native_temperature"] is None
    assert daily[0]["native_precipitation"] is None
    assert daily[0]["condition"] == "sunny"  # no precip data -> not rainy


def test_aggregate_twice_daily_forecast_splits_day_and_night():
    hourly = [
        _hourly_entry("2026-07-25T08:00:00+00:00", 20.0, 0.0),  # day
        _hourly_entry("2026-07-25T14:00:00+00:00", 28.0, 1.0),  # day
        _hourly_entry("2026-07-25T20:00:00+00:00", 16.0, 0.0),  # night (evening)
    ]
    periods = model_a.aggregate_twice_daily_forecast(hourly)

    day_periods = [p for p in periods if p["is_daytime"]]
    night_periods = [p for p in periods if not p["is_daytime"]]

    assert len(day_periods) == 1
    assert day_periods[0]["native_temperature"] == 28.0
    assert day_periods[0]["native_precipitation"] == 1.0

    assert len(night_periods) == 1
    assert night_periods[0]["native_temperature"] == 16.0


def test_aggregate_twice_daily_forecast_early_morning_belongs_to_previous_nights_period():
    """Regression test for a real bug caught before shipping: early
    morning hours (00:00-05:59) must be grouped with the PREVIOUS day's
    overnight period, not treated as their own new period starting at
    midnight. The original code had a tautological condition that always
    returned the current date regardless, which this test would have
    caught immediately.
    """
    hourly = [
        _hourly_entry("2026-07-25T22:00:00+00:00", 15.0, 0.0),  # night of the 25th
        _hourly_entry("2026-07-26T02:00:00+00:00", 13.0, 0.5),  # early morning 26th,
                                                                  # still "night of the 25th"
        _hourly_entry("2026-07-26T10:00:00+00:00", 25.0, 0.0),  # day of the 26th
    ]
    periods = model_a.aggregate_twice_daily_forecast(hourly)
    night_periods = [p for p in periods if not p["is_daytime"]]

    # The 22:00 (25th) and 02:00 (26th) entries must land in the SAME
    # night period, not two separate ones.
    assert len(night_periods) == 1
    assert night_periods[0]["native_temperature"] == 15.0  # max of 15.0 and 13.0
    assert night_periods[0]["native_precipitation"] == 0.5  # summed from both hours

    day_periods = [p for p in periods if p["is_daytime"]]
    assert len(day_periods) == 1
    assert day_periods[0]["native_temperature"] == 25.0
