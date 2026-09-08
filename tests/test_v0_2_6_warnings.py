"""Tests for v0.2.6 — official severe-weather warnings.

**Why this source, after four rejections.** MeteoSwiss publishes no
warnings in its open-data catalogue (categories A-E, no warnings
category; individual API access "not available before end of 2026").
MeteoNews and wetter.de are commercial with no open contract — the
former's warning page is robots.txt-disallowed. meteoblue's Warnings API
returned 403 "Access to the package API is not available for this user".
SRF's official API does not expose warnings, confirmed by a developer who
asked them directly. Wetter-Alarm's endpoint requires no key.

**The design constraint driving these tests:** surface the warning
without losing what makes it a warning. A severity level, a hazard type,
a validity window and an advisory text are not interchangeable, and none
survives being averaged into a float.
"""
from datetime import datetime, timezone

import pytest

from swissweather_fusion.clients import wetteralarm as wa
from swissweather_fusion.const import (
    WETTERALARM_MAX_POI_DISTANCE_KM,
    WETTERALARM_POLL_INTERVAL,
)

POI = 31547

# Inside the fixtures' validity window. Injected explicitly rather than
# relying on the wall clock: warnings carry a validity window, and a test
# whose result depends on the date it is run is not a test.
NOW = datetime(2026, 9, 8, 15, 0, tzinfo=timezone.utc)

# Shaped after the upstream client's documented field access — id,
# priority, poi_ids, valid_from/to, region[lang][name], [lang][title].
PAYLOAD = {
    "meteo_alarms": [
        {
            "id": 90210,
            "priority": 2,
            "poi_ids": [POI, 99999],
            "valid_from": "2026-09-08T12:00:00.000Z",
            "valid_to": "2026-09-08T20:00:00.000Z",
            "region": {"de": {"name": "Thurgau"}, "en": {"name": "Thurgau"}},
            "de": {
                "title": "Gewitter",
                "hint": "Vor Blitzschlag schützen.",
                "signature": "Wetter-Alarm",
            },
            "en": {"title": "Thunderstorm", "hint": "Seek shelter."},
        },
        {
            "id": 90211,
            "priority": 3,
            "poi_ids": [11111],          # a different region
            "valid_from": "2026-09-08T12:00:00.000Z",
            "valid_to": "2026-09-08T20:00:00.000Z",
            "region": {"de": {"name": "Wallis"}},
            "de": {"title": "Sturm"},
        },
    ]
}


# ---------------------------------------------------------------------------
# Parsing — nothing may be lost at the client boundary
# ---------------------------------------------------------------------------
def test_warning_for_our_location_is_found_among_national_alarms():
    """The endpoint returns EVERY active alarm in Switzerland and expects
    client-side filtering — there is no per-location query."""
    warning = wa.parse_alarms_response(PAYLOAD, POI, now=NOW)
    assert warning.is_active
    assert warning.alarm_id == 90210
    assert warning.title == "Gewitter"


def test_alarms_for_other_regions_are_ignored():
    warning = wa.parse_alarms_response(PAYLOAD, 11111, now=NOW)
    assert warning.alarm_id == 90211
    assert warning.title == "Sturm"


def test_no_matching_alarm_yields_an_inactive_warning_not_none():
    """Callers must never have to distinguish "no warning" from "lookup
    failed" by checking for None."""
    warning = wa.parse_alarms_response(PAYLOAD, 55555, now=NOW)
    assert warning is not None
    assert not warning.is_active
    assert warning.level == wa.LEVEL_NONE


def test_empty_national_list_is_the_normal_case():
    """Most days have no active alarm anywhere in the country."""
    assert not wa.parse_alarms_response({"meteo_alarms": []}, POI).is_active
    assert not wa.parse_alarms_response({}, POI).is_active


def test_every_field_survives_parsing():
    """The client must not summarise. A field discarded here is a
    decision made unilaterally and irreversibly on behalf of every
    consumer downstream."""
    w = wa.parse_alarms_response(PAYLOAD, POI, now=NOW)
    assert w.priority == 2
    assert w.region == "Thurgau"
    assert w.hint == "Vor Blitzschlag schützen."
    assert w.signature == "Wetter-Alarm"
    assert w.valid_from == datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    assert w.valid_to == datetime(2026, 9, 8, 20, tzinfo=timezone.utc)
    # And the untouched payload, so a field we do not yet interpret is
    # still recoverable rather than lost.
    assert '"id": 90210' in w.raw_json


def test_language_falls_back_rather_than_returning_nothing():
    """A warning in the wrong language is far better than no warning."""
    assert wa.parse_alarms_response(PAYLOAD, POI, language="fr", now=NOW).title == "Gewitter"
    assert wa.parse_alarms_response(PAYLOAD, POI, language="en", now=NOW).title == "Thunderstorm"


@pytest.mark.parametrize(
    "priority,expected", [(1, "yellow"), (2, "orange"), (3, "red")]
)
def test_documented_priority_levels_map_to_the_public_colour_scale(priority, expected):
    payload = {"meteo_alarms": [
        {"id": 1, "priority": priority, "poi_ids": [POI], "de": {"title": "x"}}
    ]}
    assert wa.parse_alarms_response(payload, POI).level == expected


def test_unrecognised_priority_is_preserved_not_coerced():
    """The numeric scale is not documented anywhere this project can
    verify. Coercing an unknown value into a level it may not mean is
    exactly the CombiPrecip filename mistake — encoding a
    documented-looking convention without checking it against reality."""
    payload = {"meteo_alarms": [
        {"id": 1, "priority": 7, "poi_ids": [POI], "de": {"title": "x"}}
    ]}
    warning = wa.parse_alarms_response(payload, POI)
    assert warning.level == "7"
    assert warning.priority == 7


def test_malformed_timestamps_degrade_rather_than_raise():
    """The upstream client assumed one exact strptime format and would
    raise on anything else. A warning source that crashes on an
    unexpected date is worse than one reporting the warning untimed."""
    payload = {"meteo_alarms": [{
        "id": 1, "priority": 1, "poi_ids": [POI],
        "valid_from": "not a date", "de": {"title": "Gewitter"},
    }]}
    warning = wa.parse_alarms_response(payload, POI)
    assert warning.is_active
    assert warning.valid_from is None


def test_malformed_payload_shapes_do_not_raise():
    for payload in ({"meteo_alarms": None}, {"meteo_alarms": ["junk"]}, {}):
        assert not wa.parse_alarms_response(payload, POI).is_active


# ---------------------------------------------------------------------------
# Location resolution
# ---------------------------------------------------------------------------
def test_nearest_town_is_selected_from_coordinates():
    """Asking a user for a numeric POI id would be poor configuration
    when their coordinates are already known."""
    index = [[1, 46.9481, 7.4474], [2, 47.3769, 8.5417]]
    poi_id, distance = wa.find_nearest_poi(46.95, 7.45, index)
    assert poi_id == 1
    assert distance < 1.0


def test_distant_location_is_reported_with_its_real_distance():
    """Beyond the coverage limit the coordinator declines rather than
    silently matching — 'no warnings' and 'wrong country' must not look
    identical."""
    index = [[1, 46.9481, 7.4474]]
    _, distance = wa.find_nearest_poi(52.52, 13.40, index)   # Berlin
    assert distance > WETTERALARM_MAX_POI_DISTANCE_KM


def test_bundled_poi_index_loads_and_is_usable():
    index = wa.load_poi_index()
    assert len(index) > 1000
    poi_id, distance = wa.find_nearest_poi(47.5536, 8.9120, index)
    assert isinstance(poi_id, int)
    assert distance < WETTERALARM_MAX_POI_DISTANCE_KM


def test_empty_index_disables_the_feature_rather_than_raising():
    assert wa.find_nearest_poi(46.9, 7.4, []) is None


# ---------------------------------------------------------------------------
# Polling discipline
# ---------------------------------------------------------------------------
def test_poll_interval_is_sane_for_the_event_class():
    """The upstream project polled every 60 SECONDS — 1,440 requests/day
    against an unauthenticated third-party endpoint, for the rarest event
    class here, more aggressive than this project's radar polling.
    Warnings are issued hours ahead by a human-supervised process."""
    from datetime import timedelta

    assert WETTERALARM_POLL_INTERVAL >= timedelta(minutes=10)


def test_client_uses_an_injected_session():
    """Creating an aiohttp.ClientSession per request is a documented
    antipattern that the upstream project fell into; Home Assistant
    provides a shared pooled session."""
    import inspect

    source = inspect.getsource(wa.WetterAlarmClient)
    assert "ClientSession()" not in source
    assert "self._session" in source


# ---------------------------------------------------------------------------
# The information-preservation decisions
# ---------------------------------------------------------------------------
def test_warning_is_not_folded_into_the_storm_score():
    """The storm score is max() of three heuristics this project
    invented. Adding an authority's judgement would make a high reading
    ambiguous between "our unvalidated rule fired" and "a meteorologist
    issued a warning" — two things warranting different responses, which
    one number cannot express. It would also collapse hazard type,
    validity window and advisory text into a float."""
    import inspect

    from swissweather_fusion.models import model_b

    source = inspect.getsource(model_b.score_v0_graduated)
    assert "warning" not in source.lower()
    assert "wetteralarm" not in source.lower()


def test_sensor_state_is_the_severity_and_detail_lives_in_attributes():
    """One entity carrying the whole warning: the state is the thing you
    automate on, every other field is an attribute. The upstream project
    split this across eight sensors, seven of which are meaningless
    alone."""
    from swissweather_fusion.sensor import OfficialWarningSensor

    sensor = object.__new__(OfficialWarningSensor)
    sensor._runtime = {
        "wetteralarm_coordinator": type("C", (), {
            "warning": wa.parse_alarms_response(PAYLOAD, POI, now=NOW),
            "poi_id": POI,
            "poi_distance_km": 1.2,
        })()
    }
    assert OfficialWarningSensor.native_value.fget(sensor) == "orange"

    attrs = OfficialWarningSensor.extra_state_attributes.fget(sensor)
    for field in ("title", "hint", "region", "valid_from", "valid_to",
                  "priority", "alarm_id", "signature"):
        assert field in attrs, f"{field} was lost between client and entity"
    assert attrs["active"] is True


def test_severity_is_text_not_a_number():
    """Levels are ordinal categories issued by an authority, not a
    measurement. Rendering them 1/2/3 invites arithmetic that means
    nothing — the gap between yellow and orange is not the gap between
    orange and red."""
    from swissweather_fusion.sensor import OfficialWarningSensor

    sensor = object.__new__(OfficialWarningSensor)
    sensor._runtime = {
        "wetteralarm_coordinator": type("C", (), {
            "warning": wa.parse_alarms_response(PAYLOAD, POI, now=NOW),
            "poi_id": POI, "poi_distance_km": 1.0,
        })()
    }
    assert isinstance(OfficialWarningSensor.native_value.fget(sensor), str)


def test_binary_sensor_mirrors_the_same_fact_for_automations():
    from swissweather_fusion.binary_sensor import OfficialWarningActiveBinarySensor

    s = object.__new__(OfficialWarningActiveBinarySensor)
    s._runtime = {"wetteralarm_coordinator": type("C", (), {
        "warning": wa.parse_alarms_response(PAYLOAD, POI, now=NOW)
    })()}
    assert OfficialWarningActiveBinarySensor.is_on.fget(s) is True

    s._runtime = {"wetteralarm_coordinator": type("C", (), {
        "warning": wa.WeatherWarning()
    })()}
    assert OfficialWarningActiveBinarySensor.is_on.fget(s) is False


def test_sensor_is_blank_before_the_first_poll():
    from swissweather_fusion.sensor import OfficialWarningSensor

    sensor = object.__new__(OfficialWarningSensor)
    sensor._runtime = {}
    assert OfficialWarningSensor.native_value.fget(sensor) is None


# ---------------------------------------------------------------------------
# Post-import audit findings (v0.2.6 audit pass)
# ---------------------------------------------------------------------------
# The client above was written informed by redlukas/wetter-alarm and then
# audited separately. These cover defects that audit found — two of which
# the upstream project also has, and one of which directly contradicted
# this release's own stated design principle.
def test_concurrent_warnings_are_all_returned(  ):
    """SWF-026-002. The first implementation returned the FIRST matching
    alarm and dropped the rest.

    Concurrent warnings are normal — frost and thunderstorm can be in
    force at once — and list order carries no meaning, so a red
    thunderstorm warning could be silently discarded in favour of a
    yellow frost one that happened to come first. That is exactly the
    information loss this release was scoped to prevent, reintroduced at
    the parsing layer.
    """
    payload = {"meteo_alarms": [
        {"id": 1, "priority": 1, "poi_ids": [POI], "de": {"title": "Frost"}},
        {"id": 2, "priority": 3, "poi_ids": [POI], "de": {"title": "Gewitter"}},
    ]}
    warnings = wa.parse_all_alarms(payload, POI, now=NOW)
    assert len(warnings) == 2
    assert {w.title for w in warnings} == {"Frost", "Gewitter"}


def test_headline_warning_is_the_most_severe_not_the_first():
    """The single-value entity state must not depend on response order."""
    payload = {"meteo_alarms": [
        {"id": 1, "priority": 1, "poi_ids": [POI], "de": {"title": "Frost"}},
        {"id": 2, "priority": 3, "poi_ids": [POI], "de": {"title": "Gewitter"}},
    ]}
    headline = wa.parse_alarms_response(payload, POI, now=NOW)
    assert headline.title == "Gewitter"
    assert headline.level == "red"


def test_expired_warnings_are_excluded():
    """SWF-026-003. Publishers do not always remove alarms promptly, and
    the upstream project never checked the window. Reporting an expired
    storm warning as active is worse than reporting nothing — it is a
    false alarm that looks authoritative."""
    payload = {"meteo_alarms": [{
        "id": 9, "priority": 3, "poi_ids": [POI],
        "valid_from": "2026-09-01T00:00:00.000Z",
        "valid_to": "2026-09-02T00:00:00.000Z",
        "de": {"title": "Sturm"},
    }]}
    assert not wa.parse_alarms_response(payload, POI, now=NOW).is_active


def test_not_yet_active_warnings_are_excluded():
    """A warning issued for tomorrow is real but not in force now."""
    payload = {"meteo_alarms": [{
        "id": 9, "priority": 3, "poi_ids": [POI],
        "valid_from": "2026-09-20T00:00:00.000Z",
        "valid_to": "2026-09-21T00:00:00.000Z",
        "de": {"title": "Sturm"},
    }]}
    assert not wa.parse_alarms_response(payload, POI, now=NOW).is_active


def test_warning_without_a_parseable_window_is_treated_as_current():
    """Discarding a real warning over a formatting problem would be the
    wrong direction to fail."""
    payload = {"meteo_alarms": [
        {"id": 9, "priority": 3, "poi_ids": [POI], "de": {"title": "Sturm"}}
    ]}
    assert wa.parse_alarms_response(payload, POI, now=NOW).is_active


def test_warning_is_hashable():
    """SWF-026-006. A frozen dataclass holding a dict is unhashable,
    which would break any future use in a set or as a dict key. The raw
    payload is kept as JSON text instead — still recoverable, still
    immutable."""
    payload = {"meteo_alarms": [
        {"id": 1, "priority": 1, "poi_ids": [POI], "de": {"title": "x"}}
    ]}
    warning = wa.parse_alarms_response(payload, POI, now=NOW)
    assert hash(warning) is not None
    assert warning.raw_json is not None


@pytest.mark.parametrize("poi_ids", [None, "not-a-list", 42, {}])
def test_malformed_poi_ids_do_not_raise(poi_ids):
    payload = {"meteo_alarms": [{"id": 1, "poi_ids": poi_ids, "de": {}}]}
    assert not wa.parse_alarms_response(payload, POI, now=NOW).is_active


def test_poi_search_runs_off_the_event_loop():
    """SWF-026-004. The file read was dispatched to an executor but the
    6,261-entry haversine scan was not. Small (~3 ms) but pure CPU work
    on the loop for no reason — and this project has already shipped one
    blocking-on-the-loop defect."""
    import inspect

    from swissweather_fusion import coordinator as coord

    source = inspect.getsource(coord.WetterAlarmCoordinator._async_resolve_poi)
    body = source.split('"""')[-1]
    assert "find_nearest_poi" in body
    # It must appear inside the function passed to the executor, not after it.
    executor_call = body.index("async_add_executor_job")
    assert body.index("find_nearest_poi") < executor_call, (
        "the POI scan runs on the event loop"
    )


def test_wetteralarm_health_is_surfaced():
    """SWF-026-005. The coordinator creates a SourceHealth like every
    other source; omitting it from the telemetry list meant that health
    was recorded and never shown — no last-success sensor, no
    contribution to the degraded/status entities. A source that can fail
    silently is one whose failures are found late."""
    from swissweather_fusion.const import SOURCE_WETTERALARM
    from swissweather_fusion.sensor import ALL_TELEMETRY_SOURCES

    assert SOURCE_WETTERALARM in ALL_TELEMETRY_SOURCES


def test_concurrent_warnings_reach_the_entity():
    """Surfacing only the headline would repeat the parsing-layer
    information loss one level up."""
    from swissweather_fusion.sensor import OfficialWarningSensor

    payload = {"meteo_alarms": [
        {"id": 1, "priority": 1, "poi_ids": [POI], "de": {"title": "Frost"}},
        {"id": 2, "priority": 3, "poi_ids": [POI], "de": {"title": "Gewitter"}},
    ]}
    sensor = object.__new__(OfficialWarningSensor)
    sensor._runtime = {"wetteralarm_coordinator": type("C", (), {
        "warning": wa.parse_alarms_response(payload, POI, now=NOW),
        "warnings": wa.parse_all_alarms(payload, POI, now=NOW),
        "poi_id": POI, "poi_distance_km": 1.0,
    })()}
    attrs = OfficialWarningSensor.extra_state_attributes.fget(sensor)
    assert attrs["warning_count"] == 2
    assert len(attrs["concurrent_warnings"]) == 2


def test_upstream_mit_licence_is_included():
    """SWF-026-007. The bundled POI index is derived from
    redlukas/wetter-alarm, which is MIT licensed. MIT requires the
    copyright notice be retained in copies or substantial portions.
    Attribution in a docstring is courtesy; the notice file is the
    obligation."""
    import pathlib

    root = pathlib.Path(__file__).parent.parent
    notice = root / "LICENSES" / "wetter-alarm-MIT.txt"
    assert notice.exists(), "upstream MIT notice is not distributed"
    text = notice.read_text()
    assert "MIT" in text and "redlukas" in text


# ---------------------------------------------------------------------------
# Performance and resilience audit (v0.2.6)
# ---------------------------------------------------------------------------
def test_optimised_search_returns_the_same_answer_as_brute_force():
    """SWF-026-008. The bounding-box pre-filter is an OPTIMISATION, not
    an approximation: it must never change which POI is selected.

    A degree of latitude is ~111 km everywhere and a degree of longitude
    is ~111 km x cos(lat), so using 111 km for both is conservative and
    cannot reject a candidate haversine would have accepted.
    """
    index = wa.load_poi_index()
    for lat, lon in [(47.5536, 8.9120), (46.9481, 7.4474), (46.2044, 6.1432)]:
        optimised = wa.find_nearest_poi(lat, lon, index)
        brute = None
        for poi_id, plat, plon in index:
            d = wa._haversine_km(lat, lon, plat, plon)
            if brute is None or d < brute[1]:
                brute = (int(poi_id), d)
        assert optimised[0] == brute[0], f"different POI chosen at {lat},{lon}"
        assert optimised[1] == pytest.approx(brute[1])


def test_search_rejects_most_candidates_without_trigonometry():
    """SWF-026-008, guarded by WORK DONE rather than wall-clock time.

    A first attempt at this test asserted an elapsed-time threshold. It
    passed with the optimisation removed, because wall-clock limits are
    machine-dependent and a fast runner absorbs a 50x regression without
    crossing an absolute bound. A timing test that cannot fail is not a
    test — the same "success condition satisfiable without the code
    working" failure recorded in the remediation audit's section 9.8.

    Counting haversine calls measures the thing the optimisation actually
    changes, and is deterministic on any hardware.
    """
    index = wa.load_poi_index()
    calls = {"n": 0}
    original = wa._haversine_km

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    wa._haversine_km = counting
    try:
        wa.find_nearest_poi(47.5536, 8.9120, index)
    finally:
        wa._haversine_km = original

    assert calls["n"] < len(index) / 4, (
        f"{calls['n']} haversine calls over {len(index)} entries — the "
        "bounding-box pre-filter is not rejecting candidates"
    )


def test_out_of_coverage_location_still_resolves_via_full_scan():
    """The bounding box starts narrow. A location with nothing inside it
    must fall back to a complete scan rather than reporting no coverage —
    the caller decides what to do with a distant match, not the search."""
    index = wa.load_poi_index()
    result = wa.find_nearest_poi(52.52, 13.40, index)   # Berlin
    assert result is not None
    assert result[1] > 100


def test_transient_index_failure_is_retried():
    """SWF-026-009. `_poi_resolved` was set BEFORE the try, so one
    transient failure — a slow disk, a momentary I/O error at startup —
    permanently disabled official warnings for the whole process
    lifetime, with no retry until Home Assistant restarted.

    Failing permanently on a transient error is the wrong default.
    """
    import inspect

    from swissweather_fusion import coordinator as coord

    source = inspect.getsource(coord.WetterAlarmCoordinator._async_resolve_poi)
    body = source.split('"""')[-1]
    # The flag must be set only after a successful resolution, i.e. after
    # the executor call rather than before the try.
    assert body.index("async_add_executor_job") < body.index(
        "self._poi_resolved = True"
    ), "resolution is marked complete before it has succeeded"


def test_poi_index_is_not_retained_after_resolution():
    """The parsed index costs ~1 MB of Python objects — far more than the
    144 KB on disk. It is needed once, at startup, and holding it for the
    process lifetime would be a permanent cost for a one-off lookup."""
    import inspect

    from swissweather_fusion import coordinator as coord

    source = inspect.getsource(coord.WetterAlarmCoordinator)
    assert "self._index" not in source
    assert "self.poi_index" not in source


# ---------------------------------------------------------------------------
# v0.2.7 (SWF-027-001) — pre-warning visibility
# ---------------------------------------------------------------------------
# A live installation compared this integration's sensor against
# Wetter-Alarm's own website at 15:35 CET (13:35 UTC) and found a real
# gap: the website showed "Gewittergefahr, gultig ab 20:00" while the
# sensor reported "none". Both were correct for what they answer —
# is_current() deliberately excludes a not-yet-started warning, which is
# what stops an EXPIRED warning showing as active (SWF-026-003) — but the
# sensor was answering the wrong question for a dashboard.
REAL_PAYLOAD = {"meteo_alarms": [{
    "id": 338253, "priority": 1,
    "valid_from": "2026-09-08T18:00:00.000Z",
    "valid_to": "2026-09-08T23:00:00.000Z",
    "poi_ids": [145140],
    "de": {"title": "Gewittergefahr", "hint": "lose Gegenstände sichern"},
    "region": {"de": {"name": "Frauenfeld"}},
}]}
SCREENSHOT_TIME = datetime(2026, 9, 8, 13, 35, tzinfo=timezone.utc)  # 15:35 CEST


def test_reproduces_the_reported_gap_with_is_current():
    """The exact scenario. This must stay False: is_current() answers
    "in force right now" and must not change meaning."""
    warning = wa.parse_alarms_response(REAL_PAYLOAD, 145140, now=SCREENSHOT_TIME)
    assert not warning.is_active


def test_is_upcoming_closes_the_gap_at_the_same_moment():
    """The fix, checked at the exact reported time."""
    from swissweather_fusion.const import WETTERALARM_LOOKAHEAD

    warnings = wa.parse_all_alarms(
        REAL_PAYLOAD, 145140, now=SCREENSHOT_TIME, lookahead=WETTERALARM_LOOKAHEAD
    )
    assert warnings and warnings[0].title == "Gewittergefahr"


def test_is_upcoming_respects_the_lookahead_boundary():
    """A warning 7 hours out must not appear with a 6-hour lookahead —
    the point of a bound is that it bounds."""
    far_future = datetime(2026, 9, 8, 10, 59, tzinfo=timezone.utc)  # 7h1m before 18:00
    from datetime import timedelta

    matched = [
        w for w in wa._iter_matching_alarms(REAL_PAYLOAD, 145140, "de")
        if w.is_upcoming(timedelta(hours=6), far_future)
    ]
    assert not matched


def test_is_upcoming_true_exactly_at_the_lookahead_edge():
    from datetime import timedelta

    exactly_6h_before = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
    matched = [
        w for w in wa._iter_matching_alarms(REAL_PAYLOAD, 145140, "de")
        if w.is_upcoming(timedelta(hours=6), exactly_6h_before)
    ]
    assert matched


def test_is_upcoming_still_true_once_the_warning_is_actually_active():
    """A currently-active warning must also satisfy is_upcoming — the
    dashboard should not lose sight of it the moment it starts."""
    from datetime import timedelta

    now = datetime(2026, 9, 8, 19, 0, tzinfo=timezone.utc)  # during the window
    matched = [
        w for w in wa._iter_matching_alarms(REAL_PAYLOAD, 145140, "de")
        if w.is_upcoming(timedelta(hours=6), now)
    ]
    assert matched


def test_is_upcoming_excludes_an_expired_warning():
    """The SWF-026-003 guarantee must survive: is_upcoming is a superset
    of is_current going forward in time, never backward."""
    from datetime import timedelta

    after_expiry = datetime(2026, 9, 9, 1, 1, tzinfo=timezone.utc)  # 1 min past valid_to
    matched = [
        w for w in wa._iter_matching_alarms(REAL_PAYLOAD, 145140, "de")
        if w.is_upcoming(timedelta(hours=6), after_expiry)
    ]
    assert not matched


def test_default_lookahead_is_zero_so_existing_callers_are_unaffected():
    """Every caller that does not opt in gets exactly is_current()'s
    behaviour — the fix must be additive, not a silent behaviour change
    for automations already depending on the strict definition."""
    with_no_lookahead = wa.parse_all_alarms(REAL_PAYLOAD, 145140, now=SCREENSHOT_TIME)
    assert with_no_lookahead == []


def test_coordinator_exposes_upcoming_without_a_second_fetch():
    """Both current and upcoming views are derived from the SAME poll —
    no extra request against the unauthenticated endpoint."""
    import inspect

    from swissweather_fusion import coordinator as coord

    source = inspect.getsource(coord.WetterAlarmCoordinator._async_update_data)
    assert source.count("async_fetch_alarms") == 1
    assert "upcoming_warnings" in source


def test_next_warning_attribute_surfaces_the_soonest_pre_warning():
    from swissweather_fusion.sensor import OfficialWarningSensor

    sensor = object.__new__(OfficialWarningSensor)
    sensor._runtime = {"wetteralarm_coordinator": type("C", (), {
        "warning": wa.WeatherWarning(),  # nothing active
        "warnings": [],
        "upcoming_warnings": wa.parse_all_alarms(
            REAL_PAYLOAD, 145140, now=SCREENSHOT_TIME,
            lookahead=__import__("datetime").timedelta(hours=6),
        ),
        "poi_id": 145140, "poi_distance_km": 0.5,
    })()}
    attrs = OfficialWarningSensor.extra_state_attributes.fget(sensor)
    assert attrs["next_warning"]["title"] == "Gewittergefahr"
    assert attrs["next_warning"]["valid_from"] == "2026-09-08T18:00:00+00:00"


def test_no_next_warning_key_when_nothing_upcoming():
    """The attribute should not appear at all rather than being present
    and null — cleaner for a template checking `next_warning is defined`."""
    from swissweather_fusion.sensor import OfficialWarningSensor

    sensor = object.__new__(OfficialWarningSensor)
    sensor._runtime = {"wetteralarm_coordinator": type("C", (), {
        "warning": wa.WeatherWarning(), "warnings": [],
        "upcoming_warnings": [],
        "poi_id": 145140, "poi_distance_km": 0.5,
    })()}
    attrs = OfficialWarningSensor.extra_state_attributes.fget(sensor)
    assert "next_warning" not in attrs


def test_already_active_warning_is_not_duplicated_as_next_warning():
    """Once a warning is active it belongs in the main state; it must
    not also appear as 'next', which would be confusing on a dashboard."""
    from swissweather_fusion.sensor import OfficialWarningSensor

    now_during = datetime(2026, 9, 8, 19, 0, tzinfo=timezone.utc)
    active = wa.parse_all_alarms(REAL_PAYLOAD, 145140, now=now_during)
    upcoming = wa.parse_all_alarms(
        REAL_PAYLOAD, 145140, now=now_during,
        lookahead=__import__("datetime").timedelta(hours=6),
    )
    sensor = object.__new__(OfficialWarningSensor)
    sensor._runtime = {"wetteralarm_coordinator": type("C", (), {
        "warning": active[0], "warnings": active,
        "upcoming_warnings": upcoming,
        "poi_id": 145140, "poi_distance_km": 0.5,
    })()}
    attrs = OfficialWarningSensor.extra_state_attributes.fget(sensor)
    assert "next_warning" not in attrs
    assert attrs["active"] is True


# ---------------------------------------------------------------------------
# v0.2.8 (SWF-028-001) — flat next_warning_* attributes
# ---------------------------------------------------------------------------
# Home Assistant's state_attr() returns a silent None for nested dict
# attributes in some core versions — a documented upstream bug
# (home-assistant/core#150292). A live installation confirmed it exactly:
# state_attr(eid, 'next_warning') returned None while Developer Tools
# showed the dict correctly and top-level scalar attributes (title,
# priority, region, valid_from, valid_to for an ACTIVE warning) rendered
# fine in the same template. The bug is specific to nesting.
def test_next_warning_scalars_are_top_level_attributes():
    """The fix: the same fields the active-warning case already exposes
    as top-level scalars are now available for the upcoming case too,
    sidestepping the HA core bug entirely rather than working around it
    in every consuming template."""
    from swissweather_fusion.sensor import OfficialWarningSensor

    sensor = object.__new__(OfficialWarningSensor)
    sensor._runtime = {"wetteralarm_coordinator": type("C", (), {
        "warning": wa.WeatherWarning(), "warnings": [],
        "upcoming_warnings": wa.parse_all_alarms(
            REAL_PAYLOAD, 145140, now=SCREENSHOT_TIME,
            lookahead=__import__("datetime").timedelta(hours=6),
        ),
        "poi_id": 145140, "poi_distance_km": 0.5,
    })()}
    attrs = OfficialWarningSensor.extra_state_attributes.fget(sensor)

    assert attrs["next_warning_title"] == "Gewittergefahr"
    assert attrs["next_warning_level"] == "yellow"
    assert attrs["next_warning_priority"] == 1
    assert attrs["next_warning_region"] == "Frauenfeld"
    assert attrs["next_warning_valid_from"] == "2026-09-08T18:00:00+00:00"
    assert attrs["next_warning_valid_to"] == "2026-09-08T23:00:00+00:00"


def test_nested_next_warning_dict_is_preserved_alongside_the_flat_fields():
    """The nested dict is kept, not replaced — dropping it would lose
    information for any consumer reading attributes directly in Python
    (scripts, pyscript), where the HA core bug does not apply."""
    from swissweather_fusion.sensor import OfficialWarningSensor

    sensor = object.__new__(OfficialWarningSensor)
    sensor._runtime = {"wetteralarm_coordinator": type("C", (), {
        "warning": wa.WeatherWarning(), "warnings": [],
        "upcoming_warnings": wa.parse_all_alarms(
            REAL_PAYLOAD, 145140, now=SCREENSHOT_TIME,
            lookahead=__import__("datetime").timedelta(hours=6),
        ),
        "poi_id": 145140, "poi_distance_km": 0.5,
    })()}
    attrs = OfficialWarningSensor.extra_state_attributes.fget(sensor)

    assert attrs["next_warning"]["title"] == "Gewittergefahr"
    assert attrs["next_warning_title"] == attrs["next_warning"]["title"]


def test_has_next_warning_boolean_is_a_scalar_shortcut():
    """A plain boolean is never affected by the nested-dict bug, so a
    template can check `has_next_warning` without touching next_warning
    or next_warning_title at all."""
    from swissweather_fusion.sensor import OfficialWarningSensor

    with_upcoming = object.__new__(OfficialWarningSensor)
    with_upcoming._runtime = {"wetteralarm_coordinator": type("C", (), {
        "warning": wa.WeatherWarning(), "warnings": [],
        "upcoming_warnings": wa.parse_all_alarms(
            REAL_PAYLOAD, 145140, now=SCREENSHOT_TIME,
            lookahead=__import__("datetime").timedelta(hours=6),
        ),
        "poi_id": 145140, "poi_distance_km": 0.5,
    })()}
    assert OfficialWarningSensor.extra_state_attributes.fget(with_upcoming)[
        "has_next_warning"
    ] is True

    without = object.__new__(OfficialWarningSensor)
    without._runtime = {"wetteralarm_coordinator": type("C", (), {
        "warning": wa.WeatherWarning(), "warnings": [], "upcoming_warnings": [],
        "poi_id": 145140, "poi_distance_km": 0.5,
    })()}
    assert OfficialWarningSensor.extra_state_attributes.fget(without)[
        "has_next_warning"
    ] is False


def test_no_flat_next_warning_keys_when_nothing_upcoming():
    """The flat keys must not appear at all (not even as None) when
    there is nothing upcoming — consistent with next_warning's own
    absence in that case."""
    from swissweather_fusion.sensor import OfficialWarningSensor

    sensor = object.__new__(OfficialWarningSensor)
    sensor._runtime = {"wetteralarm_coordinator": type("C", (), {
        "warning": wa.WeatherWarning(), "warnings": [], "upcoming_warnings": [],
        "poi_id": 145140, "poi_distance_km": 0.5,
    })()}
    attrs = OfficialWarningSensor.extra_state_attributes.fget(sensor)
    assert "next_warning_title" not in attrs
    assert "next_warning" not in attrs
    assert attrs["has_next_warning"] is False
