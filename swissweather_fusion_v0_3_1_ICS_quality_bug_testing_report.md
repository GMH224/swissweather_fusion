# SwissWeather Fusion v0.3.1 — ICS Quality Bug & Testing Report

**Release:** v0.3.1 (external defect remediation)
**Date:** 11 September 2026
**Suite:** 781 tests passing (734 before), 47 new, pyflakes clean
**Standard applied:** *if I break the fix, does this test fail?*

---

## 1. Method

Every fix in this release was written against a confirmed finding, and
every finding was verified against source before a line was changed. The
triage document (`SwissWeather_Fusion_v0_3_0_External_Report_Triage.md`)
records the disposition of all 76 external findings, including the eight
declined and the reasons.

Three techniques were used, in this order:

1. **Source verification** — read the implementation, reproduce the
   reasoning, reject claims the code does not support. Eight findings
   were declined this way, two because the proposed remediation would
   have introduced a worse defect than it fixed.
2. **Regression tests confirmed failing first** — each fix was reverted
   after its test was written, to check the test actually bites.
3. **Mutation testing** — eleven plausible implementation mistakes
   injected into the fixed code. This is what earned its place; see §4.

---

## 2. Defects found during implementation

### SWF-031-001 (Medium) — a reachability test defeated by its own comment

The fix for SWF-ICS-028 added `wetteralarm_coordinator` to the setup
cleanup tuple, plus a reachability test asserting every constructed
coordinator appears there — because the original bug was a comment
claiming "EVERY already-constructed coordinator" that v0.2.6 silently
falsified.

Mutation testing removed the coordinator from the tuple again. **The test
stayed green.** The v0.3.1 comment explaining the finding sits inside the
same block and contains the string `wetteralarm_coordinator`, so the
substring check matched the explanation of the bug rather than the fix
for it.

Fixed by stripping comment lines before the membership check.

Worth stating plainly: the lesson of SWF-ICS-028 is that a comment
asserting completeness is not a mechanism, and the mechanism written to
replace it was defeated by a comment.

### SWF-031-002 (Medium) — three guards that nothing exercised

All three found by mutation, none by review:

**The origin check was never invoked by any test.** Deleting the call to
`_assert_trusted_asset_origin` from `async_fetch_latest_bytes` passed the
entire suite, because the origin tests called the function directly. The
function was well tested; the code path that is supposed to use it was
not tested at all.

**An `endswith()` host check passed.** The parametrised cases included
`data.geo.admin.ch.evil.com` (trusted name as a *prefix*) but not
`evil-data.geo.admin.ch` (trusted name as a *suffix*), which is the case
a suffix implementation accepts. Registering that host is not realistic;
the check should not depend on that remaining true.

**The final `isfinite` gate in `blend()` was untested.** Every fixture
was already stopped by one of the staged guards, so removing the final
gate changed nothing. Closed with a case where each individual term is
finite and their *accumulation* overflows — precisely what the staged
guards cannot see, because they inspect one contribution at a time.

### SWF-031-003 (Low) — an automated refactor edited its own target

Converting eleven writers to the `_transaction()` context manager was
scripted. The script matched `_transaction` itself and stripped its
`commit()`, leaving a context manager that rolled back correctly and
never committed.

Caught immediately by three existing migration tests. Recorded because
"the refactor matched the thing it was refactoring toward" is a shape
worth recognising next time.

---

## 3. Test fixtures that were asserting the defect

Three pre-existing fixtures failed against correct code. In each case the
fixture was wrong, and the failure was informative rather than an
obstacle.

| Fixture | What it was really asserting |
| --- | --- |
| `test_migration_from_v1_reopens_recent_rows_and_archives_old_ones` | One timestamp for both `issued_at` and `valid_at` — a zero-lead "forecast" for the hour it was issued in. That is an analysis, and exactly what SWF-ICS-051 removes. A fixture that cannot survive a correct data migration was describing something that should not have existed |
| `test_meteonomiqs_hourly_forecast_is_persisted_with_prefixed_variable_names` | A fixed past date, so the test asserted that a past-hour row gets persisted — the defect, not the feature |
| `_seed_providers` (pressure cross-check) | A hard-coded `issued_at` that now ages out of the vintage window. It would have silently stopped seeding, then passed or failed for reasons unrelated to what it tests |

All three were corrected at the fixture, with the reason recorded at the
site rather than only in this document.

---

## 4. Mutation testing

Eleven mutations, each a plausible implementation mistake.

| Mutation | Tests failed | Caught |
| --- | ---: | --- |
| SWF-ICS-051 guard removed | 3 | Yes |
| SWF-ICS-051 zero-lead allowed (`<=` → `<`) | 2 | Yes |
| SWF-ICS-050 stale metadata retained on failure | 1 | Yes |
| SWF-ICS-011 future radar allowed | 1 | Yes |
| SWF-ICS-045 rollback removed | 1 | Yes |
| SWF-ICS-014 Meteonomiqs bypasses barrier again | 4 | Yes |
| Non-finite learned statistic accepted | 1 | Yes |
| SWF-ICS-009 origin check **call** deleted | 0 | **No** |
| SWF-ICS-009 exact match → `endswith` | 0 | **No** |
| SWF-ICS-028 Wetter-Alarm removed from cleanup | 0 | **No** |
| SWF-ICS-076 final finite gate removed | 0 | **No** |

**Four of eleven escaped on the first pass.** All four are now caught.

The conclusion worth carrying forward: the value of this technique in
this release was not confirming that the fixes work. It was discovering
that four of them were **untested while the suite was green** — a 100%
pass rate across 781 tests, with four guards nothing verified.

That is the same failure shape this project keeps finding in its
production code (§7.1, "implemented, unit-tested, never reached"), now
observed in the test suite itself.

---

## 5. New test coverage

47 new tests in `tests/test_v0_3_1_ics_remediation.py`, grouped by what
they protect.

**Measurement integrity.** Past-hour and zero-lead rows are dropped;
unparseable timestamps are passed through rather than silently discarded;
the guard covers both row shapes; the v5 data migration clears
contaminated buckets while preserving forward history; it does not run
twice.

**The "will we reset again" tests.** Two were written specifically to
answer the maintainer's question rather than to close a finding, and one
of them found a live defect:

- `test_every_writer_of_learnable_rows_goes_through_the_barrier` — walks
  every call to `insert_forecast_snapshots_bulk` and asserts a validation
  call in the same enclosing function. **This found SWF-ICS-014**, a
  second past-hour path through `MeteonomiqsCoordinator` that the 051 fix
  had missed.
- `test_a_non_finite_learned_statistic_is_refused_at_the_boundary` —
  `bucket_stats` is the only table whose corruption cannot be repaired
  forward, so its guard is asserted at the table boundary rather than at
  any of the entrances.

**Radar integrity.** Future-dated readings rejected; small clock skew
still tolerated; genuinely stale readings still rejected; non-finite
calibration falls back to the ODIM default. The accumulation bound test
states explicitly that 400 mm/h is a *decode* check and not a
meteorological one, so nobody later tightens it and starts discarding
real cloudbursts.

**Attack surface.** Five parametrised hostile hrefs including both the
prefix and suffix cases; a separate test asserting the download path
actually calls the check; the streaming bound; diagnostic detail
truncation.

**Structural.** A reachability test over transaction ownership, so a
future method that commits without rollback fails the suite. A
reachability test over coordinator cleanup, with comments stripped.

---

## 6. Anti-patterns avoided

- No assertion is satisfiable by a crash — every `None` assertion is
  paired with a count assertion.
- No wall-clock thresholds.
- Every new fixture that claims to be provider data is a verbatim
  capture (§7.3).
- `test_negative_lead_would_still_bucket_as_short` is labelled in its own
  docstring as **documentation, not a guard** — it stays green if the
  barrier fix is removed, which is exactly why the barrier test exists
  separately. Recording that distinction at the site prevents the weaker
  test being cited as coverage for the stronger one.

---

## 7. What was not tested

- **No live Home Assistant run.** The migration is exercised against
  complete v2, v3 and v0.3.0-shaped fixtures. A fixture is not an
  installation.
- **No real malformed HDF5 product.** The external report reproduced
  those failures against synthetic files and its findings were accepted
  on that evidence rather than independently reproduced. The fixes are
  tested at the function level.
- **No concurrency test** racing a comparison write against a
  reconciliation batch, though both now share the transaction manager
  that `test_db_concurrency.py` already exercises.
- **Executor cancellation, `gather()` semantics under first-refresh
  failure, and setup cancellation** (SWF-ICS-025 / 029 / 062) cannot be
  qualified without a live runtime. Open gaps, not unfixed defects.
- **The provider-quota and Model-B persistence cluster** was deliberately
  not verified finding-by-finding. Each claim is of the family closed by
  the transaction-ownership work; they should be re-tested against
  v0.3.1 rather than against v0.3.0.
