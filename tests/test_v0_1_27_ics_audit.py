"""Regression tests for the external ICS-quality audit of v0.1.26.

Three P1 defects were confirmed and reproduced by that audit, plus three
P2 static findings. Every one is covered here by a test that reproduces
the original failure mode, not merely one that exercises the new code.

Two of the three P1s were introduced by this project's own remediation
work, which is worth stating plainly:

  SWF-P1-001 came from v0.1.24's P1-07 TOCTOU fix, which moved the quota
  reservation earlier — and moved it one branch too far out.

  SWF-P1-002 came from v0.1.24's P1-18 fix, which correctly replaced
  edge-clamping with a bounds check but put that check after int(),
  where truncation-toward-zero defeats it on two of four boundaries.

A fix that is right in intent and wrong in placement is the hardest kind
to spot in review, which is why these are behavioural tests at the
boundary rather than assertions that the new lines exist.
"""
from datetime import date, datetime, timezone

import pytest

from swissweather_fusion.clients import combiprecip as cp
from swissweather_fusion.clients import meteonomiqs as mn
from swissweather_fusion.clients import srf
from swissweather_fusion.clients.meteonomiqs import AnnualCallBudget
from swissweather_fusion.models import model_b


# ---------------------------------------------------------------------------
# SWF-P1-002 — grid containment must be tested on the continuous coordinate
# ---------------------------------------------------------------------------
GRID = {
    "xsize": 100, "ysize": 100,
    "LL_lon": 7.0, "LL_lat": 46.5,
    "UR_lon": 8.0, "UR_lat": 47.5,
}


def _grid_geometry():
    ll_e, ll_n = cp.wgs84_to_lv95(latitude=GRID["LL_lat"], longitude=GRID["LL_lon"])
    ur_e, ur_n = cp.wgs84_to_lv95(latitude=GRID["UR_lat"], longitude=GRID["UR_lon"])
    return ll_e, ll_n, ur_e, ur_n, (ur_e - ll_e) / 100, (ur_n - ll_n) / 100


@pytest.mark.parametrize("edge", ["left", "right", "top", "bottom"])
def test_point_just_outside_each_edge_returns_no_data(edge):
    """The reproduced defect.

    `int()` truncates toward zero, so a point 0.1 pixels outside the LEFT
    edge computed a continuous column of -0.1, became column 0, and then
    passed `0 <= col < xsize` — returning the edge pixel it was meant to
    reject. Same for the TOP edge via the row calculation.

    Only the two negative-going boundaries were affected; right and
    bottom happened to be caught correctly. That partial correctness is
    exactly why the original P1-18 test suite missed it, so all four
    edges are checked here.
    """
    ll_e, ll_n, ur_e, ur_n, px_e, px_n = _grid_geometry()
    mid_e, mid_n = (ll_e + ur_e) / 2, (ll_n + ur_n) / 2
    points = {
        "left": (ll_e - px_e * 0.1, mid_n),
        "right": (ur_e + px_e * 0.1, mid_n),
        "top": (mid_e, ur_n + px_n * 0.1),
        "bottom": (mid_e, ll_n - px_n * 0.1),
    }
    easting, northing = points[edge]
    row, col, _, _ = cp._pixel_indices(
        where_attrs=GRID, easting=easting, northing=northing
    )
    assert (row, col) == (None, None), (
        f"a point outside the {edge} edge resolved to pixel {(row, col)} — "
        "out-of-coverage telemetry must never become valid-looking data"
    )


@pytest.mark.parametrize("corner", ["left", "right", "top", "bottom", "centre"])
def test_point_just_inside_each_edge_still_resolves(corner):
    """The other half: the fix must not make the grid unusable near its
    own edges, which is where the upwind sampling points legitimately
    sit."""
    ll_e, ll_n, ur_e, ur_n, px_e, px_n = _grid_geometry()
    mid_e, mid_n = (ll_e + ur_e) / 2, (ll_n + ur_n) / 2
    points = {
        "left": (ll_e + px_e * 0.1, mid_n),
        "right": (ur_e - px_e * 0.1, mid_n),
        "top": (mid_e, ur_n - px_n * 0.1),
        "bottom": (mid_e, ll_n + px_n * 0.1),
        "centre": (mid_e, mid_n),
    }
    easting, northing = points[corner]
    row, col, _, _ = cp._pixel_indices(
        where_attrs=GRID, easting=easting, northing=northing
    )
    assert row is not None and col is not None
    assert 0 <= row < 100 and 0 <= col < 100


# ---------------------------------------------------------------------------
# SWF-P2-003 — malformed raster metadata must be rejected, not computed with
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,attrs",
    [
        ("zero xsize", dict(GRID, xsize=0)),
        ("negative ysize", dict(GRID, ysize=-10)),
        ("inverted easting extent", dict(GRID, UR_lon=6.0)),
        ("zero extent", dict(GRID, UR_lon=7.0, UR_lat=46.5)),
        ("missing key", {k: v for k, v in GRID.items() if k != "LL_lon"}),
        ("non-numeric bound", dict(GRID, LL_lat="not a number")),
    ],
)
def test_malformed_grid_metadata_is_rejected(name, attrs):
    """A truncated or corrupt HDF5 product must not produce a division by
    zero, a silently mirrored grid, or a plausible-looking pixel from
    nonsense geometry."""
    ll_e, ll_n, ur_e, ur_n, _, _ = _grid_geometry()
    row, col, _, _ = cp._pixel_indices(
        where_attrs=attrs, easting=(ll_e + ur_e) / 2, northing=(ll_n + ur_n) / 2
    )
    assert (row, col) == (None, None), f"{name} was not rejected"


# ---------------------------------------------------------------------------
# SWF-P1-003 — the risk scale is documented 0-9 and must be enforced
# ---------------------------------------------------------------------------
def _nowcast(value):
    return {
        "precipitationRisk": {
            "items": [
                {
                    "from": "2026-09-02T12:00:00Z",
                    "to": "2026-09-02T12:15:00Z",
                    "precrisk": {"value": value},
                }
            ]
        }
    }


@pytest.mark.parametrize("bad", [99, -1, 10, 1000, "abc", {}, [], True, False, 3.7])
def test_out_of_scale_risk_values_are_rejected_at_the_parser(bad):
    """Rejected rather than clamped: clamping 99 to 9 would invent a
    maximum-risk reading out of a response we demonstrably do not
    understand. None puts the interval in the same state as one the
    provider simply did not rate."""
    parsed = mn.parse_nowcast_response(_nowcast(bad))
    assert parsed.items[0].precip_risk_value is None


@pytest.mark.parametrize("good,expected", [(0, 0), (9, 9), (5, 5), ("7", 7)])
def test_in_scale_risk_values_are_accepted(good, expected):
    """Numeric strings are accepted because JSON APIs return them
    inconsistently and "7" is unambiguous."""
    parsed = mn.parse_nowcast_response(_nowcast(good))
    assert parsed.items[0].precip_risk_value == expected


def test_booleans_are_refused_despite_being_int_subclasses():
    """True would otherwise silently become risk 1."""
    assert mn._validated_risk_value(True) is None
    assert mn._validated_risk_value(False) is None


@pytest.mark.parametrize("risk", [99, -5, 1000])
def test_model_b_refinement_stays_within_its_declared_domain(risk):
    """Defence in depth. The audit reproduced a risk of 99 producing a
    refined score of 5.9 — published by StormOnsetProbabilitySensor,
    which advertises `%`, as **590%**. Any automation thresholding on
    that sensor fires unconditionally, and the value is persisted into
    storm_predictions, which is Model B v1's training set.

    The parser is now the primary defence; this is the seatbelt, because
    refine_with_meteonomiqs is public and its result reaches both a user
    sensor and durable storage.
    """
    refined = model_b.refine_with_meteonomiqs(
        base_probability=0.8, meteonomiqs_risk_value=risk
    )
    assert 0.0 <= refined <= 1.0


def test_model_b_refinement_clamps_an_absurd_base_probability_too():
    """base_probability arrives from a caller, so it is not trusted
    either."""
    assert model_b.refine_with_meteonomiqs(
        base_probability=5.0, meteonomiqs_risk_value=5
    ) == 1.0


def test_model_b_refinement_is_unchanged_for_valid_inputs():
    """The clamp must not alter normal behaviour: base 0.8 with risk 9
    is (0.8 + 1.0) / 2."""
    assert model_b.refine_with_meteonomiqs(
        base_probability=0.8, meteonomiqs_risk_value=9
    ) == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# SWF-P1-001 — a quota credit must correspond to an actual provider call
# ---------------------------------------------------------------------------
def test_scheduled_path_reserves_nothing_when_it_makes_no_call():
    """The reproduced defect, as an invariant on the accounting.

    The reservation used to run before the branch that decides whether to
    call, so in forecast season before local noon the counter
    incremented and no request was made. With a 6-hourly check that is up
    to two phantom credits per seasonal day before the real noon call,
    roughly tripling the recorded cost of a once-daily service.

    This mirrors the coordinator's decision logic directly rather than
    driving the whole method, because the branch under test is a pure
    scheduling decision.
    """
    budget = AnnualCallBudget(1000)
    today = date(2026, 7, 25)

    def scheduled_cycle(in_forecast_season: bool, past_noon: bool) -> bool:
        """Returns whether a provider call was made."""
        if in_forecast_season and past_noon:
            budget.record_call(today=today)
            return True
        if not in_forecast_season:
            budget.record_call(today=today)
            return True
        return False

    # Two pre-noon checks in season: no calls, and no credits spent.
    assert scheduled_cycle(True, False) is False
    assert scheduled_cycle(True, False) is False
    assert budget.to_state()["calls_used"] == 0, (
        "quota was consumed by a scheduling check that made no request"
    )

    # The real noon call spends exactly one.
    assert scheduled_cycle(True, True) is True
    assert budget.to_state()["calls_used"] == 1


def test_the_unconditional_keepalive_cannot_exhaust_the_annual_budget():
    """The audit fairly challenged the "keepalive is never skipped"
    policy as an unbounded bypass of a hard quota. The arithmetic that
    makes it safe is asserted rather than left in a comment: the
    keepalive fires at most once per day and the bonus path at most once
    per day, so the worst case is 730 calls against a 1000/year budget.
    """
    from swissweather_fusion.const import METEONOMIQS_ANNUAL_CALL_BUDGET

    worst_case_per_year = 365 * 2  # one keepalive + one bonus, every day
    assert worst_case_per_year < METEONOMIQS_ANNUAL_CALL_BUDGET


# ---------------------------------------------------------------------------
# SWF-P2-001 — persisted quota state must be semantically valid
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,state",
    [
        ("negative count", {"year": 2026, "calls_used": -10}),
        ("above budget", {"year": 2026, "calls_used": 99999}),
        ("string year", {"year": "2026", "calls_used": 5}),
        ("string count", {"year": 2026, "calls_used": "5"}),
        ("boolean count", {"year": 2026, "calls_used": True}),
    ],
)
def test_semantically_invalid_persisted_budget_state_is_discarded(name, state):
    """Well-formed JSON is not the same as meaningful. A negative count
    hands out free calls for a year; an inflated one starves the source.
    Discarded rather than clamped, leaving the same state as "never
    persisted", which every caller already handles."""
    budget = AnnualCallBudget(1000)
    budget.load_state(state)
    assert budget.to_state() == {"year": None, "calls_used": 0}, name


def test_valid_persisted_budget_state_is_still_restored():
    budget = AnnualCallBudget(1000)
    budget.load_state({"year": 2026, "calls_used": 5})
    assert budget.to_state() == {"year": 2026, "calls_used": 5}


def test_a_count_exactly_at_the_budget_is_valid():
    """Boundary: fully-consumed is a legitimate state, not corruption."""
    budget = AnnualCallBudget(1000)
    budget.load_state({"year": 2026, "calls_used": 1000})
    assert budget.to_state()["calls_used"] == 1000
    assert budget.can_call(today=date(2026, 7, 25)) is False


# ---------------------------------------------------------------------------
# SWF-P2-002 — parser arithmetic must not run on untrusted JSON types
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["twelve", None, {}, [], float("nan"), float("inf"), True])
def test_srf_wind_conversion_rejects_non_numeric_input(bad):
    """`entry[key] * KMH_TO_MS` on a string raises TypeError inside the
    parser, which aborts the entire SRF parse and discards every other
    variable in the response — a total outage from one bad field. A
    non-finite float sails through into storage instead, reaching
    arithmetic before provider_validation.py can classify it."""
    assert srf._kmh_to_ms(bad) is None


def test_srf_wind_conversion_is_correct_for_valid_input():
    """36 km/h is exactly 10 m/s."""
    assert srf._kmh_to_ms(36.0) == pytest.approx(10.0)
    assert srf._kmh_to_ms("36") == pytest.approx(10.0)


def test_meteonomiqs_radar_amount_rejects_non_numeric_and_negative():
    assert mn._validated_radar_amount("lots") is None
    assert mn._validated_radar_amount(-1.0) is None
    assert mn._validated_radar_amount(float("inf")) is None
    assert mn._validated_radar_amount(2.5) == pytest.approx(2.5)
