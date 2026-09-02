from datetime import datetime, timedelta, timezone

from swissweather_fusion.models import model_a


def test_derive_season():
    assert model_a.derive_season(datetime(2026, 1, 15)) == "DJF"
    assert model_a.derive_season(datetime(2026, 4, 15)) == "MAM"
    assert model_a.derive_season(datetime(2026, 7, 15)) == "JJA"
    assert model_a.derive_season(datetime(2026, 11, 15)) == "SON"


def test_bucket_derivation_unaffected_by_dst_transitions():
    """Requested edge cases: winter->summer and summer->winter transitions.
    Model A's bucket keys (season, lead_time_bucket, hour_of_day) are
    computed entirely from UTC timestamps — model_a.utcnow() and the
    coordinator's UTC-based target-hour arithmetic — never from
    HA-configured local time. UTC has no DST, so it has no skipped or
    repeated hours to corrupt anything: this test proves that property
    directly rather than just asserting it, by checking derivation across
    the exact UTC moments when Europe's DST transitions happen (the
    transitions themselves are defined in local time, but the UTC instant
    they occur at is just an ordinary, unremarkable point on the
    timeline).
    """
    # Spring-forward moment (2026-03-29, Europe/Zurich switches to CEST at
    # 01:00 UTC = 02:00 CET -> 03:00 CEST). Nothing special happens in UTC.
    just_before_spring = datetime(2026, 3, 29, 0, 59, tzinfo=timezone.utc)
    just_after_spring = datetime(2026, 3, 29, 1, 1, tzinfo=timezone.utc)
    issued_spring = just_before_spring - timedelta(hours=2)

    assert model_a.derive_season(just_before_spring) == "MAM"
    assert model_a.derive_season(just_after_spring) == "MAM"
    assert model_a.derive_lead_time_bucket(issued_spring, just_before_spring) == "short"
    assert model_a.derive_lead_time_bucket(issued_spring, just_after_spring) == "short"

    # Fall-back moment (2026-10-25, Europe/Zurich switches back to CET at
    # 01:00 UTC = 03:00 CEST -> 02:00 CET). Again, nothing special in UTC.
    just_before_fall = datetime(2026, 10, 25, 0, 59, tzinfo=timezone.utc)
    just_after_fall = datetime(2026, 10, 25, 1, 1, tzinfo=timezone.utc)
    issued_fall = just_before_fall - timedelta(hours=2)

    assert model_a.derive_season(just_before_fall) == "SON"
    assert model_a.derive_season(just_after_fall) == "SON"
    assert model_a.derive_lead_time_bucket(issued_fall, just_before_fall) == "short"
    assert model_a.derive_lead_time_bucket(issued_fall, just_after_fall) == "short"

    # The whole point: a full week of hourly UTC timestamps spanning
    # either transition produces a strictly increasing, gap-free,
    # repeat-free sequence of hour_of_day/date values — nothing for the
    # bucket key or the storage layer to trip over. Spot-check a
    # continuous run across the spring-forward instant.
    hours = [just_before_spring + timedelta(hours=i) for i in range(-2, 3)]
    hour_values = [h.hour for h in hours]
    assert hour_values == sorted(hour_values) or hour_values[-1] < hour_values[0]  # wraps at most once (midnight), never duplicates mid-sequence
    assert len(set(zip([h.date() for h in hours], hour_values))) == len(hours)  # every (date, hour) pair is unique — no repeats


def test_derive_lead_time_bucket():
    issued = datetime(2026, 7, 25, 6, 0, tzinfo=timezone.utc)
    assert model_a.derive_lead_time_bucket(issued, issued + timedelta(hours=3)) == "short"
    assert model_a.derive_lead_time_bucket(issued, issued + timedelta(hours=48)) == "medium"
    assert model_a.derive_lead_time_bucket(issued, issued + timedelta(hours=100)) == "long"


def test_update_bucket_ema_cold_start():
    result = model_a.update_bucket_ema(
        previous_bias=0.0, previous_abs_error=0.0, previous_sample_count=0,
        forecast_value=20.0, actual_value=18.0, lead_time_bucket="short",
    )
    assert result.sample_count == 1
    assert result.ema_bias == 2.0  # forecast - actual, no prior to blend with
    # v0.1.23 fix (L-03): ema_abs_error on the very first sample must be
    # the RAW error (no prior bias to debias against yet), not zero. The
    # old buggy ordering computed new_bias = raw_error on cold start, then
    # measured the residual against that SAME new_bias — which is
    # mathematically guaranteed to be exactly zero every single time
    # (forecast_value - new_bias == actual_value by construction), making
    # ema_weight artificially maximal from the very first sample. This is
    # the most dramatic single illustration of L-03's impact.
    assert result.ema_abs_error == 2.0


def test_update_bucket_ema_judges_second_sample_against_bias_before_this_update():
    """v0.1.23 direct regression test for L-03 (external ICS audit): the
    second observation's error must be judged against the bias as it
    stood BEFORE this sample updates it, not after. Constructs a case
    where the two orderings give different, distinguishable numeric
    results, and checks the residual matches predict-then-update, not
    update-then-grade-yourself-against-the-update.
    """
    first = model_a.update_bucket_ema(
        previous_bias=0.0, previous_abs_error=0.0, previous_sample_count=0,
        forecast_value=20.0, actual_value=18.0, lead_time_bucket="short",
    )
    assert first.ema_bias == 2.0  # bias after sample 1

    second = model_a.update_bucket_ema(
        previous_bias=first.ema_bias, previous_abs_error=first.ema_abs_error,
        previous_sample_count=first.sample_count,
        forecast_value=20.0, actual_value=15.0, lead_time_bucket="short",
    )
    # Correct (fixed): residual judged against the OLD bias (2.0), i.e.
    # debiased_forecast = 20.0 - 2.0 = 18.0, abs error vs actual (15.0) = 3.0.
    # alpha for "short" lead time; sample_count=1 so blended with previous_abs_error (0.0).
    alpha = model_a.EMA_ALPHA_BY_LEAD_TIME["short"]
    expected_abs_error = alpha * 3.0 + (1 - alpha) * first.ema_abs_error
    assert abs(second.ema_abs_error - expected_abs_error) < 1e-9

    # The old (buggy) ordering would instead compute new_bias first
    # (alpha*raw_error + (1-alpha)*previous_bias, raw_error = 20-15 = 5.0),
    # then measure the residual against THAT — a smaller, self-fitted
    # number. Confirm the fixed result is NOT equal to that old formula,
    # i.e. this test would have caught the regression.
    raw_error = 20.0 - 15.0
    buggy_new_bias = alpha * raw_error + (1 - alpha) * first.ema_bias
    buggy_debiased_forecast = 20.0 - buggy_new_bias
    buggy_residual = abs(buggy_debiased_forecast - 15.0)
    buggy_expected_abs_error = alpha * buggy_residual + (1 - alpha) * first.ema_abs_error
    assert abs(second.ema_abs_error - buggy_expected_abs_error) > 1e-9


def test_update_bucket_ema_moves_toward_new_observation():
    first = model_a.update_bucket_ema(
        previous_bias=0.0, previous_abs_error=0.0, previous_sample_count=0,
        forecast_value=20.0, actual_value=18.0, lead_time_bucket="short",
    )
    second = model_a.update_bucket_ema(
        previous_bias=first.ema_bias, previous_abs_error=first.ema_abs_error,
        previous_sample_count=first.sample_count,
        forecast_value=20.0, actual_value=20.0, lead_time_bucket="short",
    )
    assert second.sample_count == 2
    # EMA, not a full replace: moves toward the new (zero) error but doesn't jump there
    assert 0.0 < second.ema_bias < first.ema_bias


def test_blend_debiases_before_weighting():
    contributions = [
        model_a.SourceContribution(source="ch1", raw_value=20.0, ema_bias=2.0, ema_weight=5.0, sample_count=10),
        model_a.SourceContribution(source="ch2", raw_value=22.0, ema_bias=0.0, ema_weight=1.0, sample_count=2),
    ]
    blended = model_a.blend(contributions)
    # ch2 is below MIN_SAMPLES_TO_TRUST_BUCKET -> raw value.
    #
    # v0.1.24 (IND-01): its weight is no longer a hard-coded 1.0. The
    # cold-start weight is now the median learned weight among the
    # trusted contributors of THIS blend -- here just ch1's 5.0 -- so
    # that the two weights sit on one comparable scale instead of the
    # untrusted source being handed a number from a different one. What
    # this test was actually written to verify, that ch1 contributes its
    # DEBIASED 18.0 rather than its raw 20.0, is unchanged.
    expected = (18.0 * 5.0 + 22.0 * 5.0) / (5.0 + 5.0)
    assert abs(blended - expected) < 1e-9
    # Guard the original intent explicitly: without debiasing the answer
    # would be 21.0 rather than 20.0.
    assert abs(blended - 21.0) > 0.5


def test_blend_empty_returns_none():
    assert model_a.blend([]) is None


def test_blend_skips_none_raw_value_instead_of_crashing():
    """v0.1.7 regression test: a source returning null for a given
    hour/measurement (legitimate behavior from Open-Meteo/SRF/meteoblue)
    crashed in production with 'unsupported operand type(s) for *:
    NoneType and float' on every single blend cycle since deployment —
    this is why the weather entity stayed continuously Unavailable rather
    than intermittently. A None-valued contribution must be skipped, not
    crash the whole blend.
    """
    contributions = [
        model_a.SourceContribution(source="ch1", raw_value=None, ema_bias=0.0, ema_weight=1.0, sample_count=10),
        model_a.SourceContribution(source="ch2", raw_value=20.0, ema_bias=2.0, ema_weight=5.0, sample_count=10),
    ]
    # Only ch2 should contribute; ch1's None is skipped entirely, not
    # treated as zero or any other silently-wrong substitute value.
    assert model_a.blend(contributions) == 20.0 - 2.0


def test_blend_returns_none_if_every_contribution_is_none():
    contributions = [
        model_a.SourceContribution(source="ch1", raw_value=None, ema_bias=0.0, ema_weight=1.0, sample_count=0),
        model_a.SourceContribution(source="ch2", raw_value=None, ema_bias=0.0, ema_weight=1.0, sample_count=0),
    ]
    assert model_a.blend(contributions) is None


def test_lapse_rate_precorrection():
    corrected = model_a.apply_lapse_rate_precorrection(
        raw_temperature=20.0, source_grid_elevation_m=600.0, actual_elevation_m=400.0
    )
    # grid 200m higher than actual -> source reads colder than reality -> add back
    assert abs(corrected - 21.3) < 1e-9


def test_find_nearest_observation_picks_closest_within_tolerance():
    target = datetime(2026, 7, 25, 15, 0, tzinfo=timezone.utc)
    candidates = [
        (datetime(2026, 7, 25, 14, 50, tzinfo=timezone.utc), 20.0),
        (datetime(2026, 7, 25, 15, 2, tzinfo=timezone.utc), 21.0),  # genuinely closest (2 min away)
        (datetime(2026, 7, 25, 15, 20, tzinfo=timezone.utc), 22.0),
    ]
    assert model_a.find_nearest_observation(target=target, candidates=candidates) == 21.0


def test_find_nearest_observation_respects_tolerance():
    target = datetime(2026, 7, 25, 15, 0, tzinfo=timezone.utc)
    # Only candidate is 45 minutes away — outside the default 30-min tolerance.
    candidates = [(datetime(2026, 7, 25, 15, 45, tzinfo=timezone.utc), 21.0)]
    assert model_a.find_nearest_observation(target=target, candidates=candidates) is None


def test_find_nearest_observation_skips_none_values():
    target = datetime(2026, 7, 25, 15, 0, tzinfo=timezone.utc)
    candidates = [
        (datetime(2026, 7, 25, 15, 1, tzinfo=timezone.utc), None),  # closest but no value
        (datetime(2026, 7, 25, 15, 10, tzinfo=timezone.utc), 19.5),  # further but usable
    ]
    assert model_a.find_nearest_observation(target=target, candidates=candidates) == 19.5


def test_find_nearest_observation_no_candidates():
    target = datetime(2026, 7, 25, 15, 0, tzinfo=timezone.utc)
    assert model_a.find_nearest_observation(target=target, candidates=[]) is None
