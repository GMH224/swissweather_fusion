"""Tests for v0.3.2 — the runtime-surface defect class, and the daily
forecast truncation.

**The lesson this file encodes.** v0.3.1 shipped with every config form
returning 500 to the frontend, and so did the eighteen releases before it.
783 tests passed throughout. They passed because a unit test calls a
validator directly, and Home Assistant does not: it serialises the schema
to JSON first, and that step raises on constructs the unit test never
touches.

The defect class is **code that only executes inside Home Assistant's own
machinery** — schema serialisation, diagnostics JSON encoding, translation
lookup, entity attribute contracts. None of it runs in a unit test unless
a test deliberately reproduces the framework's step.

Every test here reproduces one of those steps.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
import voluptuous as vol
import voluptuous_serialize

from swissweather_fusion import config_flow
from swissweather_fusion.models import model_a


# ---------------------------------------------------------------------------
# The 500 class: schema serialisation
# ---------------------------------------------------------------------------
def test_no_schema_validator_is_a_bare_function():
    """A plain function inside `vol.All` cannot be serialised, and every
    form that contains one returns 500.

    Asserted structurally as well as behaviourally: the behavioural test
    below catches a broken validator, this one catches the SHAPE that
    caused it, so a future validator written the old way fails for a
    reason that names the actual problem.
    """
    import inspect

    offenders = []
    for name, obj in vars(config_flow).items():
        if not name.endswith("_VALIDATOR"):
            continue
        parts = getattr(obj, "validators", (obj,))
        for part in parts:
            if inspect.isfunction(part) or inspect.ismethod(part):
                offenders.append(f"{name} -> {part.__name__}")
    assert not offenders, (
        "validators built from plain functions cannot be rendered by Home "
        f"Assistant: {offenders}"
    )


def test_every_validator_survives_home_assistant_serialisation():
    """The behavioural half. Discovered by inspection, not listed — the
    last one was missed precisely because the list was written by hand."""
    validators = {
        name: obj for name, obj in vars(config_flow).items()
        if name.endswith("_VALIDATOR") or name in ("_finite_float", "_non_empty_str")
    }
    assert len(validators) >= 5, "discovery found suspiciously few validators"
    for name, validator in validators.items():
        try:
            voluptuous_serialize.convert(
                vol.Schema({vol.Optional("field"): validator})
            )
        except Exception as err:  # noqa: BLE001
            pytest.fail(f"{name} would make its form return 500: {err}")


def test_validators_still_reject_what_they_were_written_for():
    """Making them serialisable must not have made them permissive.

    P1-27 was about "nan" and "inf" arriving as STRINGS from a form
    field — which is why a naive `math.isfinite(float(v))` guard is not
    enough on its own and the string cases are asserted explicitly.
    """
    for bad in ("nan", "inf", "-inf", float("nan"), float("inf")):
        with pytest.raises(vol.Invalid):
            config_flow._LATITUDE_VALIDATOR(bad)
    assert config_flow._LATITUDE_VALIDATOR("47.5") == 47.5
    assert config_flow._ELEVATION_VALIDATOR("471") == 471.0

    for bad in (None, "", "   "):
        with pytest.raises(vol.Invalid):
            config_flow._non_empty_str(bad)
    assert config_flow._non_empty_str("  secret  ") == "secret"


# ---------------------------------------------------------------------------
# The 500 class: diagnostics must be JSON-encodable
# ---------------------------------------------------------------------------
def test_a_datetime_in_diagnostics_would_break_the_download():
    """Same class as the schema defect, different framework step.

    Home Assistant JSON-encodes the diagnostics payload. A `datetime`,
    `timedelta`, `set` or dataclass anywhere in it makes the download
    return 500 — and the download is the ONLY way this project's
    measurement data reaches anyone.

    This test does not inspect the real payload (that needs a live
    coordinator); it pins the encoder behaviour so the risk is stated and
    the helper below has something to check against.
    """
    with pytest.raises(TypeError):
        json.dumps({"ts": datetime.now(timezone.utc)})
    with pytest.raises(TypeError):
        json.dumps({"interval": timedelta(hours=1)})


def test_the_comparison_report_is_json_encodable():
    """The v0.3.0 measurement is delivered through the diagnostics
    download and nowhere else. If it cannot be encoded, the release has
    no output at all."""
    from swissweather_fusion.models.comparison import build_report

    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    rows = [{
        "valid_at": (base + timedelta(hours=i)).isoformat(),
        "measurement": "temperature", "lead_hours": 1, "blend_value": 10.5,
        "contributors": json.dumps(
            {"ch1": {"raw": 10.4, "debiased": 10.4, "basis": "poll"}}
        ),
        "actual_value": 10.0, "lead_time_basis": "poll",
    } for i in range(40)]

    report = build_report(rows, min_samples=30).as_dict()
    encoded = json.dumps(report)          # must not raise
    assert json.loads(encoded)["total_pairs"] == 40


# ---------------------------------------------------------------------------
# SWF-032-001 — the last forecast day collapsed by eight to ten degrees
# ---------------------------------------------------------------------------
UTC = timezone.utc


def _hour(dt: datetime, temp=None, precip=None):
    entry = {"datetime": dt.isoformat()}
    if temp is not None:
        entry["native_temperature"] = temp
    if precip is not None:
        entry["native_precipitation"] = precip
    return entry


def test_a_truncated_final_day_is_withheld_not_published_as_a_high():
    """**The reported defect, reproduced from real data.**

    Observed 2026-09-15: the final forecast day carried exactly three
    entries — 02:00, 05:00 and 08:00 local at 12.0, 11.0 and 11.4 degC —
    and was published as 12 deg / 11 deg beside genuine forecasts in the
    low twenties. Those are the coldest hours of the day, so the reported
    "high" was really the overnight minimum.
    """
    day1 = datetime(2026, 9, 20, tzinfo=UTC)
    hourly = [_hour(day1 + timedelta(hours=h), 10.0 + h, 0.0) for h in range(24)]
    # The final day stops at 08:00 — exactly the observed shape.
    day2 = datetime(2026, 9, 21, tzinfo=UTC)
    hourly += [
        _hour(day2, 12.0, 0.0),
        _hour(day2 + timedelta(hours=3), 11.0, 0.0),
        _hour(day2 + timedelta(hours=8), 11.4, 0.0),
    ]

    daily = model_a.aggregate_daily_forecast(hourly, local_tz=UTC)

    assert len(daily) == 1, "the truncated day must not be published"
    assert daily[0]["native_temperature"] == 33.0


def test_a_final_day_that_reaches_the_afternoon_is_still_published():
    """The rule must not throw away a genuinely short but usable day. A
    day sampled 06:00–15:00 contains its maximum; a day sampled
    00:00–08:00 does not."""
    day1 = datetime(2026, 9, 20, tzinfo=UTC)
    hourly = [_hour(day1 + timedelta(hours=h), 10.0, 0.0) for h in range(24)]
    day2 = datetime(2026, 9, 21, tzinfo=UTC)
    hourly += [_hour(day2 + timedelta(hours=h), 8.0 + h) for h in (6, 9, 12, 15)]

    daily = model_a.aggregate_daily_forecast(hourly, local_tz=UTC)
    assert len(daily) == 2
    assert daily[1]["native_temperature"] == 23.0


def test_today_is_never_withheld_however_late_it_is():
    """The first day is partial by construction — the forecast starts at
    `now` — and Home Assistant expects today in the daily list even at
    23:00. Applying the coverage rule to it would make the card lose
    today every evening."""
    late = datetime(2026, 9, 20, 22, tzinfo=UTC)
    hourly = [_hour(late, 12.0, 0.0), _hour(late + timedelta(hours=1), 11.0, 0.0)]

    daily = model_a.aggregate_daily_forecast(hourly, local_tz=UTC)
    assert len(daily) == 1
    assert daily[0]["native_temperature"] == 12.0


def test_the_window_is_evaluated_in_local_time_not_utc():
    """A day whose only afternoon sample is afternoon in UTC but not
    locally must be judged locally — the same v0.1.15 bug the grouping
    itself once had."""
    cest = timezone(timedelta(hours=2))
    day1 = datetime(2026, 9, 20, tzinfo=cest)
    hourly = [_hour(day1 + timedelta(hours=h), 10.0, 0.0) for h in range(24)]
    day2 = datetime(2026, 9, 21, tzinfo=cest)
    # 09:00 UTC = 11:00 CEST — outside the local afternoon window.
    hourly += [_hour(day2 + timedelta(hours=11), 15.0, 0.0)]

    assert len(model_a.aggregate_daily_forecast(hourly, local_tz=cest)) == 1


# ---------------------------------------------------------------------------
# SWF-032-002 — daily precipitation over a thinly-sampled day
# ---------------------------------------------------------------------------
def test_a_thinly_sampled_day_reports_no_precipitation_total():
    """Past the shorter sources' horizons the series drops to
    three-hourly. Summing eight three-hourly samples as though they were
    twenty-four hourly ones understates the day by roughly three times.

    None rather than a corrected estimate: whether a three-hourly value
    is an hourly rate or a three-hour accumulation is a per-provider
    question this project has not established, and multiplying by three
    on an assumption replaces a visibly-low number with a confidently
    wrong one.
    """
    day1 = datetime(2026, 9, 20, tzinfo=UTC)
    hourly = [_hour(day1 + timedelta(hours=h), 10.0, 0.1) for h in range(24)]
    day2 = datetime(2026, 9, 21, tzinfo=UTC)
    hourly += [_hour(day2 + timedelta(hours=h), 15.0, 0.1) for h in (0, 3, 6, 9, 12, 15, 18, 21)]

    daily = model_a.aggregate_daily_forecast(hourly, local_tz=UTC)
    assert len(daily) == 2
    assert daily[0]["native_precipitation"] == pytest.approx(2.4)
    assert daily[1]["native_precipitation"] is None, (
        "a three-hourly day must not publish an hourly-summed total"
    )
    # The temperature range is still reported: max/min over the available
    # samples is a real statement about those samples in a way a SUM is not.
    assert daily[1]["native_temperature"] == 15.0


def test_a_fully_sampled_day_still_sums_normally():
    day = datetime(2026, 9, 20, tzinfo=UTC)
    hourly = [_hour(day + timedelta(hours=h), 10.0, 0.25) for h in range(24)]
    daily = model_a.aggregate_daily_forecast(hourly, local_tz=UTC)
    assert daily[0]["native_precipitation"] == pytest.approx(6.0)


def test_a_day_with_no_precipitation_data_at_all_reports_none():
    day = datetime(2026, 9, 20, tzinfo=UTC)
    hourly = [_hour(day + timedelta(hours=h), 10.0) for h in range(24)]
    daily = model_a.aggregate_daily_forecast(hourly, local_tz=UTC)
    assert daily[0]["native_precipitation"] is None
