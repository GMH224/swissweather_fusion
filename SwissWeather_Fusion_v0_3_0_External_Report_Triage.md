# SwissWeather Fusion v0.3.0 — Triage of External ICS Code Defect Report

**Source document:** `SwissWeather_Fusion_v0_3_0_ICS_Code_Defect_Report.md`
(76 findings, adversarial implementation-first audit)
**Target:** `swissweather-fusion-v0.3.0`
**Triage date:** 11 September 2026
**Purpose:** disposition every finding before any is fixed, per the
project's standing rule that silently "fixing" a non-defect is itself a
defect (v0.2.8 audit §6).

---

## 1. Summary

| Disposition | Distinct | Note |
| --- | ---: | --- |
| **CONFIRMED — verified against source** | 19 | Read the code, reproduced the reasoning |
| **CONFIRMED but severity overstated** | 6 | Real, not High |
| **PARTIAL — real defect, wrong remediation** | 3 | Fixing as proposed would introduce a worse bug |
| **DECLINED — not a defect** | 4 | Includes one that would break the v0.3.0 measurement |
| **DUPLICATE of another finding** | 21 | Same defect counted 2–4 times across passes |
| **UNVERIFIED — plausible, not checked** | 23 | Mostly the CombiPrecip/HDF5 and quota clusters |

**76 findings collapse to roughly 47 distinct issues.** The duplication is
not trivial: the report's own severity table counts 48 aggregate P1,
which is not a credible posture for a Home Assistant weather integration
and overstates the real position.

**Of the 19 confirmed, three are defects introduced by v0.3.0** and one
pre-existing defect directly contaminates the measurement v0.3.0 exists
to produce.

---

## 2. The four that matter before the evaluation window

These are grouped first because they are the only findings whose timing
is forced. Everything else can wait for a scheduled remediation pass;
these decide whether the December data is worth reading.

### SWF-ICS-051 (CONFIRMED, raise Medium → High) — past-hour rows are graded as short-lead forecasts

**Verified:** `clients/open_meteo.py:316-340` parses every element of the
`hourly` array with no filter on `valid_at`. Open-Meteo's series begins
at 00:00 UTC today. `models/model_a.py:54-68` computes
`lead_hours = (valid_at - issued_at)` and returns `short` for anything
below 24, including negative values, because the first comparison is
`lead_hours < LEAD_TIME_SHORT_MAX_HOURS`.

**Consequence.** A poll at 15:00 UTC stores fifteen rows per variable per
source whose target hour has already passed. Those reconcile immediately
against observations of hours the model run already ingested — they are
hindcasts, not forecasts. Roughly a quarter of stored Open-Meteo rows
land in the `short` bucket carrying analysis skill rather than forecast
skill.

**Why this is the urgent one.** `blend_comparison` rows are recorded at
future targets only, so the paired rows themselves are clean. But each
source's `debiased` value in those rows is computed from `bucket_stats`,
and the `short` buckets are contaminated. The measurement inherits the
contamination through the back door.

**Correction:** reject `valid_at <= issued_at` at the storage boundary,
in `provider_validation`, so it applies to every provider rather than
only to the one that prompted it. Note this changes learned state, so it
belongs before the evaluation window starts, not during it.

### SWF-ICS-050 (CONFIRMED, v0.3.0 defect) — stale run time survives a metadata outage

**Verified:** `coordinator.py:210-227`. `_model_metadata_fetched[source]`
is updated on every attempt; `_model_metadata[source]` only on success.
A failing endpoint therefore returns the previous run's initialisation
time indefinitely, refreshing its own staleness clock hourly and never
clearing.

The docstring states the method "returns None whenever the run time is
unknown". It does not. It returns a stale value presented as
authoritative — the precise failure ARC-04 was written to prevent,
reproduced inside the fix for ARC-04.

**Correction:** cache the *result* including failure. On a failed fetch,
clear the entry rather than preserving it, or attach the fetch time to
the metadata object and treat anything older than two update intervals as
absent.

### SWF-ICS-049 (CONFIRMED, v0.3.0 defect) — basis inferred from provider class, not provenance

**Verified:** `coordinator.py:1879-1893`. `_comparison_basis` tests
membership in `OPEN_METEO_METADATA_MODELS` — whether a source *could*
have a run time, not whether one was obtained. If the fetch failed,
`forecast_snapshots` records `lead_time_basis='poll'` for that row while
the comparison row claims `'run'`. Two rows describing the same forecast
disagree about its provenance.

**Correction:** thread the actual per-source basis through
`_contributions_at` rather than re-deriving it from a constant. The value
is already known at the point the snapshot row was written.

### SWF-ICS-046 (CONFIRMED, v0.3.0 defect) — no rollback on the comparison bulk insert

**Verified:** `storage/db.py:1228-1237`. `executemany` then `commit`
inside `with self._lock`, no `try/except`. An exception leaves the
transaction open and releases the lock; a later successful call commits
the partial work. `apply_blend_comparison_batch`, forty lines below, has
exactly the guard this is missing — so the inconsistency is within one
module and one release.

---

## 3. Confirmed — verified against source

| ID | Disposition | Verification note |
| --- | --- | --- |
| **001** | CONFIRMED P1 | `config_flow.py:277-280`. `vol.Optional(..., default=0.0)` with no clear-override control in reconfigure. Submitting an unchanged form persists an explicit 0 m override; the `looked_up is None and override is None` guard at :244 does not fire because `override` is 0.0. At 471 m that is a systematic ~3 °C lapse-rate error, learned from silently. |
| **003 / 064** | CONFIRMED | Options flow uses bare `vol.Coerce(float)` (`config_flow.py:~577`) while setup and reconfigure use `_ELEVATION_VALIDATOR` (`:100`, finite + range −430…9000). Options therefore accepts `inf`, `nan` and 50000. Distinct from 001: options *has* a clear-override boolean, it just lacks the validator. |
| **017** | CONFIRMED, Low | `storage/db.py:960` uses `assert status in (...)` for an internal caller contract. Real, but HA is not run under `-O` and the caller is internal. Cheap to fix; not urgent. |
| **030** | CONFIRMED, downgrade High → Low | `coordinator.py:293` calls `record_success()` before the insert at `:401`. Real ordering issue, but health is a diagnostic signal with no control authority, and the fingerprint ordering — the part that actually mattered — was already fixed correctly in v0.1.24 (P0-04) and is documented at `:405-421`. |
| **039** | CONFIRMED as stated, decline as defect | `storage/db.py:350-352` sets `synchronous=NORMAL`, deliberately and with rationale (SD-card wear). The report is right that this weakens durability. It is a documented trade, not an oversight, and the report's own G6 SIGKILL test **passed** against this exact configuration. |
| **043** | CONFIRMED, Low | `coordinator.py:2624-2628` has no `ts > now` guard, unlike the Model B path. Requires a future-stamped station observation — clock skew or restored state. Cheap guard, low reachability. |
| **045 / 047 / 075** | CONFIRMED (one defect) | `insert_forecast_snapshots_bulk` (`:804-820`) and `purge_older_than` (`:1397-1424`) both commit without exception ownership, same as 046. This is one architectural gap in four finding numbers. |
| **048** | CONFIRMED, downgrade High → Low | `storage/db.py:841-849` filters on `variable`, `valid_at` hour and `source != 'blend'` but **not** on `issued_at`, so the median mixes every historical run for that hour — a 48-hour-old forecast weighted equally with a 1-hour-old one. Real. But this feeds the station *plausibility* cross-check, which asks "is the station wildly wrong", not a precision estimate. Dilution does not defeat that. |
| **051** | CONFIRMED, raise to High | See §2. |
| **046 / 049 / 050** | CONFIRMED, v0.3.0 | See §2. |
| **069 / 076** | CONFIRMED, reachability unproven | `models/model_a.py` contains no `math.isfinite` guard anywhere — verified by grep. The claim that finite inputs can overflow to ±inf through weighted multiplication is arithmetically sound. **However** `provider_validation` bounds-checks every stored value, so the reachable path is corrupted *learned state* (`ema_bias`, `ema_weight`), not a provider payload. The report does not establish that path. Real hardening item, speculative threat. |

---

## 4. Partial — real defect, proposed remediation would make it worse

### SWF-ICS-002 / 063 — duplicate config entries on reconfigure

The edge case is real: reconfiguring entry A onto entry B's coordinates
produces two entries with the same `unique_id`.

**The proposed fix is wrong.** Adding `_abort_if_unique_id_configured()`
after `async_set_unique_id` would abort on *this same entry* and make
relocation impossible — which is the problem the reconfigure step exists
to solve, and which the code comment at `config_flow.py:252-258`
explicitly documents.

Correct fix: abort only when the matching entry is a different
`entry_id`. Same shape as **P1-17** in the v0.2.8 audit, where a proposed
fix would have converted a silent bug into a permanent hard outage.

### SWF-ICS-042 — `INSERT OR IGNORE` "silently discards" a conflicting row

**DECLINED as a defect, and the proposed fix would break the release.**

The uniqueness constraint is the documented dedupe. The blend coordinator
runs every ten minutes; a second write for the same
`(valid_at, measurement, lead_hours)` is not a conflicting observation,
it is the same cell recomputed at a *shorter* actual lead. First-write
wins is what makes a 24-hour-lead cell genuinely 24 hours ahead.

The suggested `ON CONFLICT ... DO UPDATE` would overwrite a genuine
24-hour forecast with a recomputation made ten minutes later, destroying
the lead-time semantics the entire measurement rests on.

### HDF5 quality cluster (021, 033, 071, 072) — partially pre-dispositioned

Any of these that reduce to "parse the ODIM quality sub-group" were
already declined in v0.2.8 as **P1-16**: the quality code is in the
filename, which is always present, while the sub-group is optional even
within the spec. Findings 071–073 appear to be structurally different
(dataset-shape validation, not quality parsing) and are carried forward
as UNVERIFIED rather than declined.

---

## 5. Declined

| ID | Reason |
| --- | --- |
| **042** | Would break the v0.3.0 measurement — see §4 |
| **039** | Documented durability trade; the report's own G6 test passed against it |
| **044** | `srf_probe.py` is a standalone maintainer script that never enters the event loop. The report states this itself and files it anyway |
| **015** | `storage/db.py:1378` documents that `storm_events` is Model B's entire training set and is deliberately never purged. Unbounded growth of a training set is the design, and the table gains a handful of rows per year |

---

## 6. Duplicates

Counted once each in §1. Listed so the remediation plan does not open
twenty-one tickets for seven defects.

| Cluster | Finding IDs | Distinct defects |
| --- | --- | --- |
| Missing radar timestamp → `now` | 032, 065, 074 | 1 |
| Unbounded HTTP body | 008, 026, 070 | 1 |
| SRF token refresh not single-flight | 023, 034 | 1 |
| Duplicate config-entry identity | 002, 063 | 1 |
| Options-flow elevation validator | 003, 064 | 1 |
| HDF5 calibration accepts non-finite | 057, 066 | 1 |
| Radar quality in mutable client state | 022, 067 | 1 |
| Raster dims vs actual shape | 060, 073 | 1 |
| Transaction ownership | 045, 046, 047, 075 | 1 pattern, 3 sites |
| NaN/Inf in blend arithmetic | 069, 076 | 1 |
| Meteoblue quota persistence | 006, 007, 036 | 2 |

---

## 7. Second pass — the previously unverified findings

All verified against source on 11 September 2026. Summary of what
changed: **four more confirmed at High**, **five downgraded to Low
because the failure mode is fail-safe**, **one merged into another
finding**, and **one confirmed defect that no prior audit caught because
a code comment asserted it could not exist**.

### 7.1 Radar integrity — CONFIRMED, and the largest real cluster

| ID | Disposition | Verification |
| --- | --- | --- |
| **032 / 065 / 074** | **CONFIRMED High** | `combiprecip.py:435` sets `valid_at = datetime.now(timezone.utc)` unconditionally, overwritten only `if date_str and time_str`. A product with missing or malformed `/what` date-time becomes "produced right now". Converts unknown age into maximum freshness |
| **011** | **CONFIRMED High** | `model_b.py:282` gates on `now - valid_at > RADAR_FRESHNESS_LIMIT`. A future `valid_at` yields a negative timedelta and passes. Compounds with 032: a timestampless product is stamped `now` and is trivially "fresh" |
| **010 / 021 / 057 / 066 / 071** | **CONFIRMED High (one defect)** | `combiprecip.py:427,474` — `gain`/`offset` go through bare `float()` with no `isfinite`, and `value = float(raw_value) * gain + offset` has no physical bound. `provider_validation` is applied to forecast rows and **not** to `radar_observations`. NaN or absurd accumulation can reach Model B and storage |
| **033 / 060 / 072 / 073** | **CONFIRMED, downgrade High/Critical → Low** | The claims are structurally right: `f["dataset1"]["data1"]` raises `KeyError` if absent, and `_pixel_indices` derives an index from declared `xsize`/`ysize` that can exceed the real `data.shape`. **But** `coordinator.py:977-1002` wraps the entire fetch-and-extract in `try/except Exception` → `UpdateFailed`. The result is a failed poll, which HA treats as a normal transient failure. That is fail-safe, not a crash and not data corruption. The report's "can crash the radar path" is wrong |
| **056 / 067** | **CONFIRMED, Low** | `self._last_asset_quality` is genuinely set in the async fetch (`:573`) and read in the executor job (`:596`), and the split is documented at `:568-572`. A real design smell. Reachability is low: one `DataUpdateCoordinator` serialises its own refreshes, so two CombiPrecip parses cannot interleave in normal operation |
| **009** | **CONFIRMED, Low** | `self._session.get(latest.href)` with no scheme or host policy. Requires MeteoSwiss's STAC catalogue to be serving hostile hrefs; TLS covers the transport case |

**The radar cluster is the report's strongest work.** Findings 032, 011
and the validation-barrier group are real, reachable, and affect the
storm signal. They are also the only confirmed findings in this report
that could produce a *wrong output* rather than a failed one.

### 7.2 Setup and lifecycle

| ID | Disposition | Verification |
| --- | --- | --- |
| **028** | **CONFIRMED** | `__init__.py:276` constructs `wetteralarm_coordinator`; `:315-332` builds `source_coordinators` and `derived_coordinators_for_cleanup` without it. On a setup auth failure the Wetter-Alarm coordinator is never shut down. **Notable:** the comment at `:323-325` states the cleanup covers "EVERY already-constructed coordinator". That was true when written in v0.1.24 and was silently falsified by v0.2.6 adding a new coordinator. A comment asserting completeness is not a mechanism — this wants a reachability test, not a one-line fix |
| **025 / 029 / 062** | **UNRESOLVED — agree with the report** | Executor-job cancellation, `gather()` semantics under first-refresh failure, and setup cancellation cannot be qualified without a live Home Assistant runtime. The report says so itself. Carried as a verification gap, not a defect |

### 7.3 Remainder

| ID | Disposition | Verification |
| --- | --- | --- |
| **058** | **CONFIRMED, Low** | `diagnostics_recorder.py:56` bounds the event *count* via `deque(maxlen=...)`. `detail: str` (`:43`) has no length bound, so one enormous exception string is retained. Truncating `detail` at write is a two-line fix |
| **059** | **MERGED into 003/064** | `unit_conversion.py:188` reaches `math.exp(exponent)` with an unbounded `elevation_m`, but `_ELEVATION_VALIDATOR` bounds setup and reconfigure to −430…9000 m. The only path to an extreme value is the options flow's bare `vol.Coerce(float)`. Not an independent defect — it is the *consequence* of 064 |
| **055** | **CONFIRMED, Low** | `coordinator.py:2088-2092` queries a 168-hour window with no row cap — order 20,000 rows materialised per blend cycle, every ten minutes. Real memory pressure, no correctness impact. Context matters: this shape deliberately replaced roughly 8,400 individual round trips |
| **031** | **DECLINED as new** | Array-length mismatch is already detected and surfaced — `open_meteo.py:331-333` records `array_length_mismatches` and the coordinator logs and records a diagnostics event. That is the v0.1.19 fix. The report is describing behaviour that was deliberately left as truncate-plus-warn |
| **004 / 005 / 035 / 036 / 037 / 038 / 052 / 053 / 068** | **UNVERIFIED, carried** | The provider-quota and Model-B persistence cluster. Each is a two-commit or shared-state claim of the same family as 045/046/047, and the family is already confirmed. Rather than verify nine separately, treat them as covered by the transaction-ownership work and re-test afterwards |
| **012 / 013 / 014 / 016 / 018 / 019 / 020 / 024 / 027 / 041 / 054 / 061** | **UNVERIFIED, low priority** | Nothing in this group claims a wrong-output path. Deferred to a scheduled hardening pass |

---

## 8. Remediation plan

### v0.3.1 — before the evaluation window (forced by timing)

| Finding | Change |
| --- | --- |
| **051** | Reject `valid_at <= issued_at` in `provider_validation`, so it covers every provider. Requires a second `bucket_stats` reset, which is why it must land before the window rather than during it |
| **050** | Cache metadata failures, not just successes. Treat anything older than two update intervals as absent |
| **049** | Thread the actual per-source basis through `_contributions_at` instead of re-deriving it from a constant |
| **046** | Add the `try/except/rollback` that `apply_blend_comparison_batch` already has |

Roughly a day. **051 decides whether the December data is worth reading**
and is the only item here that is not optional.

### v0.3.2 — radar integrity and the elevation model

| Finding | Change |
| --- | --- |
| **032 / 065 / 074** | Missing or malformed product time must reject the file or yield `valid_at=None`. Never synthesise `now` for external measurement data |
| **011** | Reject future-dated radar in the freshness gate — an absolute bound, not a one-sided one |
| **010 / 021 / 057 / 066 / 071** | Extend the `provider_validation` barrier to `radar_observations`: finite check on `gain`, `offset` and result, plus a physical bound on hourly accumulation |
| **001** | Represent "no override" explicitly in reconfigure. Never use `0.0` as a sentinel for an optional physical parameter |
| **003 / 064 / 059** | Apply `_ELEVATION_VALIDATOR` in the options flow. Closes the only reachable path to the barometric overflow |

### v0.3.3 — architectural passes, one control per cluster

1. **Transaction ownership** (045/046/047/075, and probably 006/007/036/037/068): one context manager guaranteeing rollback, applied to every multi-statement public DB method.
2. **Reachability tests over construction and cleanup sets** (028): a test asserting every constructed coordinator appears in the cleanup tuple. The bug was a comment claiming completeness — the fix is a test that can hold it.
3. **Bounded strings and bodies** (058, 008/026/070).
4. **Finite guards in `model_a`** (069/076), after — not before — establishing whether corrupted learned state is actually reachable.

### Closed with no action

**042** (would break the measurement), **039** (documented trade, passed
the report's own crash test), **044** (out of scope by the report's own
description), **015** (never-purged training set is the design), **031**
(already handled as truncate-plus-warn), **002/063 as proposed** (fix the
different-entry check instead, not `_abort_if_unique_id_configured`),
and any HDF5 finding reducing to "parse the ODIM quality sub-group"
(P1-16, v0.2.8).

### Verification gaps that stay open

**025, 029, 062** cannot be closed without a live Home Assistant runtime.
They should be release-gating for any deployment that actually matters,
and they are not closable by inspection — which is worth stating plainly
rather than leaving them looking like unfixed defects.

---

## 9. Original unverified list (superseded by §7)

Not checked against source in this pass. Listed so the gap is explicit
rather than implied by omission.

**CombiPrecip / HDF5 cluster:** 009, 010, 011, 021, 032, 033, 056, 057,
060, 065, 066, 067, 070, 071, 072, 073, 074. This is the single largest
untriaged block and the one where the report's evidence is strongest —
several claims are backed by reproduced parser failures against synthetic
files, which is a stronger form of evidence than inspection.

**Provider quota and concurrency:** 004, 005, 006, 007, 012, 013, 014,
035, 036, 037, 038, 052, 053, 068.

**Lifecycle:** 025, 028, 029, 062. The report concedes these are
unqualified without a live Home Assistant runtime.

**Other:** 016, 018, 019, 020, 024, 027, 031, 041, 054, 055, 058, 059,
061.

---

## 8. Assessment of the report itself

**What it got right.** The four findings in §2 are real, and two of them
I introduced last week. The report found them by reading the
implementation rather than the comments, which is the correct method and
is explicitly what it claims to do. SWF-ICS-051 in particular is a defect
that has been live since v0.1 and that three prior audits missed.

Its closing section is the most valuable part: the remaining work is four
or five architectural controls — one validation barrier, one bounded-body
policy, one transaction context manager, one invariant layer — not
seventy patches. That framing is right.

**Where it is unreliable.** Severity inflation is systematic. 48
aggregate P1 for a weather integration with no actuator authority is not
a defensible posture, and the report's own text concedes "no direct
physical-actuator claim because no PLC layer is present" while retaining
ICS severity language throughout. Several findings are filed against code
the report itself describes as out of scope (044). At least two proposed
remediations would introduce worse defects than they fix (002, 042).

**On the exclusion of prior audits as evidence.** Defensible as method —
it prevents inherited blind spots — but it means the report re-raises
material already dispositioned with reasons, and offers no engagement
with those reasons. P1-16 is the clearest case.

---

## 9. Recommendation

**Do not open 76 tickets.**

**Before the evaluation window (small, forced by timing):** 051, 050,
049, 046. Roughly a day's work. 051 is the one that decides whether the
December measurement is worth reading.

**Next, independent of the window:** 001 — a silent 3 °C model error is
worse than a crash — and 003/064, which is the same subsystem and should
be fixed in the same pass.

**Then, as a structured pass rather than per-finding:** the transaction
context manager (045/046/047/075), the validation barrier, and the
bounded-body policy. Each closes a cluster.

**Verify before fixing:** the entire CombiPrecip/HDF5 block. It is the
largest remaining unknown, the report's evidence there is its strongest,
and it is also where a previously-declined finding sits — so it needs
reading, not reflexive acceptance or reflexive rejection.
