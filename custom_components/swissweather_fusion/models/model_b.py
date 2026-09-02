"""Model B: short-horizon convective/storm-onset classifier.

Scoped deliberately narrow (plan doc §4, §9, §10): this detects the SUMMER
convective cold-pool signature (a downdraft bringing sudden temperature
drop and rain, preceded by a sharp pressure "nose" and a humidity jump).
It is explicitly NOT tuned for winter frontal-passage/icing risk, which has
a different, more gradual pressure signature — see DEVELOPER.md for why
that's a deliberate scope boundary and not an oversight.

v0 here is a hand-crafted, graduated heuristic — not a binary rule, and not
yet a trained model. It combines two independent signal families:
  1. Station tendency (pressure/humidity/temperature rate-of-change) —
     the original v0 signal.
  2. CombiPrecip's 4-point upwind radar sampling (local + three
     progressively more distant upwind points) — added after the
     realization that "is there a precipitation cell approaching, and
     roughly how far away" is a much stronger direct signal
     than tendency alone, and costs nothing extra to compute once the
     radar grid is already downloaded for the local point (see
     DEVELOPER.md, "Upwind radar sampling").

v1 (a trained classifier, e.g. XGBoost/LightGBM) requires a real storm
season of logged storm_events + storm_predictions and is out of scope for
this file until that data exists — see DEVELOPER.md for the v0 -> v1
upgrade path. The richer feature set here (radar at multiple lead times,
station tendency, and an independent nowcast confirmation from
Meteonomiqs — CAPE was hoped for but turned out to require a paid
Meteonomiqs tier not available, see DEVELOPER.md)
is specifically what would make that future v1 model strong — tabular GBMs
are good at exactly this kind of multi-signal, engineered-feature problem.

Pure functions only, same rationale as model_a.py: no I/O, no HA imports,
directly unit-testable (see tests/test_model_b.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Sequence

from ..const import (
    LOCAL_POINT_PROBABILITY,
    RADAR_FRESHNESS_LIMIT,
    RADAR_PRECIP_ACCUM_MM_THRESHOLD,
    RADAR_QUALITY_MINIMUM_CODE,
    UPWIND_POINT_PROBABILITY,
    V0_HUMIDITY_RISE_PCT_THRESHOLD,
    V0_PRESSURE_DROP_HPA_THRESHOLD,
    V0_TRIGGER_PROBABILITY,
)


@dataclass(frozen=True)
class StationSample:
    """One row of station_observations, trimmed to what tendency math needs."""

    ts_epoch_seconds: float
    temperature: Optional[float]
    humidity: Optional[float]
    pressure: Optional[float]


@dataclass(frozen=True)
class RadarPointReading:
    """One CombiPrecip sampling point's reading.

    **v0.1.24 (P1-14)**: the value field was named ``precip_rate_mmh``
    and treated as an instantaneous rain rate. It is not. MeteoSwiss
    documents the CombiPrecip product this project fetches (CPC) as
    "Combiprecip 60-minute total", unit mm, aggregation "precipitation
    accumulation over 1 hour" — as distinct from RZC/PRECIP, which IS an
    instantaneous mm/h rate. Renamed to say what it holds.

    **v0.1.24 (P1-13)**: ``valid_at`` was captured from the HDF5 file's
    own scan-time metadata into RadarPixelValue and then dropped at the
    coordinator's construction site, leaving no way to tell a fresh
    reading from one Home Assistant had been re-serving for hours after
    the feed stalled. Now threaded through. None means "freshness
    unknown", which is treated as stale — see _radar_signal_probability.

    **v0.1.24 (P1-16)**: ``quality`` is MeteoSwiss's own radar quality
    code (0-9, 9 best), read from the CPC filename. None means the code
    could not be determined, which is NOT treated as bad — see
    _radar_signal_probability for why the two unknowns are handled
    asymmetrically.
    """

    label: str  # 'local' | 'near' | 'mid' | 'far'
    precip_accum_mm_1h: Optional[float]
    valid_at: Optional[datetime] = None
    quality: Optional[int] = None


@dataclass(frozen=True)
class TendencyFeatures:
    """Rate-of-change features over the configured windows (const.py), plus
    the current multi-point radar reading.

    None means "not enough history yet for this window" — the caller
    decides how to handle that (v0 simply treats a None tendency as 0,
    i.e. no signal, rather than raising).
    """

    delta_pressure_10min: Optional[float]
    delta_pressure_30min: Optional[float]
    delta_pressure_60min: Optional[float]
    delta_humidity_10min: Optional[float]
    delta_humidity_30min: Optional[float]
    delta_humidity_60min: Optional[float]
    delta_temperature_10min: Optional[float]
    delta_temperature_30min: Optional[float]
    delta_temperature_60min: Optional[float]
    radar_points: tuple[RadarPointReading, ...] = field(default_factory=tuple)


def _nearest_sample_at_or_before(
    samples: Sequence[StationSample], target_epoch: float
) -> Optional[StationSample]:
    """samples must be sorted ascending by ts_epoch_seconds."""
    candidate = None
    for s in samples:
        if s.ts_epoch_seconds <= target_epoch:
            candidate = s
        else:
            break
    return candidate


def _latest_with_value(
    samples: Sequence[StationSample], attr: str
) -> Optional[StationSample]:
    """Most recent sample that actually carries a value for `attr`.

    **v0.1.24 fix (IND-02)**. compute_tendency_features used to take
    ``latest = samples[-1]`` wholesale, and every delta returned None
    when that row's value was None. StationCoordinator wrote a row every
    5 minutes unconditionally, including when a sensor was `unavailable`
    or its value was rejected — so a single 5-minute dropout on any one
    of the three station sensors blanked ALL NINE tendency features and
    dropped score_v0 to 0.0, discarding the other 55 minutes of good
    data. That happened during exactly the conditions in which sensor
    dropouts are most likely, and presented as an unexplained score
    collapse rather than an outage, because the radar half of
    score_v0_graduated kept contributing normally.

    Resolving the endpoint per measurement instead of per row means one
    sensor going quiet no longer silences the other two.
    """
    for s in reversed(samples):
        if getattr(s, attr) is not None:
            return s
    return None


def _nearest_with_value_at_or_before(
    samples: Sequence[StationSample], target_epoch: float, attr: str
) -> Optional[StationSample]:
    """Window endpoint counterpart to _latest_with_value (IND-02).

    Same reasoning: an all-None row landing exactly at the window edge
    should not void the window when usable data sits just behind it.
    """
    candidate = None
    for s in samples:
        if s.ts_epoch_seconds <= target_epoch:
            if getattr(s, attr) is not None:
                candidate = s
        else:
            break
    return candidate


def compute_tendency_features(
    *,
    samples: Sequence[StationSample],
    now_epoch_seconds: float,
    radar_points: tuple[RadarPointReading, ...] = (),
) -> TendencyFeatures:
    """Compute Δpressure/Δhumidity/Δtemperature over 10/30/60 min windows,
    plus whatever multi-point radar reading was supplied.

    samples should be the station's recent history, sorted ascending by
    timestamp, covering at least the last 60 minutes for full features.
    """
    def delta(minutes: int, attr: str) -> Optional[float]:
        # v0.1.24 (IND-02): both endpoints are resolved per measurement,
        # so an all-None row at either end no longer voids the window.
        latest = _latest_with_value(samples, attr)
        if latest is None:
            return None
        past = _nearest_with_value_at_or_before(
            samples, now_epoch_seconds - minutes * 60, attr
        )
        if past is None:
            return None
        return getattr(latest, attr) - getattr(past, attr)

    return TendencyFeatures(
        delta_pressure_10min=delta(10, "pressure"),
        delta_pressure_30min=delta(30, "pressure"),
        delta_pressure_60min=delta(60, "pressure"),
        delta_humidity_10min=delta(10, "humidity"),
        delta_humidity_30min=delta(30, "humidity"),
        delta_humidity_60min=delta(60, "humidity"),
        delta_temperature_10min=delta(10, "temperature"),
        delta_temperature_30min=delta(30, "temperature"),
        delta_temperature_60min=delta(60, "temperature"),
        radar_points=radar_points,
    )


def score_v0(features: TendencyFeatures) -> float:
    """Original v0 threshold rule: classic "sharp pressure fall + humidity
    jump". Returns V0_TRIGGER_PROBABILITY if the rule fires, else 0.0.

    Kept standalone (rather than folded into score_v0_graduated) since it's
    still useful on its own as the simplest possible fallback if radar data
    is ever unavailable — see score_v0_graduated for the combined version
    actually used for the "storm in ~30 min" indicator.
    """
    pressure_drop = features.delta_pressure_30min
    humidity_rise = features.delta_humidity_30min

    if pressure_drop is None or humidity_rise is None:
        return 0.0

    pressure_signal = pressure_drop <= -V0_PRESSURE_DROP_HPA_THRESHOLD
    humidity_signal = humidity_rise >= V0_HUMIDITY_RISE_PCT_THRESHOLD

    if pressure_signal and humidity_signal:
        return V0_TRIGGER_PROBABILITY
    return 0.0


def _radar_point_is_usable(
    point: RadarPointReading, now: Optional[datetime]
) -> bool:
    """Whether a radar point may contribute to the score at all.

    Two independent gates, deliberately asymmetric in how they treat
    "unknown" — this asymmetry is the whole design and is not an
    oversight:

    **Freshness (P1-13), where unknown means EXCLUDE.** Home Assistant's
    DataUpdateCoordinator keeps serving its last successful .data
    indefinitely across repeated failed refreshes, so without this check
    a stalled CombiPrecip feed influences the storm score forever. A
    reading whose age cannot be established gives no evidence that it is
    current, and the cost of wrongly trusting a stale radar echo (a
    false storm warning, blinds closing on a clear day) is higher than
    the cost of ignoring one reading.

    **Quality (P1-16), where unknown means INCLUDE.** MeteoSwiss encodes
    a quality code in the CPC filename, but this project has never
    verified a real downloaded file, so it is entirely possible the code
    cannot be parsed in practice. Treating unknown quality as bad would
    then silently disable the entire radar signal — a much worse failure
    than occasionally scoring on a low-quality scan. Only a CONFIRMED
    low code excludes.
    """
    if now is not None:
        if point.valid_at is None:
            return False
        valid_at = point.valid_at
        if valid_at.tzinfo is None:
            valid_at = valid_at.replace(tzinfo=timezone.utc)
        if now - valid_at > RADAR_FRESHNESS_LIMIT:
            return False

    if point.quality is not None and point.quality < RADAR_QUALITY_MINIMUM_CODE:
        return False

    return True


def _radar_signal_probability(
    radar_points: tuple[RadarPointReading, ...],
    now: Optional[datetime] = None,
) -> float:
    """Highest probability implied by any usable point currently showing
    significant precipitation — "local" (already raining here) dominates;
    otherwise the nearest upwind point with a detection wins, since it
    implies the shortest distance and therefore the most imminent signal.

    `now` is injectable so the freshness gate is testable without
    patching the clock. Passing None disables the freshness gate
    entirely, which is what the pure-tendency unit tests want; production
    callers always pass a real timestamp.

    **v0.1.24 (P1-14)**: the threshold compares against millimetres
    accumulated over the preceding hour, not an instantaneous rate. See
    RADAR_PRECIP_ACCUM_MM_THRESHOLD in const.py.
    """
    usable = [p for p in radar_points if _radar_point_is_usable(p, now)]
    by_label = {p.label: p for p in usable}

    local = by_label.get("local")
    if local and local.precip_accum_mm_1h is not None:
        if local.precip_accum_mm_1h >= RADAR_PRECIP_ACCUM_MM_THRESHOLD:
            return LOCAL_POINT_PROBABILITY

    # Check nearest-to-farthest so the closest detection wins.
    for label in ("near", "mid", "far"):
        point = by_label.get(label)
        if point is None or point.precip_accum_mm_1h is None:
            continue
        if point.precip_accum_mm_1h >= RADAR_PRECIP_ACCUM_MM_THRESHOLD:
            return UPWIND_POINT_PROBABILITY[label]

    return 0.0


def score_v0_graduated(
    features: TendencyFeatures, now: Optional[datetime] = None
) -> float:
    """Combined v0 heuristic: the higher of the tendency-based signal and
    the radar-distance-based signal, not a strict sum — these are two
    independent ways of detecting the same underlying event, and a
    combined score should reflect "how confident are we from the strongest
    single piece of evidence", not double-count if both happen to agree.

    This is what should back a genuine "storm in the next ~30 minutes"
    percentage for use in automations (e.g. closing blinds) — still a
    hand-crafted rule, not yet a trained model, but meaningfully more
    graduated than the original binary v0 rule. See DEVELOPER.md for the
    reasoning and the v1 upgrade path once real training data exists.
    """
    tendency_score = score_v0(features)
    radar_score = _radar_signal_probability(features.radar_points, now)
    return max(tendency_score, radar_score)


def refine_with_meteonomiqs(
    *, base_probability: float, meteonomiqs_risk_value: Optional[int]
) -> float:
    """Optional refinement using Meteonomiqs's independent nowcast risk
    scale (0-9, see clients/meteonomiqs.py), when a call was actually
    spent on it (this source is budget-rationed — see const.py and
    DEVELOPER.md, "Why Meteonomiqs needs a daily heartbeat").

    Blends rather than overrides: an independent source agreeing raises
    confidence, disagreeing pulls it back toward the midpoint rather than
    fully overriding our own signals with a single external opinion.

    **On the 50/50 weighting (v0.1.24, P2-07)**: there is no statistical
    basis for weighting the two sources equally. It is not a tuned
    parameter and was never validated against outcomes; it reflects
    only that there is no MEASURED reason to trust either source more
    than the other, which is a different and much weaker claim than the
    arithmetic implies.

    Worth noting for the v1 upgrade path: Model A's EMA-learned
    per-source weights are the closest thing this project has to an
    actually-calibrated source-combination mechanism, and that mechanism
    does not currently extend to Model B at all. Extending it is the
    natural successor to this function — see DEVELOPER.md.

    **On the caller's use of the result (v0.1.24, P0-02)**: the refined
    value returned here is for display and history only. It must NOT be
    stored as the crossing-detection state variable, because crossing
    detection compares against the next cycle's UNREFINED base score;
    mixing the two scales produced a spurious "upward crossing" on
    essentially every cycle of any sustained signal. See
    ModelBCoordinator._async_update_data_inner.
    """
    if meteonomiqs_risk_value is None:
        return base_probability
    # Imported lazily to avoid a module-level cross-package import cycle
    # risk between models/ and clients/ — this is the single source of
    # truth for the scale, not a duplicated constant.
    from ..clients.meteonomiqs import RADAR_RISK_SCALE_MAX

    # v0.1.27 fix (SWF-P1-003): defence in depth. The parser now rejects
    # out-of-scale risk values (clients/meteonomiqs._validated_risk_value),
    # which is the primary fix — but this function is public, is called
    # from the coordinator, and produces a value that is persisted into
    # storm_predictions AND published as a percentage by
    # StormOnsetProbabilitySensor. A single validation layer between a
    # third-party payload and a user-visible "%" reading is not enough:
    # before this release, a risk of 99 yielded 5.9, shown as 590%, and
    # any automation thresholding on that sensor fired unconditionally.
    #
    # Clamping the NORMALISED value rather than rejecting outright,
    # because by this point we are past the boundary where discarding is
    # meaningful — the caller has already decided to refine. The parser
    # is where a bad value should die; this is the seatbelt.
    normalized = meteonomiqs_risk_value / RADAR_RISK_SCALE_MAX
    normalized = max(0.0, min(1.0, normalized))
    refined = (base_probability + normalized) / 2.0

    # The stated domain of this function's result is [0, 1]. Enforced,
    # not assumed, since base_probability arrives from a caller too.
    return max(0.0, min(1.0, refined))


@dataclass(frozen=True)
class TriggerDecision:
    should_trigger: bool
    reason: str


def evaluate_cross_model_trigger(
    *, previous_probability: float, current_probability: float, threshold: float
) -> TriggerDecision:
    """Fire once on the UPWARD crossing only, not continuously while elevated.

    This is what makes meteoblue's one-bonus-call-per-event allowance (plan
    doc §4, §10) actually mean "per event" rather than "once every scoring
    cycle for the whole duration a storm signal stays elevated" — without
    this edge-detection, a slow-moving system would trigger a poll storm of
    its own.
    """
    crossed_upward = previous_probability < threshold <= current_probability
    if crossed_upward:
        return TriggerDecision(should_trigger=True, reason="upward_crossing")
    return TriggerDecision(should_trigger=False, reason="no_crossing")
