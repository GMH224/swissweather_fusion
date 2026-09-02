"""Unit conversion and barometric reduction for station sensor readings.

v0.1.24, addressing audit findings P1-21 (unit assumption) and P1-22 /
IND-12 (missing sea-level reduction).

**Why this is a separate module with no Home Assistant imports.** It
follows the same convention as models/model_a.py and models/model_b.py:
business logic that can be exercised directly by the test suite without
a running Home Assistant instance. Everything here is a pure function of
its arguments. StationCoordinator in coordinator.py is the only caller.

**The problem P1-21 describes.** StationCoordinator._read_float_state()
returned float(state.state) without ever inspecting the entity's
unit_of_measurement. A user pointing the integration at a Fahrenheit
temperature sensor or an inHg pressure sensor produced numerically
plausible but badly wrong values, which then flowed into Model A's EMA
and Model B's tendency features. Model A would faithfully learn the
resulting offset as "provider bias", permanently corrupting every bucket
for that measurement.

**The problem P1-22 describes, and why it needs a user decision.** Every
forecast provider this project consumes reports MEAN SEA LEVEL pressure:
Open-Meteo is queried for `pressure_msl`, meteoblue's response field is
`sealevelpressure`. A station barometer at 500 m altitude reads roughly
60 hPa lower than the sea-level equivalent. Blending the two without
reduction feeds Model A a constant, elevation-dependent offset.

This cannot be detected automatically. Netatmo — the reference station
for this project — publishes BOTH values: "Pressure", which Netatmo
normalizes to mean sea level using the altitude its app captures via GPS
during setup, and "AbsolutePressure", the raw measurement at the
station's own altitude. Home Assistant's Netatmo integration exposes
both, and both carry device_class: atmospheric_pressure, so the entity
selector cannot distinguish them and neither can any runtime heuristic
that does not already know the answer. Hence
CONF_STATION_PRESSURE_IS_SEA_LEVEL, asked explicitly during setup.
"""
from __future__ import annotations

import math
from typing import Optional

# Standard barometric formula constants. These are physical constants and
# the standard atmosphere reference, not tunable parameters.
_GRAVITY_M_S2 = 9.80665
_MOLAR_MASS_AIR_KG_MOL = 0.0289644
_UNIVERSAL_GAS_CONSTANT = 8.31446261815324

# Used when no station temperature is available at the moment a pressure
# reading is reduced. 15 °C is the ISA sea-level standard temperature.
# Defaulting is deliberate: refusing to compute would discard the pressure
# reading entirely, which is a worse outcome than reducing it with a
# reference temperature. The error introduced is small — at 500 m, a 10 °C
# temperature error shifts the reduction by well under 1 hPa.
DEFAULT_REFERENCE_TEMPERATURE_C = 15.0

# Recognised unit strings, lower-cased and stripped before lookup. Home
# Assistant's own constants are the common case; the alternate spellings
# cover integrations that set the attribute by hand.
_TEMPERATURE_UNITS_CELSIUS = {"°c", "c", "celsius"}
_TEMPERATURE_UNITS_FAHRENHEIT = {"°f", "f", "fahrenheit"}
_TEMPERATURE_UNITS_KELVIN = {"k", "kelvin"}

_PRESSURE_TO_HPA_FACTOR = {
    "hpa": 1.0,
    "mbar": 1.0,
    "mb": 1.0,
    "millibar": 1.0,
    "pa": 0.01,
    "kpa": 10.0,
    "inhg": 33.863886666667,
    "mmhg": 1.3332236842105,
    "psi": 68.94757293168,
    "bar": 1000.0,
}


def convert_temperature_to_celsius(
    value: Optional[float], unit: Optional[str]
) -> Optional[float]:
    """Convert a temperature reading to Celsius.

    Three distinct cases, deliberately distinguished:

    - `unit` is None or empty: treated as "already Celsius". This
      preserves prior behaviour for entities that simply do not populate
      unit_of_measurement, of which there are many. Changing this to a
      rejection would break working installations on upgrade.
    - `unit` is recognised: converted.
    - `unit` is present but NOT recognised: returns None. Explicit
      rejection, not a silent guess — an unrecognised unit means we do
      not know what the number means, and a wrong number is worse than no
      number for a value that feeds a learning loop.
    """
    if value is None or not math.isfinite(value):
        return None
    if unit is None or not str(unit).strip():
        return value

    normalized = str(unit).strip().lower()
    if normalized in _TEMPERATURE_UNITS_CELSIUS:
        return value
    if normalized in _TEMPERATURE_UNITS_FAHRENHEIT:
        return (value - 32.0) * 5.0 / 9.0
    if normalized in _TEMPERATURE_UNITS_KELVIN:
        return value - 273.15
    return None


def convert_pressure_to_hpa(
    value: Optional[float], unit: Optional[str]
) -> Optional[float]:
    """Convert a pressure reading to hectopascals.

    Same missing-unit and unrecognised-unit semantics as
    convert_temperature_to_celsius above.
    """
    if value is None or not math.isfinite(value):
        return None
    if unit is None or not str(unit).strip():
        return value

    normalized = str(unit).strip().lower().replace(" ", "")
    factor = _PRESSURE_TO_HPA_FACTOR.get(normalized)
    if factor is None:
        return None
    return value * factor


def convert_humidity_to_percent(
    value: Optional[float], unit: Optional[str]
) -> Optional[float]:
    """Normalise a relative humidity reading to percent.

    Included for symmetry and because the caller passes a
    measurement_kind for all three station entities. Relative humidity is
    effectively always reported in percent, so this is close to a
    pass-through; the only real work is rejecting a non-finite value and
    an unexpected unit.
    """
    if value is None or not math.isfinite(value):
        return None
    if unit is None or not str(unit).strip():
        return value

    normalized = str(unit).strip().lower()
    if normalized in {"%", "percent", "pct"}:
        return value
    return None


def reduce_station_pressure_to_sea_level(
    station_pressure_hpa: Optional[float],
    elevation_m: Optional[float],
    temperature_c: Optional[float] = None,
) -> Optional[float]:
    """Reduce a station-level pressure reading to mean sea level.

    Uses the standard barometric formula:

        P0 = P * exp((g * M * h) / (R * T))

    where T is absolute temperature in Kelvin and h is elevation in
    metres. At 500 m and 15 °C this raises the reading by roughly 60 hPa,
    which is the whole point — see the module docstring for why the
    provider side is already MSL.

    Returns the input unchanged when elevation is zero or missing (there
    is nothing to reduce), and None when the pressure itself is missing
    or non-finite.
    """
    if station_pressure_hpa is None or not math.isfinite(station_pressure_hpa):
        return None
    if elevation_m is None or not math.isfinite(elevation_m) or elevation_m == 0:
        return station_pressure_hpa

    if temperature_c is None or not math.isfinite(temperature_c):
        temperature_c = DEFAULT_REFERENCE_TEMPERATURE_C
    temperature_k = temperature_c + 273.15
    if temperature_k <= 0:
        # Physically impossible input; fall back to the reference rather
        # than producing a division blow-up or a nonsense exponent.
        temperature_k = DEFAULT_REFERENCE_TEMPERATURE_C + 273.15

    exponent = (_GRAVITY_M_S2 * _MOLAR_MASS_AIR_KG_MOL * elevation_m) / (
        _UNIVERSAL_GAS_CONSTANT * temperature_k
    )
    return station_pressure_hpa * math.exp(exponent)
