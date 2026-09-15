# SwissWeather Fusion v0.3.1 — Release Audit (ICS)

**Release:** v0.3.1 (remediation of the external ICS code defect report)
**Prior release:** v0.3.0 (deployed 10 September 2026, running)
**Schema:** v4 → v5 (**data migration, no column changes**)
**Date:** 11 September 2026
**Verdict:** **PASS with one accepted deferral and three verification gaps**
**Tests:** 781 passing (from 734), pyflakes clean across `custom_components/`

---

## 1. What this release is

An external adversarial audit filed 76 findings against v0.3.0. Triage
established that they collapse to roughly 47 distinct issues, of which 19
were confirmed against source and **three were defects introduced by
v0.3.0 itself**.

v0.3.1 fixes every confirmed finding that can produce a wrong output, a
corrupted measurement, or an unbounded allocation. It changes no
forecasting behaviour beyond what the corrections require.

The scope decision was the maintainer's, and it was to patch more rather
than less: security hardening and cheap structural items were pulled
forward rather than scheduled, on the reasoning that the evaluation
window must not be spent on a build known to be wrong.

---

## 2. The finding this release exists for

### SWF-ICS-051 (High) — past-hour rows were graded as short-lead forecasts

**Live since v0.1. Missed by three prior audits.** It sits in five lines
of a function everyone had read.

`parse_forecast_response` stored every element of the provider's hourly
array with no filter on `valid_at`. Open-Meteo's series begins at 00:00
UTC today, so a poll at 15:00 stored fifteen hours whose target had
already passed. `derive_lead_time_bucket` computed a negative lead and
returned `short`, because its first test is `lead_hours < 24` with no
lower bound.

Those rows reconciled immediately against observations of hours the model
run had already ingested. They are hindcasts. Roughly a quarter of every
Open-Meteo poll was teaching Model A that its short-range forecast skill
equalled its analysis skill.

**Why it matters more than its severity suggests.** `blend_comparison` —
the entire point of v0.3.0 — grades each source on its *debiased* value,
and that debiasing is read from the contaminated `short` buckets. The
measurement inherited the contamination through the back door. The
December evaluation would have produced a number, and it would have been
wrong in a direction nobody could have inferred.

**Fix:** rejected at the shared validation barrier rather than in the
Open-Meteo parser, so it covers every provider including ones not yet
written. Rows are **dropped, not nulled**: a null-valued row still
creates a bucket, and the defect is the row's existence, not its number.

### The second path, found by asking rather than by reading

The fix above was incomplete on its first pass.
`MeteonomiqsCoordinator` writes `forecast_snapshots` directly and
**bypassed the validation barrier entirely** (SWF-ICS-014, rated Medium
by the external report and deprioritised in triage). A guard that lives
in a shared function protects only the callers that call it.

No harm had been done — those rows carry a variable-name prefix that
keeps them out of reconciliation, the blend and the reference median, and
that isolation was verified rather than assumed. But the structure was
wrong, and it would have forced a third database reset once discovered.

It was found by asking "what else could force a reset?", not by working
the finding list. That question is now a test:
`test_every_writer_of_learnable_rows_goes_through_the_barrier`.

---

## 3. Findings fixed

### 3.1 Defects introduced by v0.3.0

| ID | Severity | Fix |
| --- | --- | --- |
| **SWF-ICS-050** | High | `_get_model_metadata` updated its fetch timestamp on every attempt but only overwrote the cached value on success, so a failing endpoint returned the previous run's initialisation time indefinitely — refreshing its own staleness clock hourly and never clearing, while its docstring claimed it returned None when the run time was unknown. Failures are now cached and the entry cleared. **This is the precise failure ARC-04 exists to prevent, reproduced inside the fix for ARC-04.** |
| **SWF-ICS-049** | High | `_comparison_basis` tested membership in `OPEN_METEO_METADATA_MODELS` — whether a source *could* have a run time, not whether one was obtained. A failed fetch produced a comparison row claiming `run` while the forecast row it came from said `poll`. The per-row basis recorded at storage time is now threaded through |
| **SWF-ICS-046** | High | The comparison bulk insert committed without rollback, while `apply_blend_comparison_batch` forty lines below it did not. Both now use the shared transaction manager |

### 3.2 Measurement and learning integrity

| ID | Fix |
| --- | --- |
| **051, 014** | See §2 |
| **043** | Model A's reconciliation had no `ts > now` guard on station observations, unlike Model B. A clock-skewed or restored row could become ground truth for a forecast of the past |
| **069 / 076** | Staged `math.isfinite` guards in `blend()`, plus a final gate on the result. A randomized campaign found finite inputs producing `-inf` through accumulation overflow — input validation cannot catch this, because every input was finite |
| **048** | `get_reference_value` had no `issued_at` filter, so the station cross-check median mixed every run that ever covered an hour. A wider reference is more willing to accept a station reading it should have flagged, and those readings then teach Model A. Bounded to a 24-hour vintage window |

### 3.3 Radar integrity — the only confirmed wrong-output path

| ID | Fix |
| --- | --- |
| **032 / 065 / 074** | A missing or malformed product timestamp defaulted to `datetime.now()`, converting "this product's age is unknown" into "this product was made this instant" — the most dangerous direction to be wrong in for a freshness gate. The staler and more broken the file, the fresher it looked. Now rejects the file |
| **011** | The freshness gate tested `age > LIMIT` only, so a future-dated product passed trivially. Now bounded on both sides with a five-minute clock-skew tolerance |
| **010 / 021 / 057 / 066 / 071** | `gain` and `offset` went through bare `float()` and the decoded value had no physical bound. A NaN gain makes every pixel NaN, and NaN compares False against both the `nodata` and `undetect` sentinels — so it flowed to storage and Model B looking like a measurement. `provider_validation` guards every forecast value and was never wired to the radar path |
| **033 / 060 / 073** | Grid metadata is now cross-checked against the actual array shape. **Downgraded from High/Critical in triage**: this already failed closed, because the coordinator wraps the parse in `try/except` → `UpdateFailed`. The change is the error message, not the outcome — "grid metadata disagrees with the data array" is diagnosable; a bare `IndexError` from inside h5py is not |

### 3.4 Attack surface

| ID | Fix |
| --- | --- |
| **009** | The CombiPrecip asset `href` is read from a provider-supplied STAC document and was handed straight to the HTTP session, so the provider chose which host this integration contacted. Now an exact-match HTTPS allowlist. Exact match, not suffix: `evil-data.geo.admin.ch` ends with the trusted string |
| **008 / 026 / 070** | `resp.read()` allocated whatever arrived, on the one client downloading a binary file. Now a streaming read bounded at 64 MB, with the declared `Content-Length` refused first |
| **058** | The diagnostics deque bounded the event *count*; one multi-megabyte error string was retained whole and copied into every diagnostics download. Detail is truncated at 2000 characters |

### 3.5 Structural

| ID | Fix |
| --- | --- |
| **045 / 047 / 075** | A `_transaction()` context manager with commit-on-success and rollback-on-exception. **All eleven remaining writers were converted, not only the three flagged** — and a reachability test now fails if a future method commits without rollback. SQLite rolls back when the *process* dies; it does not roll back when an exception is caught inside a live process, where the transaction stays open and the next successful commit writes the earlier partial work |
| **028** | `wetteralarm_coordinator` was in neither cleanup tuple, so the setup auth-failure path leaked it. The comment above that tuple has claimed "EVERY already-constructed coordinator" since v0.1.24 and was true when written; v0.2.6 added a coordinator and silently falsified it |
| **017** | `assert status in (...)` replaced with an explicit raise. `python -O` strips assert, and nobody runs Home Assistant that way — but a validation whose existence depends on an interpreter flag is not a validation |

---

## 4. Findings declined

Documented because silently "fixing" a non-defect is itself a defect.

| ID | Reason |
| --- | --- |
| **042** | `INSERT OR IGNORE` is the documented dedupe. The proposed `ON CONFLICT DO UPDATE` would overwrite a genuine 24-hour-lead forecast with a recomputation made ten minutes later, destroying the lead-time semantics the measurement rests on |
| **039** | `synchronous=NORMAL` is a documented SD-card durability trade, and the report's own SIGKILL test passed against this exact configuration |
| **044** | `srf_probe.py` is a standalone maintainer script that never enters the event loop — which the report states in its own text before filing it |
| **015** | `storm_events` is Model B's entire training set and is documented as deliberately never purged. It gains a handful of rows per year |
| **018** | The claim was that reset cleanup invalidates observations "based on broad provider consensus". It uses fixed plausibility constants (`PRESSURE_PLAUSIBLE_MIN/MAX_HPA`), not consensus |
| **031** | Array-length mismatch is already detected, logged and recorded as a diagnostics event — that is the v0.1.19 fix. Truncation is front-aligned by index, so no misalignment occurs |
| **002 / 063 as proposed** | The duplicate-entry edge case is real; the proposed `_abort_if_unique_id_configured()` would abort on the entry being reconfigured and make relocation impossible, which the code comment already documents. Correct fix is a different-entry check. **Deferred, not applied** — the fix touches the config flow and this release already changes it in two places |
| **HDF5 quality sub-group** | Pre-dispositioned as P1-16 in v0.2.8: the quality code is in the filename, which is always present, while the sub-group is optional even within the spec |

---

## 5. Bugs introduced during this pass

### SWF-031-001 (Medium) — the reachability test read its own documentation as evidence

Found by mutation testing. Removing `wetteralarm_coordinator` from the
cleanup tuple left `test_every_constructed_coordinator_appears_in_a_cleanup_set`
**green**, because the v0.3.1 comment explaining SWF-ICS-028 sits inside
that block and contains the string being searched for.

The test was reading the explanation of the bug as proof the bug was
fixed. Comments are now stripped before the membership check.

This is the most instructive defect in the release: the fix for "a
comment asserting completeness is not a mechanism" was itself defeated by
a comment.

### SWF-031-002 (Medium) — three guards nothing exercised

Also from mutation testing:

- Deleting the **call** to `_assert_trusted_asset_origin` passed every
  test, because the origin tests exercised the function directly and not
  the download path meant to use it. A guard nothing invokes is not a
  guard.
- An `endswith()` implementation of the host check passed, because no
  fixture covered `evil-data.geo.admin.ch`.
- The final `isfinite(result)` gate in `blend()` was untested — every
  fixture was already stopped by an earlier stage.

All three closed.

### SWF-031-003 (Low) — the transaction conversion damaged its own helper

The scripted conversion of eleven writers to `_transaction()` matched the
helper itself and stripped its `commit()`. Caught immediately by three
existing migration tests. Recorded because an automated refactor that
edits the thing it is refactoring toward is a pattern worth not
repeating.

---

## 6. Test fixtures that were asserting the defect

Three fixtures failed against correct code, and in each case the fixture
was wrong rather than the fix.

| Fixture | Problem |
| --- | --- |
| `test_migration_from_v1_reopens_recent_rows_and_archives_old_ones` | Used one timestamp for both `issued_at` and `valid_at` — a zero-lead "forecast" for the hour it was issued in, which is an analysis and exactly what SWF-ICS-051's cleanup removes |
| `test_meteonomiqs_hourly_forecast_is_persisted_with_prefixed_variable_names` | Used a fixed past date, so it was asserting that a past-hour row gets persisted. That is the defect, not the feature |
| `_seed_providers` (pressure cross-check) | Hard-coded `issued_at`, which now ages out of the vintage window — the fixture would have silently stopped seeding and passed or failed for unrelated reasons |

---

## 7. The database reset, and whether there will be another

**This release clears learned state for the second time in ten days.**
Schema v5 is a data migration with no column changes, dispatched on its
own marker because a v0.3.0 database passes every column check and still
needs it.

Cleared: `bucket_stats`, `blend_comparison`, and the past-hour
`forecast_snapshots` rows (`valid_at <= issued_at`). Preserved: forward
forecast history, station observations, radar and storm history.

**Expect a visible accuracy dip for several days.** Every source returns
to cold start and the blend averages raw values until buckets refill.
That is the reset, not a fault.

### What could force a third reset

A reset is forced only by something that corrupts `bucket_stats`, because
an EMA cannot un-absorb a sample — "notice and fix it later" is not
available as a recovery strategy.

**Closed in this release:**

- past-hour rows (051) — and now covered at *every* writer, not just the
  one where it was found
- the Meteonomiqs bypass (014)
- future station observations as ground truth (043)
- non-finite learned statistics — refused at `apply_reconciliation_batch`,
  which is the boundary of the irreversible table. Every other guard is
  on the way in and is one refactor from being bypassed; this one holds
  regardless of what happens upstream
- a diluted station reference admitting bad observations (048)

**One remains, and nothing currently planned triggers it.**
`derive_lead_time_bucket` still keys off `issued_at` rather than run
time (ARC-04's second half). Switching it later would mix bucket
semantics and force a reset.

It is deferred on a measurement: at the 15:00Z run on 2026-09-10,
ICON-CH1's publication lag was 1h52m and ICON-D2's 1h29m — a **22-minute
spread**, which a 24-hour-wide bucket absorbs. The number that makes
run-relative bucketing necessary is AROME's **5h09m**, and AROME is not
in this release or the next one. If AROME is never added and lead-time
buckets are never narrowed, this never forces a reset.

---

## 8. Verification record

**Test suite.** 781 passing, up from 734. 47 new tests. Every regression
test was confirmed failing against the pre-fix code before the fix was
restored.

**Mutation testing.** Eleven mutations across the fixed code, each a
plausible implementation mistake rather than arbitrary noise:

| Mutation | Caught first pass |
| --- | --- |
| 051 guard removed | Yes (3 tests) |
| 051 zero-lead allowed | Yes (2 tests) |
| 050 stale metadata retained | Yes |
| 011 future radar allowed | Yes |
| 045 rollback removed | Yes |
| 014 Meteonomiqs bypasses barrier | Yes (4 tests) |
| Non-finite learned statistic allowed | Yes |
| 009 origin check call deleted | **No** → SWF-031-002 |
| 009 exact match → `endswith` | **No** → SWF-031-002 |
| 028 Wetter-Alarm removed again | **No** → SWF-031-001 |
| 076 final finite gate removed | **No** → SWF-031-002 |

Four of eleven escaped on the first pass. All four now caught. **The
value of this technique in this release was not confirming the fixes —
it was finding that four of them were untested while the suite was
green.**

**Not done.** No live Home Assistant run. The migration is exercised
against complete v2, v3 and v0.3.0-shaped fixtures; a fixture is not an
installation. No HDF5 parser test against a real malformed product — the
external report did that work and its findings were accepted on that
evidence rather than reproduced here.

**Verification gaps that remain open and are not closable by
inspection:** SWF-ICS-025 (executor-job cancellation), 029 (`gather()`
semantics under first-refresh failure), 062 (setup cancellation). These
need a live Home Assistant runtime. The external report says so itself.
They are gaps, not unfixed defects, and are recorded as such so they stop
looking like work nobody did.

**Carried, unverified:** the provider-quota and Model-B persistence
cluster (004, 005, 006, 007, 012, 013, 035, 036, 037, 038, 052, 053,
068). Each is a two-commit or shared-state claim of the family closed by
the transaction-ownership work. They should be re-tested against v0.3.1
rather than verified against v0.3.0.

---

## 9. What to send back

Unchanged from v0.3.0: download diagnostics from the device page in two
to three months and send the file.
`internal_coordinators.blend_comparison` holds the entire report.

**The evaluation window starts from this deploy, not the last one.** All
nine days of v0.3.0 data were contaminated by SWF-ICS-051 and have been
discarded.

If `total_pairs` is still 0 after a week, the measurement is not running
and that is worth investigating rather than waiting out.
