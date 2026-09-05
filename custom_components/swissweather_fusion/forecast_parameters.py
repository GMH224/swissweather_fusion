"""Registry of forecast parameters and how each one is fused.

v0.2.0. Implements §17 of the Model A Expansion architecture, and
resolves finding AR-03 of the architecture review.

**Why a registry rather than branching logic.** Model A grows from 5
fused measurements to roughly 20. Each has different units, different
plausible bounds, a different fusion rule and a different relationship to
the learning loop. Expressed as `if variable == ...` chains that becomes
unreadable and, worse, becomes a place where a new parameter silently
inherits the wrong behaviour by falling through to a default.

**The parameter classes** come from the architecture document §6:

- **Class A** — learned. Reconcilable against a local station
  measurement, so the EMA bias/error machinery applies.
- **Class B** — fused but not learned. The provider gives a value, but
  this installation has no ground truth to check it against, so a
  learned "bias" would be fabricated. Blended into a consensus and
  presented as such.
- **Class C** — categorical. Never averaged arithmetically.
- **Class D** — provider metadata. Not a weather value; must not be
  blended as though it were.

**On fusion strategies (architecture review AR-03).** The architecture
document proposed a single "availability-aware arithmetic blend" for all
of Class B. That is right for continuous, well-behaved quantities and
wrong for the three parameters users look at most:

- **Precipitation is zero-inflated and heavy-tailed.** Averaging a model
  predicting 0 mm with one predicting 10 mm yields 5 mm — a value
  neither model forecast, describing weather neither expects. Across many
  hours the mean systematically under-forecasts peaks and over-forecasts
  drizzle.
- **Snowfall is near-binary at the margin.** It snows or it does not. A
  mean across disagreeing models invents a small non-zero snowfall that
  misrepresents both.
- **A gust forecast is an extreme statistic.** The mean of several
  maxima is not a maximum, and it understates the hazard in the one
  direction where being wrong matters most.

So each parameter declares its own strategy. The strategies themselves
are documented and defensible starting points, not calibrated choices —
see DEVELOPER.md.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Sequence


class ParameterClass(str, Enum):
    """See the module docstring."""

    LEARNED = "A"
    FUSED = "B"
    CATEGORICAL = "C"
    METADATA = "D"


# ---------------------------------------------------------------------------
# Fusion strategies
# ---------------------------------------------------------------------------
def fuse_mean(values: Sequence[float]) -> Optional[float]:
    """Arithmetic mean. For continuous, approximately-Gaussian quantities."""
    if not values:
        return None
    return sum(values) / len(values)


def fuse_median(values: Sequence[float]) -> Optional[float]:
    """Median — the strategy for precipitation-like quantities.

    Resists the zero-inflation problem: with sources at [0, 0, 8] the
    median is 0 (most models say dry) rather than 2.67 (a drizzle nobody
    forecast). With [6, 8, 10] it is 8, close to the mean, so it costs
    nothing when models agree.
    """
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def fuse_max(values: Sequence[float]) -> Optional[float]:
    """Maximum — for extremes such as wind gusts.

    A gust forecast is already the peak of a distribution. Averaging
    several peaks produces something that is not a peak and understates
    the hazard.
    """
    if not values:
        return None
    return max(values)


def fuse_min(values: Sequence[float]) -> Optional[float]:
    """Minimum — for conservative quantities such as visibility, where
    the worst case is the operationally relevant one."""
    if not values:
        return None
    return min(values)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ForecastParameter:
    """One fusable forecast parameter.

    `minimum_sources` exists because some parameters should not be
    published on the word of a single model. Defaults to 1 — a lone
    source is better than nothing for most things — but snowfall sets 2,
    since a single model predicting snow against several predicting rain
    is exactly the case where a confident answer is least warranted.
    """

    name: str
    parameter_class: ParameterClass
    unit: Optional[str]
    fuse: Callable[[Sequence[float]], Optional[float]]
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    minimum_sources: int = 1
    description: str = ""

    def validate(self, value: Optional[float]) -> Optional[float]:
        """Reject non-finite and out-of-bounds values.

        Deliberately duplicates the spirit of provider_validation.py
        rather than calling it: that module guards what reaches STORAGE,
        this one guards what reaches FUSION. A value can be storable
        (a provider genuinely said it) and still be unfit to blend.
        """
        if value is None:
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(numeric):
            return None
        if self.minimum is not None and numeric < self.minimum:
            return None
        if self.maximum is not None and numeric > self.maximum:
            return None
        return numeric

    def fuse_values(self, values: Sequence[Optional[float]]) -> Optional[float]:
        """Validate, then fuse, honouring minimum_sources."""
        clean = [v for v in (self.validate(x) for x in values) if v is not None]
        if len(clean) < self.minimum_sources:
            return None
        return self.fuse(clean)


def _p(name, cls, unit, fuse, lo=None, hi=None, min_sources=1, desc=""):
    return ForecastParameter(
        name=name, parameter_class=cls, unit=unit, fuse=fuse,
        minimum=lo, maximum=hi, minimum_sources=min_sources, description=desc,
    )


# The full registry. Class A entries are listed for completeness and to
# keep bounds in one place, but their fusion is performed by
# models/model_a.blend(), which applies the learned bias correction —
# `fuse` here is only the fallback used when no learned state exists.
PARAMETERS: dict[str, ForecastParameter] = {
    # -- Class A: learned against the local station ------------------------
    "temperature": _p("temperature", ParameterClass.LEARNED, "°C", fuse_mean,
                      -60, 60, desc="2 m air temperature"),
    "humidity": _p("humidity", ParameterClass.LEARNED, "%", fuse_mean,
                   0, 100, desc="relative humidity"),
    "pressure": _p("pressure", ParameterClass.LEARNED, "hPa", fuse_mean,
                   800, 1100, desc="mean sea level pressure"),

    # -- Class B: fused, not learned (no local ground truth) ---------------
    # Precipitation family: median, for the zero-inflation reason above.
    "precip": _p("precip", ParameterClass.FUSED, "mm", fuse_median,
                 0, 500, desc="total precipitation"),
    "rain": _p("rain", ParameterClass.FUSED, "mm", fuse_median,
               0, 500, desc="liquid precipitation component"),
    "showers": _p("showers", ParameterClass.FUSED, "mm", fuse_median,
                  0, 500, desc="convective shower component"),
    # Two sources required: a lone model forecasting snow against several
    # forecasting rain is precisely when confidence is least warranted.
    "snowfall": _p("snowfall", ParameterClass.FUSED, "cm", fuse_median,
                   0, 300, min_sources=2, desc="snowfall amount"),
    "snow_depth": _p("snow_depth", ParameterClass.FUSED, "m", fuse_median,
                     0, 20, desc="snow depth on ground"),
    "precip_probability": _p("precip_probability", ParameterClass.FUSED, "%",
                             fuse_mean, 0, 100,
                             desc="probability of precipitation; a genuine "
                                  "probability, so averaging is defensible"),
    # Wind: speed is continuous, gusts are an extreme.
    "wind_speed": _p("wind_speed", ParameterClass.FUSED, "m/s", fuse_mean,
                     0, 150, desc="10 m wind speed"),
    "wind_gust_speed": _p("wind_gust_speed", ParameterClass.FUSED, "m/s",
                          fuse_max, 0, 200,
                          desc="peak gust; fused with max, not mean"),
    # Bearing is an angle and must NOT be averaged linearly — the mean of
    # 350 deg and 10 deg is 180 deg, exactly backwards. Handled separately
    # by fuse_wind_bearing() below; registered here for bounds only.
    "wind_bearing": _p("wind_bearing", ParameterClass.FUSED, "°", fuse_median,
                       0, 360, desc="wind direction; see fuse_wind_bearing"),
    "dew_point": _p("dew_point", ParameterClass.FUSED, "°C", fuse_mean,
                    -60, 40, desc="dew point temperature"),
    "apparent_temperature": _p("apparent_temperature", ParameterClass.FUSED,
                               "°C", fuse_mean, -80, 60,
                               desc="feels-like temperature"),
    "cloud_coverage": _p("cloud_coverage", ParameterClass.FUSED, "%", fuse_mean,
                         0, 100, desc="total cloud cover"),
    "visibility": _p("visibility", ParameterClass.FUSED, "m", fuse_min,
                     0, 100_000,
                     desc="horizontal visibility; fused with min, since the "
                          "worst case is the operationally relevant one"),
    "uv_index": _p("uv_index", ParameterClass.FUSED, None, fuse_mean,
                   0, 20, desc="UV index"),
    "sunshine_duration": _p("sunshine_duration", ParameterClass.FUSED, "s",
                            fuse_mean, 0, 3600,
                            desc="sunshine seconds within the hour"),

    # -- v0.2.5: convective and vertical structure ------------------------
    # CAPE is the standard measure of how much energy is available to a
    # rising parcel — the closest thing to a thunderstorm predictor any
    # of these providers offers. Fused with MAX rather than mean: like a
    # wind gust it is a hazard indicator, and averaging away one model's
    # warning is the wrong direction to be wrong in.
    "cape": _p("cape", ParameterClass.FUSED, "J/kg", fuse_max, 0, 8000,
               desc="convective available potential energy"),
    # Convective inhibition is the lid on that energy. It is reported as
    # a NEGATIVE number (or zero), and more negative means a stronger
    # cap. Fused with max — i.e. the LEAST inhibited of the models —
    # because a weak cap is the pessimistic case, consistent with CAPE.
    "convective_inhibition": _p("convective_inhibition", ParameterClass.FUSED,
                                "J/kg", fuse_max, -5000, 0,
                                desc="convective inhibition; 0 = no cap"),
    # Height of the 0 degC isotherm. The honest rain-vs-snow
    # discriminator at a given altitude, replacing a surface-temperature
    # guess. Mean is right: it is a smooth continuous field.
    "freezing_level_height": _p("freezing_level_height", ParameterClass.FUSED,
                                "m", fuse_mean, 0, 9000,
                                desc="altitude of the 0 degC isotherm"),
    "snowfall_height": _p("snowfall_height", ParameterClass.FUSED, "m",
                          fuse_mean, 0, 9000,
                          desc="altitude above which precipitation falls as snow"),
    "cloud_base": _p("cloud_base", ParameterClass.FUSED, "m", fuse_mean,
                     0, 20000, desc="height of the cloud base"),
    # -- v0.2.5: provider self-reported confidence (Class D) --------------
    # meteoblue's own hourly forecast-confidence score, parsed since
    # v0.1.x into ParsedMeteoblueForecast.predictability and never
    # stored. A source telling us it is unsure about a particular hour is
    # information, and discarding it was the architecture document's
    # Class D gap.
    "predictability": _p("predictability", ParameterClass.FUSED, "%",
                         fuse_mean, 0, 100,
                         desc="provider-reported forecast confidence"),
}


def get(name: str) -> Optional[ForecastParameter]:
    return PARAMETERS.get(name)


def learned_parameters() -> tuple[str, ...]:
    """Class A names — the reconciliation set."""
    return tuple(
        n for n, p in PARAMETERS.items() if p.parameter_class is ParameterClass.LEARNED
    )


def fused_parameters() -> tuple[str, ...]:
    """Class B names — fused into a consensus, never learned."""
    return tuple(
        n for n, p in PARAMETERS.items() if p.parameter_class is ParameterClass.FUSED
    )


def fuse_wind_bearing(values: Sequence[Optional[float]]) -> Optional[float]:
    """Circular mean of wind directions, in degrees.

    Wind bearing cannot be averaged linearly. The arithmetic mean of 350°
    and 10° is 180° — due south when both sources say due north. The
    circular mean converts to unit vectors, averages those, and converts
    back, giving 0°.

    Returns None when the vector sum is degenerate (opposing directions
    that cancel), because in that case there genuinely is no meaningful
    average direction and inventing one would be worse than admitting it.
    """
    clean = []
    for value in values:
        if value is None:
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(numeric):
            clean.append(math.radians(numeric % 360))
    if not clean:
        return None

    sin_sum = sum(math.sin(a) for a in clean)
    cos_sum = sum(math.cos(a) for a in clean)
    # Degenerate: directions cancel, no meaningful mean exists.
    if abs(sin_sum) < 1e-9 and abs(cos_sum) < 1e-9:
        return None
    # Round before the modulo: atan2 returns ~-8.7e-16 for a true 0 deg
    # result, which would otherwise emerge as 359.99999999999994 rather
    # than 0. Six decimals is far finer than any provider reports.
    return round(math.degrees(math.atan2(sin_sum, cos_sum)), 6) % 360
