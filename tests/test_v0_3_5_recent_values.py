"""Regression tests for v0.3.5 — `recent_values` on the reference station.

Requested by the solar layer (Open-Meteo Solar Forecast learning hub,
request of 13 Oct 2026): every 10-minute record of the last hours, so it
can build measured hours with its strict 5-of-6 rule. On the owner's
system the dataset sensor showed only 10 of 19 records between 04:40 and
07:40 UTC: MeteoSwiss publishes with a varying delay, so a 10-minute poll
sometimes sees the same newest record twice and never sees the next one
as "newest".

The MeteoSwiss "now" file holds every record from yesterday 12 UTC to the
present (MeteoSwiss open-data download documentation), so the records a
poll skipped are in the file the integration already downloads.

Contract checked here (request §2): time = end of interval, UTC ISO on the
10-minute grid; same parameters as `values`; every published record;
nothing invented; ascending, no duplicates; 6-hour window (36 records);
unrecorded; complete at the first update after a restart; everything else
unchanged.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from swissweather_fusion.clients import meteoswiss_smn as smn
from swissweather_fusion.const import REFERENCE_STATION_RECENT_WINDOW

from .test_v0_3_4_reference_station import (
    HEADER,
    NOW_FILE,
    ROWS,
    FakeClient,
    _coordinator,
    _files,
)

UTC = timezone.utc
STEP = timedelta(minutes=10)
WINDOW = REFERENCE_STATION_RECENT_WINDOW
# Field values of a real Taenikon record, everything after the timestamp.
_TEMPLATE = ROWS[0].split(";", 2)[2]


def _row(t: datetime, template: str = _TEMPLATE) -> str:
    return f"TAE;{t:%d.%m.%Y %H:%M};{template}"


def _now_file(newest: datetime, *, missing=(), extra_lines=(), shuffle=False) -> str:
    """A 'now' file like MeteoSwiss's: yesterday 12 UTC up to `newest`."""
    start = (newest - timedelta(days=1)).replace(hour=12, minute=0)
    times, t = [], start
    while t <= newest:
        if t not in missing:
            times.append(t)
        t += STEP
    lines = [_row(t) for t in times]
    if shuffle:
        lines = lines[1::2] + lines[0::2]
    return "\n".join([HEADER, *lines, *extra_lines]) + "\n"


NEWEST = datetime(2026, 10, 13, 7, 40, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Parsing (request §2 rules 1-6)
# ---------------------------------------------------------------------------
def test_window_holds_36_records_oldest_first_ending_at_the_newest():
    records = smn.parse_records(_now_file(NEWEST), WINDOW)
    times = [r.measured_at for r in records]
    assert len(records) == 36
    assert times[0] == NEWEST - WINDOW + STEP
    assert times[-1] == NEWEST
    assert all(b - a == STEP for a, b in zip(times, times[1:]))   # no 20-minute jumps


def test_window_is_at_least_three_and_at_most_six_hours():
    assert timedelta(hours=3) <= WINDOW <= timedelta(hours=6)


def test_file_order_does_not_matter():
    records = smn.parse_records(_now_file(NEWEST, shuffle=True), WINDOW)
    times = [r.measured_at for r in records]
    assert times == sorted(times) and len(times) == 36


def test_a_record_meteoswiss_did_not_publish_stays_absent():
    gap = NEWEST - 7 * STEP
    records = smn.parse_records(_now_file(NEWEST, missing={gap}), WINDOW)
    times = [r.measured_at for r in records]
    assert gap not in times
    assert len(records) == 35                     # not filled, not interpolated
    assert times[0] == NEWEST - WINDOW + STEP     # window not stretched to make up for it


def test_empty_fields_are_null_and_every_parameter_is_kept():
    records = smn.parse_records(_now_file(NEWEST), WINDOW)
    for r in records:
        assert len(r.values) == 32
        assert r.values["ppz850s0"] is None       # empty at Taenikon
        assert r.values["gre000z0"] == 250.0


def test_values_carry_the_same_names_as_the_values_attribute():
    text = _now_file(NEWEST)
    latest = smn.parse_latest(text)
    records = smn.parse_records(text, WINDOW)
    assert set(records[-1].values) == set(latest.values)
    assert records[-1].values == latest.values
    assert records[-1].measured_at == latest.measured_at


def test_duplicate_timestamp_keeps_the_first_row_like_parse_latest():
    other = _TEMPLATE.replace("12.1;14.7", "99.9;14.7", 1)
    text = _now_file(NEWEST, extra_lines=[_row(NEWEST, other)])
    records = smn.parse_records(text, WINDOW)
    assert [r.measured_at for r in records].count(NEWEST) == 1
    assert records[-1].values["tre200s0"] == 12.1
    assert smn.parse_latest(text).values == records[-1].values


def test_rows_off_the_ten_minute_grid_are_skipped():
    odd = NEWEST - timedelta(minutes=35)
    records = smn.parse_records(_now_file(NEWEST, extra_lines=[_row(odd)]), WINDOW)
    assert odd not in [r.measured_at for r in records]
    assert len(records) == 36


def test_window_is_anchored_on_the_newest_record_not_the_clock():
    old = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
    assert len(smn.parse_records(_now_file(old), WINDOW)) == 36


def test_short_file_gives_what_exists():
    records = smn.parse_records(NOW_FILE, WINDOW)    # the three real v0.3.4 records
    assert [r.measured_at.minute for r in records] == [20, 30, 40]


def test_unusable_files_give_an_empty_list():
    assert smn.parse_records("", WINDOW) == []
    assert smn.parse_records("a;b\n1;2\n", WINDOW) == []
    assert smn.parse_records(HEADER + "\n", WINDOW) == []


# ---------------------------------------------------------------------------
# The problem the request describes, and that this closes (request §1, §3)
# ---------------------------------------------------------------------------
def _published_file(poll: datetime, delay: timedelta) -> str:
    """What MeteoSwiss serves at `poll` if the newest record lags by `delay`."""
    newest = (poll - delay).replace(minute=(poll - delay).minute // 10 * 10,
                                    second=0, microsecond=0)
    return _now_file(newest)


def test_varying_publication_delay_skips_records_for_latest_but_not_for_recent():
    start = datetime(2026, 10, 13, 4, 45, tzinfo=UTC)
    delays = [timedelta(minutes=m) for m in (12, 22, 12, 22, 19, 11, 21, 13, 22, 12)]
    seen_latest, seen_recent = set(), set()
    for i, delay in enumerate(delays * 2):
        text = _published_file(start + i * STEP, delay)
        seen_latest.add(smn.parse_latest(text).measured_at)
        seen_recent.update(r.measured_at for r in smn.parse_records(text, WINDOW))
    span = [t for t in sorted(seen_recent) if t >= min(seen_latest)]
    # the latest-only view misses records (the solar layer's observation) ...
    assert len(seen_latest) < len(span)
    # ... the recent list has every record of every hour: 6 of 6
    for hour in {t.replace(minute=0) for t in span if t.replace(minute=0) + timedelta(hours=1) <= max(span)}:
        assert sum(1 for t in seen_recent if t.replace(minute=0) == hour) == 6


# ---------------------------------------------------------------------------
# Coordinator and sensor (rules 7-9)
# ---------------------------------------------------------------------------
def _poll(text=None, coordinator=None):
    files = _files(**({smn.now_url("TAE"): text} if text else {}))
    c = coordinator or _coordinator(FakeClient(files))
    if coordinator is not None:
        c._client = FakeClient(files)
    return c, asyncio.run(c._async_update_data())


def test_coordinator_publishes_recent_records_as_iso_utc():
    _, data = _poll(_now_file(NEWEST))
    recent = data["recent"]
    assert len(recent) == 36
    assert recent[0]["time"] == (NEWEST - WINDOW + STEP).isoformat()
    assert recent[-1]["time"] == data["measured_at"] == "2026-10-13T07:40:00+00:00"
    assert recent[-1]["values"] == data["values"]
    assert all(set(r) == {"time", "values"} for r in recent)
    assert json.loads(json.dumps(data)) == data


def test_complete_again_at_the_first_update_after_a_restart():
    # a brand-new coordinator (as after a Home Assistant restart) has no memory
    _, data = _poll(_now_file(NEWEST))
    assert len(data["recent"]) == 36


def test_no_record_survives_from_memory_between_polls():
    c, first = _poll(_now_file(NEWEST))
    gap = NEWEST + STEP - 5 * STEP
    _, second = _poll(_now_file(NEWEST + STEP, missing={gap}), coordinator=c)
    times = [r["time"] for r in second["recent"]]
    assert gap.isoformat() in [r["time"] for r in first["recent"]]
    assert gap.isoformat() not in times          # the source decides, not a buffer


def test_one_download_per_poll_still():
    c, _ = _poll(_now_file(NEWEST))
    files = _files(**{smn.now_url("TAE"): _now_file(NEWEST)})
    c._client = client = FakeClient(files)
    asyncio.run(c._async_update_data())
    assert client.calls == [smn.now_url("TAE")]


def _sensor(data):
    from types import SimpleNamespace

    from swissweather_fusion.sensor import ReferenceStationSensor as S

    entity = object.__new__(S)
    entity._runtime = {"reference_station_coordinator": SimpleNamespace(data=data)}
    return S, entity


def test_sensor_exposes_recent_values_and_keeps_it_out_of_the_recorder():
    _, data = _poll(_now_file(NEWEST))
    S, entity = _sensor(data)
    attrs = S.extra_state_attributes.fget(entity)
    assert attrs["recent_values"] == data["recent"]
    assert "recent_values" in S._unrecorded_attributes
    assert {"values", "parameters", "station"} <= S._unrecorded_attributes
    assert "recent_values" in attrs["semantics"]


def test_existing_attributes_are_unchanged():
    _, data = _poll(_now_file(NEWEST))
    S, entity = _sensor(data)
    attrs = S.extra_state_attributes.fget(entity)
    assert set(attrs) == {"station", "age_minutes", "stale", "values",
                          "parameters", "recent_values", "semantics"}
    assert attrs["values"] == data["values"]
    assert S.native_value.fget(entity) == NEWEST


def test_sensor_without_data_gives_an_empty_list():
    S, entity = _sensor(None)
    assert S.extra_state_attributes.fget(entity)["recent_values"] == []


def test_attribute_size_stays_modest():
    _, data = _poll(_now_file(NEWEST))
    size = len(json.dumps(data["recent"]))
    assert size < 40_000, size
