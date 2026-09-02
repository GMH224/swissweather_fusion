from datetime import datetime, timedelta, timezone

from swissweather_fusion.models import model_a


def test_aggregate_daily_forecast_uses_local_timezone_not_utc():
    """v0.1.15 regression test: confirms the actual fix, not just that the
    UTC default is preserved. 23:00 UTC on the 25th is 01:00 CEST on the
    26th — with local_tz passed correctly, that hour must land in the
    26th's bucket, not the 25th's (which is what the old UTC-only
    grouping would have done).
    """
    from datetime import timezone as _timezone

    cest = _timezone(timedelta(hours=2))  # Switzerland summer time, fixed offset
    hourly = [
        _hourly_entry("2026-07-25T22:00:00+00:00", 15.0, 0.0),  # 00:00 CEST on the 26th
        _hourly_entry("2026-07-25T23:00:00+00:00", 14.0, 0.0),  # 01:00 CEST on the 26th
        _hourly_entry("2026-07-26T10:00:00+00:00", 25.0, 1.0),  # 12:00 CEST on the 26th
    ]
    daily_utc = model_a.aggregate_daily_forecast(hourly)  # default: UTC grouping
    daily_local = model_a.aggregate_daily_forecast(hourly, local_tz=cest)

    # Under UTC grouping, the two late-evening-UTC hours land on the 25th.
    assert len(daily_utc) == 2
    day1_utc = daily_utc[0]
    assert day1_utc["native_temperature"] == 15.0  # only the 22:00 UTC hour

    # Under correct local (CEST) grouping, all three hours are actually
    # the same local calendar day (the 26th) — one bucket, not two.
    assert len(daily_local) == 1
    assert daily_local[0]["native_temperature"] == 25.0  # max across all 3 hours
    assert daily_local[0]["native_templow"] == 14.0


def test_aggregate_twice_daily_forecast_uses_local_timezone_not_utc():
    from datetime import timezone as _timezone

    cest = _timezone(timedelta(hours=2))
    # 10:00 UTC = 12:00 CEST (daytime under local tz); under UTC-only
    # grouping this is still daytime too (both are within 06-18), so use
    # a boundary-crossing hour instead: 16:30 UTC = 18:30 CEST (night
    # under local tz, but still "daytime" by raw UTC hour since 16 < 18).
    hourly = [_hourly_entry("2026-07-25T16:00:00+00:00", 20.0, 0.0)]  # UTC hour 16

    utc_periods = model_a.aggregate_twice_daily_forecast(hourly)
    local_periods = model_a.aggregate_twice_daily_forecast(hourly, local_tz=cest)

    # UTC hour 16 is "daytime" (06-18 UTC).
    assert utc_periods[0]["is_daytime"] is True
    # The same instant, correctly localized to CEST, is hour 18 — which is
    # the boundary itself, "night" per TWICE_DAILY_DAY_END_HOUR (>=18).
    assert local_periods[0]["is_daytime"] is False


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


def test_aggregate_twice_daily_forecast_night_uses_min_day_uses_max():
    """v0.1.23 regression test (own-review finding, not in the external
    audit): the night period must report the overnight LOW
    (min(temps)) and the day period must report the daytime HIGH
    (max(temps)) — previously both periods used max(), so a night entry
    silently reported its warmest point instead of its low.
    """
    hourly = [
        _hourly_entry("2026-07-25T08:00:00+00:00", 12.0, 0.0),  # day
        _hourly_entry("2026-07-25T14:00:00+00:00", 27.0, 0.0),  # day (high)
        _hourly_entry("2026-07-25T09:00:00+00:00", 19.0, 0.0),  # day
        _hourly_entry("2026-07-25T19:00:00+00:00", 11.0, 0.0),  # night
        _hourly_entry("2026-07-25T23:00:00+00:00", 4.0, 0.0),   # night (low)
        _hourly_entry("2026-07-26T02:00:00+00:00", 8.0, 0.0),   # night
    ]
    periods = model_a.aggregate_twice_daily_forecast(hourly)
    day = next(p for p in periods if p["is_daytime"])
    night = next(p for p in periods if not p["is_daytime"])

    assert day["native_temperature"] == 27.0  # max across day entries
    assert night["native_temperature"] == 4.0  # min across night entries


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
    # v0.1.23 fix: night periods now report min(temps) (overnight low),
    # not max(temps) — see aggregate_twice_daily_forecast's docstring.
    assert night_periods[0]["native_temperature"] == 13.0  # min of 15.0 and 13.0
    assert night_periods[0]["native_precipitation"] == 0.5  # summed from both hours

    day_periods = [p for p in periods if p["is_daytime"]]
    assert len(day_periods) == 1
    assert day_periods[0]["native_temperature"] == 25.0
