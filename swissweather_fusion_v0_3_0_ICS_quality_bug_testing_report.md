# SwissWeather Fusion v0.3.0 — ICS Quality Bug & Testing Report

**Release:** v0.3.0 (W0 — measurement repair)
**Date:** 11 September 2026
**Suite:** 734 tests passing (691 before), 45 new, pyflakes clean
**Standard applied:** *if I break the fix, does this test fail?*

---

## 1. Scope of testing

v0.3.0 changes one behaviour the user can see — the removal of a
misleading sensor attribute — and adds one capability: a paired
measurement. Everything else is required to be identical to v0.2.8.

That gives an unusually sharp exit criterion, and it is the one used
throughout: **the 691 pre-existing tests must still pass unchanged**,
except where a test asserted behaviour this release deliberately
corrects. Four did. Each was rewritten rather than deleted, and §4
records why.

---

## 2. Defects found during implementation

### SWF-030-001 (High) — bounds validation would have stopped silently

`provider_validation.validate_forecast_rows` opened with:

```python
if len(row) != 6:
    validated.append(row)   # passed through, unvalidated
    continue
```

The Open-Meteo coordinator now appends two columns. The moment it did,
physical-bounds validation would have stopped running for ch1, ch2 and
icon_d2 — three of five sources, and the majority of all stored forecast
values — with no error, no log line, and every existing test still green.

**Why it matters beyond itself.** This is the exact defect class the
release exists to make measurable: a check that is implemented,
unit-tested, and silently not reached. It appeared *inside* the fix for
that class. The lesson recorded in the code is that a shape test written
as an exact equality is a tripwire for the next person to widen the
shape.

**Fix:** validate by field position, not tuple length. `value` is field 4
in both shapes.

**Tests:** `test_physical_bounds_validation_still_applies_to_the_wider_row`
and `..._to_the_narrow_row`. Both assert the rejection count, the nulled
value, the preserved row width, and the preserved trailing columns —
so a fix that validated correctly but mangled the new columns would also
fail.

**Broken-fix check:** reverting to `len(row) != 6` fails the wider-row
test. Confirmed.

### SWF-030-002 (High) — migration dispatch skipped the v3 rebuild

To avoid running v3's destructive rebuild on databases that did not need
it, the dispatch tested one column:

```python
if "reconciliation_status" not in actual.get("forecast_snapshots", set()):
    self._migrate_to_v3()
```

A **v2** database has that column and still lacks
`storm_predictions.reconciled`. The rebuild was skipped, and
`_INDEX_SQL` then failed with `no such column: reconciled` — the exact
v0.1.24 setup outage that the table → migrate → index ordering exists to
prevent.

**Caught by the existing suite**, not by inspection:
`test_v0_1_26_construction.py` failed with eight errors on the first run
after the change. That suite was written for a different release and paid
for itself here.

**Why the fix is structural.** This is the second time a hand-picked
sentinel column has caused this (v0.2.2, SWF-021-008 was the first).
A rule that must be remembered when writing a new migration is evidently
not enough, so v4's requirement set is now *derived* from v3's:

```python
_V4_REQUIRED_COLUMNS = {
    **_V3_REQUIRED_COLUMNS,
    "forecast_snapshots": (
        _V3_REQUIRED_COLUMNS["forecast_snapshots"]
        | {"run_initialised_at", "lead_time_basis"}
    ),
}
```

A future v5 cannot drop a v3 requirement while copying the block.

**Tests:** `test_a_v2_database_still_gets_the_v3_rebuild_before_v4`
constructs a real v2 database and asserts it opens.
`test_v4_requirements_are_derived_from_v3_not_restated` asserts the
subset relation directly, so the derivation cannot be flattened back into
a literal without failing.

### SWF-030-003 (Low) — a guard no test could see

Found by mutation testing, not by review. Deleting the per-source
minimum-sample guard from `build_report` left the **entire suite green**,
because every fixture that had a thinly-sampled source also had a thinly
sampled cell, and the cell-level guard masked it.

Without the guard, a source that spoke three times and happened to be
perfect on all three would become the benchmark the whole blend is judged
against.

**Test:** `test_a_thinly_sampled_source_is_excluded_even_in_a_well_sampled_cell`
— 60 rows in the cell, meteoblue present on three of them.

---

## 3. Pre-existing defect recorded, not fixed

**IND-030-01 (Low, documentation).** `const.py` claimed the coordinator
consulted Open-Meteo's metadata API before fetching. No metadata call
existed anywhere in the codebase. The comment described the plan document
rather than the implementation.

No behavioural impact, but it is the documentation half of the
"implemented but never reached" class — an auditor reading it would have
concluded run-time awareness existed. Comment corrected.

---

## 4. Tests that asserted the old, wrong behaviour

Four tests failed after the ARC-05 fix. All four were *correct tests of
incorrect behaviour*, which is worth distinguishing from flaky tests.

| Test | Disposition |
| --- | --- |
| `test_accuracy_reports_whether_the_blend_beats_the_best_source` | Rewritten as `test_per_source_and_blend_mae_are_still_reported`, asserting the per-source figures survive and the verdict key is **absent** |
| `test_accuracy_reports_honestly_when_the_blend_loses` | Removed; its intent is served by the ARC-05 regression test, which constructs a genuine loss |
| `test_freshness_is_applied_to_learned_weights_only` | Retargeted at `_contributions_at`, where the guarded loop now lives. Invariant unchanged |
| `test_reset_on_a_fresh_database_is_a_harmless_noop` | Extended for the new `comparisons_cleared` key |

The first of these carries a docstring explaining, at the site of the old
assertion, why it passed and was wrong anyway. A changelog entry would
not be read by the next person to touch that file.

---

## 5. New test coverage

45 new tests in `tests/test_v0_3_0_w0.py`.

**The headline regression.**
`test_the_old_unpaired_comparison_can_report_a_win_when_the_blend_actually_loses`
transcribes v0.2.8's comparison into the test file — a regression test
for deleted logic must carry its own copy of that logic or it asserts
nothing — then shows the two methods reaching opposite conclusions from
identical underlying forecasts. It asserts `legacy is True` explicitly,
so if the premise ever stops holding the test says so rather than
quietly passing.

**Reachability (§7.1 class).** Every metadata model is a real source;
every Open-Meteo model has a metadata mapping; every forecast source has
an update cadence; every comparison lead offset is positive and ordered.

**Migration.** Complete v3 and complete v2 fixtures, built at their real
shapes rather than as convenient subsets — the v0.2.2 finding was a
partial fixture cited as coverage for a migration that then failed on the
columns it omitted. Asserted: raw facts survive, Model B history
survives, learned state is cleared, the upgraded database can actually
store the new columns, and a second open is a no-op.

**Degradation.** A metadata fetch against an exploding session returns
`None` rather than raising; a source with no metadata endpoint makes no
request at all; a recording failure does not break the forecast cycle; a
malformed contributors blob costs one row, not the report.

**Anti-patterns avoided.** No assertion is satisfiable by a crash — every
`None` assertion is paired with a sample-count assertion. No wall-clock
thresholds.

---

## 6. Mutation testing

Applied to `models/comparison.py`, the analytical core. Five mutations,
each a plausible implementation mistake rather than arbitrary noise:

| Mutation | Tests failed | Caught |
| --- | --- | --- |
| `margin` sign flipped | 3 | Yes |
| Benchmark = weakest source instead of strongest | 3 | Yes |
| Per-source pairing replaced by global blend MAE | 1 | Yes |
| `None` verdict collapsed to `False` | 2 | Yes |
| Per-source sample floor removed | **0** | **No** → SWF-030-003 |

The fifth is the reason this section exists. Four of five caught looks
like good coverage; the one that escaped was a real gap that review had
not found, and it was the cheapest of the five to fix.

Re-run after fixing SWF-030-003: all five caught.

---

## 7. What was not tested

Stated plainly, because a test report that only lists what was verified
is half a report.

- **No live Home Assistant run.** Every result is from the suite and from
  live provider responses. The migration is exercised against fixtures;
  a fixture is not an installation.
- **No multi-month data.** The report logic is tested against synthetic
  rows. Nothing confirms it behaves sensibly against three months of real
  accumulation — that is what the evaluation period is for.
- **`run_initialised_at` is stored but not consumed**, so nothing tests
  run-relative bucketing. Deferred with the feature.
- **No concurrency test on `blend_comparison`.** The existing
  `test_db_concurrency.py` covers the shared lock the new methods use,
  but no test specifically races a comparison write against a
  reconciliation batch.
- **Class B and C parameters are not covered**, because they are not
  recorded. The absence is asserted (`test_only_class_a_measurements_are_recorded`)
  so it stays deliberate rather than becoming an oversight.
