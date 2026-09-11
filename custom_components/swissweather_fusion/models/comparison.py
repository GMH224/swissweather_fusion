"""The paired blend-vs-best-source report (v0.3.0, W0 / ARC-05).

Pure functions only — no I/O, no Home Assistant imports, no database
access, same contract as model_a.py. The coordinator reads rows from
storage/db.py and hands them here as plain dicts.

**What this module exists to fix.** Until v0.3.0 the project answered
"does fusion beat its inputs?" by comparing two sample-count-weighted
averages taken from bucket_stats: one over the blend pseudo-source's
rows, one over each provider's. Those two populations are not the same
population. Providers write a sample for every reconcilable hour of every
run across all three lead-time buckets; the blend wrote at six fixed lead
offsets, none beyond 48 hours, so it never entered the `long` bucket at
all. Comparing the means of two differently-distributed samples is not a
skill comparison, and because forecast error grows with lead time, the
difference ran in the blend's favour.

**The rule this module follows instead.** A source is only ever compared
with the blend on rows where BOTH produced a value, and the blend's MAE
is recomputed on each source's own subset rather than taken from a global
average. So `margin` for source S is:

    MAE(S over rows where S and the blend both spoke)
  - MAE(blend over exactly those same rows)

Positive means the blend was closer to the observation than S. This is
the only formulation in which "the blend beats CH1" is a statement about
the same forecasts rather than about two different ones.

**Why the strongest competitor, not the average competitor.** The verdict
compares the blend against whichever single source did best, because the
alternative to running this project is not "use the average source" — it
is "use the one source that turns out to be good here". If the blend
cannot beat that, the fusion is not earning its complexity, which is the
honest bar and the one DEVELOPER.md set.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from ..const import BLEND_COMPARISON_MIN_SAMPLES


@dataclass(frozen=True)
class SourceResult:
    """One source's paired result within one (measurement, lead) cell."""

    source: str
    sample_count: int
    source_mae: float
    blend_mae_on_subset: float
    # Positive = the blend was closer to the truth than this source, over
    # exactly the rows where both spoke.
    margin: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "samples": self.sample_count,
            "source_mae": round(self.source_mae, 3),
            "blend_mae": round(self.blend_mae_on_subset, 3),
            "margin": round(self.margin, 3),
        }


@dataclass(frozen=True)
class CellResult:
    """The verdict for one (measurement, lead offset) pair."""

    measurement: str
    lead_hours: int
    sample_count: int
    blend_mae: Optional[float]
    per_source: tuple[SourceResult, ...] = ()
    # The single source that came closest to (or beat) the blend.
    strongest_source: Optional[str] = None
    strongest_margin: Optional[float] = None
    blend_wins: Optional[bool] = None
    run_basis_rows: int = 0

    @property
    def has_verdict(self) -> bool:
        return self.blend_wins is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "measurement": self.measurement,
            "lead_hours": self.lead_hours,
            "samples": self.sample_count,
            "blend_mae": round(self.blend_mae, 3) if self.blend_mae is not None else None,
            "strongest_source": self.strongest_source,
            "margin_vs_strongest": (
                round(self.strongest_margin, 3)
                if self.strongest_margin is not None else None
            ),
            "blend_wins": self.blend_wins,
            "run_basis_rows": self.run_basis_rows,
            "per_source": [s.as_dict() for s in self.per_source],
        }


@dataclass(frozen=True)
class ComparisonReport:
    """Every cell, plus the aggregate the sensor shows as a headline."""

    cells: tuple[CellResult, ...] = ()
    total_pairs: int = 0
    first_valid_at: Optional[str] = None
    last_valid_at: Optional[str] = None
    cells_with_verdict: int = 0
    cells_blend_wins: int = 0
    # None until every cell that has a verdict agrees, or until at least
    # one cell has a verdict at all. Never False merely for lack of data —
    # see verdict() for why that distinction is the whole point.
    overall_blend_wins: Optional[bool] = None
    min_samples: int = BLEND_COMPARISON_MIN_SAMPLES

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_pairs": self.total_pairs,
            "window_start": self.first_valid_at,
            "window_end": self.last_valid_at,
            "min_samples_per_cell": self.min_samples,
            "cells_with_verdict": self.cells_with_verdict,
            "cells_blend_wins": self.cells_blend_wins,
            "overall_blend_wins": self.overall_blend_wins,
            "cells": [c.as_dict() for c in self.cells],
        }


def _mae(errors: list[float]) -> float:
    return sum(errors) / len(errors)


def _decode_contributors(raw: Any) -> dict[str, Any]:
    """Contributors JSON -> dict, tolerating anything unexpected.

    A malformed blob costs one row, not the whole report. Rows are
    written by this same codebase so malformed should be impossible —
    but "impossible" states that crash a report are how a measurement
    silently stops being taken.
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        decoded = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def build_report(
    rows: Iterable[Any],
    *,
    min_samples: int = BLEND_COMPARISON_MIN_SAMPLES,
) -> ComparisonReport:
    """Aggregate reconciled comparison rows into per-cell verdicts.

    `rows` are mappings (sqlite3.Row works) with at least: valid_at,
    measurement, lead_hours, blend_value, contributors, actual_value,
    lead_time_basis. Rows without an actual_value are ignored rather than
    treated as zero error.
    """
    # (measurement, lead) -> accumulators
    grouped: dict[tuple[str, int], dict[str, Any]] = {}
    total_pairs = 0
    first_valid: Optional[str] = None
    last_valid: Optional[str] = None

    for row in rows:
        actual = row["actual_value"]
        blend_value = row["blend_value"]
        if actual is None or blend_value is None:
            continue
        measurement = row["measurement"]
        try:
            lead = int(row["lead_hours"])
        except (TypeError, ValueError):
            continue

        valid_at = row["valid_at"]
        if first_valid is None or valid_at < first_valid:
            first_valid = valid_at
        if last_valid is None or valid_at > last_valid:
            last_valid = valid_at

        cell = grouped.setdefault(
            (measurement, lead),
            {
                "blend_errors": [],
                "per_source": {},
                "run_basis_rows": 0,
            },
        )
        blend_error = abs(blend_value - actual)
        cell["blend_errors"].append(blend_error)
        total_pairs += 1
        if row["lead_time_basis"] == "run":
            cell["run_basis_rows"] += 1

        for source, payload in _decode_contributors(row["contributors"]).items():
            if not isinstance(payload, dict):
                continue
            debiased = payload.get("debiased")
            if debiased is None:
                continue
            try:
                source_error = abs(float(debiased) - actual)
            except (TypeError, ValueError):
                continue
            bucket = cell["per_source"].setdefault(
                source, {"source_errors": [], "blend_errors": []}
            )
            # Both errors appended together, so the two lists are the
            # same rows in the same order by construction. This pairing
            # is the entire correctness argument of the module; it is
            # not something to reconstruct later from two separate
            # queries.
            bucket["source_errors"].append(source_error)
            bucket["blend_errors"].append(blend_error)

    cells: list[CellResult] = []
    for (measurement, lead), data in sorted(grouped.items()):
        blend_errors = data["blend_errors"]
        sample_count = len(blend_errors)
        blend_mae = _mae(blend_errors) if blend_errors else None

        source_results: list[SourceResult] = []
        for source, bucket in sorted(data["per_source"].items()):
            n = len(bucket["source_errors"])
            if n < min_samples:
                continue
            source_mae = _mae(bucket["source_errors"])
            blend_subset_mae = _mae(bucket["blend_errors"])
            source_results.append(
                SourceResult(
                    source=source,
                    sample_count=n,
                    source_mae=source_mae,
                    blend_mae_on_subset=blend_subset_mae,
                    margin=source_mae - blend_subset_mae,
                )
            )

        strongest: Optional[SourceResult] = None
        if source_results and sample_count >= min_samples:
            # Smallest margin = the source the blend beat by the least,
            # i.e. the strongest single-source alternative.
            strongest = min(source_results, key=lambda r: r.margin)

        cells.append(
            CellResult(
                measurement=measurement,
                lead_hours=lead,
                sample_count=sample_count,
                blend_mae=blend_mae,
                per_source=tuple(source_results),
                strongest_source=strongest.source if strongest else None,
                strongest_margin=strongest.margin if strongest else None,
                blend_wins=(strongest.margin > 0) if strongest else None,
                run_basis_rows=data["run_basis_rows"],
            )
        )

    decided = [c for c in cells if c.has_verdict]
    wins = [c for c in decided if c.blend_wins]

    return ComparisonReport(
        cells=tuple(cells),
        total_pairs=total_pairs,
        first_valid_at=first_valid,
        last_valid_at=last_valid,
        cells_with_verdict=len(decided),
        cells_blend_wins=len(wins),
        overall_blend_wins=verdict(decided),
        min_samples=min_samples,
    )


def verdict(decided_cells: list[CellResult]) -> Optional[bool]:
    """The headline answer, or None when there isn't one yet.

    **None is a real answer here and must not collapse to False.** The
    v0.1.28 defect (SWF-P1-007) was an accuracy sensor that showed
    nothing for four releases while looking implemented, because a
    failure and an empty result were indistinguishable to the reader. So
    this returns None when no cell has enough samples, and every caller
    reports sample counts alongside it — a None with `total_pairs: 0`
    means "not yet", a None with `total_pairs: 40000` means something is
    wrong, and the two are visibly different.

    With verdicts in hand, the blend wins only if it wins in the majority
    of decided cells. A blend that wins at 1 hour and loses at 48 is not
    a blend that works; it is a blend with a restricted range of
    validity, and the per-cell table is where that shows up.
    """
    if not decided_cells:
        return None
    wins = sum(1 for c in decided_cells if c.blend_wins)
    return wins * 2 > len(decided_cells)
