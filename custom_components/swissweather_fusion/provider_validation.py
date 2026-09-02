"""Provider-independent validation of forecast values before storage.

v0.1.24, addressing audit finding P1-23.

**Why this exists.** Each provider client performs structural validation
of its own response shape — Open-Meteo checks array lengths, meteoblue
checks for the expected keys, SRF checks its two documented response
shapes. None of them checks whether the NUMBERS are physically possible.
A provider returning 1e30 for a temperature, or NaN for a pressure,
produced a perfectly well-formed row that went straight into
forecast_snapshots and from there into Model A's EMA — where a single
absurd sample permanently distorts a bucket, because an EMA has no
mechanism for forgetting an outlier it has already absorbed.

**Why it is shared rather than per-client.** The three provider
coordinators all build the same row tuple shape before calling
insert_forecast_snapshots_bulk. Validating in one place means a new
provider gets the protection for free, and means the bounds are defined
once instead of drifting between clients.

**Why the bounds are generous.** These are a last line of defence
against corrupt data, not a plausibility model. A forecast of -45 °C is
implausible for Switzerland but is not corrupt, and rejecting it would
be the integration silently overruling a provider on meteorology. The
bounds below are set wide enough that a value outside them is almost
certainly a parsing error, a sentinel value, or a unit mistake — not a
weather event.

**Why a rejected value becomes None rather than dropping the row.**
Every provider already has a legitimate "no data for this hour" case,
which is represented as a row with value None. Reusing that
representation means the downstream consumers — the blend, the
reconciliation loop — need no new code path, and it preserves the
evidence that the provider did return something for that hour.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Optional

# (minimum, maximum) inclusive bounds per variable, in the canonical
# units this project stores: °C, %, hPa, mm, m/s.
PHYSICAL_BOUNDS: dict[str, tuple[float, float]] = {
    "temperature": (-60.0, 60.0),
    "humidity": (0.0, 100.0),
    "pressure": (800.0, 1100.0),
    "precipitation": (0.0, 500.0),
    "wind_speed": (0.0, 150.0),
}


def validate_forecast_value(variable: str, value: Optional[float]) -> Optional[float]:
    """Return the value if it is storable, otherwise None.

    A value is storable when it is None (already "no data"), or when it
    is a finite number within the variable's physical bounds. An unknown
    variable name passes through unbounded but still finite-checked —
    unknown here means "a variable added since these bounds were
    written", and silently rejecting it would be worse than storing it.
    """
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None

    bounds = PHYSICAL_BOUNDS.get(variable)
    if bounds is None:
        return numeric
    low, high = bounds
    if numeric < low or numeric > high:
        return None
    return numeric


def validate_forecast_rows(
    rows: Iterable[tuple[Any, ...]],
) -> tuple[list[tuple[Any, ...]], int]:
    """Validate a batch of forecast rows before bulk insert.

    Rows are the 6-tuples every provider coordinator already builds:
    ``(source, issued_at, valid_at, variable, value, trigger_reason)``.

    Returns ``(validated_rows, rejection_count)``. Row count, order and
    shape are all preserved exactly — only out-of-bounds values are
    replaced with None. The rejection count is returned rather than
    logged here so the caller can record it as a diagnostics event with
    its own provider context attached.
    """
    validated: list[tuple[Any, ...]] = []
    rejected = 0

    for row in rows:
        # Defensive: a row that is not the expected shape is passed
        # through untouched rather than being silently reshaped. Storage
        # will reject it loudly, which is the correct outcome — this
        # module's job is value sanity, not schema enforcement.
        if len(row) != 6:
            validated.append(row)
            continue

        source, issued_at, valid_at, variable, value, trigger_reason = row
        clean = validate_forecast_value(variable, value)
        if clean is None and value is not None:
            rejected += 1
        validated.append(
            (source, issued_at, valid_at, variable, clean, trigger_reason)
        )

    return validated, rejected
