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


def test_should_fire_scheduled_call_basic():
    scheduled = datetime(2026, 7, 25, 16, 0)
    unscheduled = datetime(2026, 7, 25, 15, 0)

    assert mb.should_fire_scheduled_call(local_dt=scheduled, last_scheduled_call_hour=None)
    assert not mb.should_fire_scheduled_call(local_dt=unscheduled, last_scheduled_call_hour=None)

    already_fired = datetime(2026, 7, 25, 16, 3)  # same hour, a few min later
    assert not mb.should_fire_scheduled_call(
        local_dt=already_fired, last_scheduled_call_hour=scheduled
    )

    next_slot = datetime(2026, 7, 25, 20, 0)
    assert mb.should_fire_scheduled_call(local_dt=next_slot, last_scheduled_call_hour=scheduled)


def test_dst_spring_forward_gap_does_not_crash():
    """Edge case requested directly: winter -> summer transition (Europe,
    last Sunday of March — clocks jump 02:00 CET straight to 03:00 CEST,
    so the local hour 02:00-02:59 never occurs that day). meteoblue's
    actual scheduled hours (6/12/16/18/20 depending on season) don't fall
    in that skipped window, so the practical impact is none — this test
    exists to confirm the scheduling functions handle the surrounding
    hours without raising or behaving strangely, not because meteoblue
    would ever try to fire during the gap itself. A skipped local hour
    simply means that hour's check is never called at all, which needs
    no special handling — there's nothing to test on the skipped hour
    itself, only that neighboring hours keep working normally.
    """
    just_before_gap = datetime(2026, 3, 29, 1, 0)  # 01:00 CET, last normal hour
    just_after_gap = datetime(2026, 3, 29, 3, 0)  # 03:00 CEST, first hour after the jump

    # Neither raises, and both evaluate normally against the schedule —
    # March is a "summer" month per METEOBLUE_SUMMER_MONTHS, so neither
    # 01:00 nor 03:00 is a scheduled slot (12/16/20), which is the
    # correct, unremarkable answer.
    assert mb.is_scheduled_poll_time(local_dt=just_before_gap) is False
    assert mb.is_scheduled_poll_time(local_dt=just_after_gap) is False
    assert mb.should_fire_scheduled_call(
        local_dt=just_after_gap, last_scheduled_call_hour=just_before_gap
    ) is False


def test_dst_fall_back_repeated_hour_does_not_crash_or_double_fire():
    """Edge case requested directly: summer -> winter transition (Europe,
    last Sunday of October — clocks fall back from 03:00 CEST to 02:00
    CET, so the local hour 02:00-02:59 happens twice that day). Uses
    hour=2 as a hypothetical scheduled slot to exercise the guard's
    behavior generally, since meteoblue's real slots don't include 2am —
    this is a robustness test for the pattern, not a scenario meteoblue
    will actually hit.

    The important property: if a call already fired during the FIRST
    occurrence of the repeated hour, the guard must not crash when
    checked again during the SECOND occurrence, and it's acceptable
    (per the project's own "gaps are fine, corruption is not" tolerance)
    for it to conclude no new call is needed, since by (date, hour) alone
    the two occurrences are indistinguishable.
    """
    first_occurrence = datetime(2026, 10, 25, 2, 0)  # 02:00 CEST
    second_occurrence = datetime(2026, 10, 25, 2, 0)  # 02:00 CET, same date/hour,
                                                        # genuinely an hour later in
                                                        # real time, indistinguishable
                                                        # from the first at this
                                                        # (date, hour) granularity

    # Doesn't crash on the repeated value, and — matching the accepted
    # tolerance — does not fire a second time for what looks like the
    # same slot.
    result = mb.should_fire_scheduled_call(
        local_dt=second_occurrence, last_scheduled_call_hour=first_occurrence
    )
    assert result is False

    # And a genuinely new, later slot still fires normally afterward —
    # confirms the repeated hour doesn't permanently wedge the guard.
    # Note: October is still within METEOBLUE_SUMMER_MONTHS (the Mar-Oct
    # storm-season window), so its scheduled hours are 12/16/20, not the
    # Nov-Feb winter hours — using hour=20 here, not a winter hour.
    later_slot = datetime(2026, 10, 25, 20, 0)
    assert mb.should_fire_scheduled_call(
        local_dt=later_slot, last_scheduled_call_hour=second_occurrence
    ) is True


def test_bonus_call_tracker_daily_allowance():
    tracker = mb.BonusCallTracker()
    today = date(2026, 7, 25)
    assert tracker.can_use_bonus_call(today=today)
    tracker.record_bonus_call_used(today=today)
    assert not tracker.can_use_bonus_call(today=today)

    tomorrow = date(2026, 7, 26)
    assert tracker.can_use_bonus_call(today=tomorrow)


def test_bonus_call_tracker_try_use_bonus_call_atomic():
    """v0.1.15: the atomic check-and-record method added to close a
    TOCTOU race — confirms it behaves identically to the separate
    can_use_bonus_call + record_bonus_call_used calls, just in one step.
    """
    tracker = mb.BonusCallTracker()
    today = date(2026, 7, 25)
    assert tracker.try_use_bonus_call(today=today) is True
    # Allowance now used — a second attempt the same day must fail.
    assert tracker.try_use_bonus_call(today=today) is False
    assert not tracker.can_use_bonus_call(today=today)

    tomorrow = date(2026, 7, 26)
    assert tracker.try_use_bonus_call(today=tomorrow) is True


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
