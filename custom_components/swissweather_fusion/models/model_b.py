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
  2. CombiPrecip's 4-point upwind radar sampling (local + ~20/35/60min
     upwind) — added after the realization that "is there a precipitation
     cell approaching, and how far out" is a much stronger direct signal
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
from typing import Optional, Sequence

from ..const import (
    LOCAL_POINT_PROBABILITY,
    RADAR_PRECIP_DETECTION_MMH_THRESHOLD,
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
    """One CombiPrecip sampling point's current precipitation rate."""

    label: str  # 'local' | 'near' | 'mid' | 'far'
    precip_rate_mmh: Optional[float]


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
    latest = samples[-1] if samples else None

    def delta(minutes: int, attr: str) -> Optional[float]:
        if latest is None:
            return None
        past = _nearest_sample_at_or_before(samples, now_epoch_seconds - minutes * 60)
        if past is None:
            return None
        latest_val = getattr(latest, attr)
        past_val = getattr(past, attr)
        if latest_val is None or past_val is None:
            return None
        return latest_val - past_val

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


def _radar_signal_probability(radar_points: tuple[RadarPointReading, ...]) -> float:
    """Highest probability implied by any point currently showing
    significant precipitation — "local" (already raining here) dominates;
    otherwise the nearest upwind point with a detection wins, since it
    implies the shortest lead time.

    Point order in UPWIND_POINT_PROBABILITY matters here: "near" (~20min)
    should win over "mid" (~35min) if both show precipitation, since the
    near point represents the more imminent, more certain signal.
    """
    by_label = {p.label: p for p in radar_points}

    local = by_label.get("local")
    if local and local.precip_rate_mmh is not None:
        if local.precip_rate_mmh >= RADAR_PRECIP_DETECTION_MMH_THRESHOLD:
            return LOCAL_POINT_PROBABILITY

    # Check nearest-to-farthest so the closest (soonest) detection wins.
    for label in ("near", "mid", "far"):
        point = by_label.get(label)
        if point is None or point.precip_rate_mmh is None:
            continue
        if point.precip_rate_mmh >= RADAR_PRECIP_DETECTION_MMH_THRESHOLD:
            return UPWIND_POINT_PROBABILITY[label]

    return 0.0


def score_v0_graduated(features: TendencyFeatures) -> float:
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
    radar_score = _radar_signal_probability(features.radar_points)
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
    """
    if meteonomiqs_risk_value is None:
        return base_probability
    # Imported lazily to avoid a module-level cross-package import cycle
    # risk between models/ and clients/ — this is the single source of
    # truth for the scale, not a duplicated constant.
    from ..clients.meteonomiqs import RADAR_RISK_SCALE_MAX

    normalized = meteonomiqs_risk_value / RADAR_RISK_SCALE_MAX
    return (base_probability + normalized) / 2.0


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
