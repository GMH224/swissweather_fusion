# SwissWeather Fusion v0.1.23 — Consolidated Defect Log

**Purpose:** a single, verified register of every known defect in the delivered
v0.1.23 build, replacing the need to read the two external audits and the
v0.1.24 reconstruction document side by side.

**Date:** 1 September 2026
**Reviewed artifact:** `swissweather-fusion-v0_1_23.zip` (66 files, 7,406
production Python lines)

**Inputs consolidated here:**

1. `SwissWeather_Fusion_v0_1_23_ICS_Style_Code_Audit.md` — second external
   ICS-style audit, 50 findings (4 P0 / 30 P1 / 13 P2 / 3 P3).
2. `swissweather_fusion_v0_1_23_to_v0_1_24_bugfix_and_architecture_m.pdf` —
   the reconstruction document describing a remediation pass whose working
   environment was lost. Treated here as a **proposed fix specification**, not
   as evidence that a defect exists.
3. An independent audit of the production source performed for this log,
   which contributed 13 findings (IND-01 … IND-13) not present in either
   document.

**Total: 63 findings.**

---

## 1. Method and baseline

Every audit finding was checked against the actual v0.1.23 source rather than
accepted from either document. Neither the `tests/` directory nor the
pre-existing remediation audits inside the zip were used as evidence that a
defect does or does not exist; they were used only to establish the baseline
below.

Baseline facts, independently reproduced:

| Check | Result |
| --- | --- |
| `python -m pytest tests/` | 198 passed |
| `python -m pyflakes custom_components/swissweather_fusion/` | 5 warnings, all pre-existing unused imports |
| Undefined-name defects (pyflakes) | none present |
| Test dependencies required | `pytest`, `pyflakes`, `aiohttp`, `h5py`, `numpy`, `pyproj` |

Both figures match the reconstruction document's claims exactly, which is the
main reason that document is treated below as a credible fix specification
even though its code no longer exists.

External documentation was consulted where a finding turns on a third-party
product contract. The MeteoSwiss open-data radar documentation
(`https://opendatadocs.meteoswiss.ch/d-radar-data/d1-precipitation-radar-products`)
is decisive for five findings and is quoted where relevant.

### Verdict codes

| Code | Meaning |
| --- | --- |
| **CONFIRMED** | Defect reproduced in the source as described. |
| **CONFIRMED / evidence corrected** | Defect is real; the audit's stated mechanism is wrong in a way that matters for the fix. |
| **PARTIAL** | Part of the claim holds, part does not. |
| **REFUTED** | No defect. The code is correct as written. |
| **FIX-DIVERGENT** | Defect is real, but the fix proposed in the reconstruction document must **not** be applied as written. |

### Verdict summary

| | Count |
| --- | --- |
| Audit findings confirmed | 47 |
| Audit findings partially valid | 2 (P1-22, P2-13) |
| Audit findings refuted | 1 (P1-19) |
| Audit findings whose proposed fix is wrong or insufficient | 6 (P1-13, P1-14, P1-16, P1-17, P1-19, P2-01) |
| Independent findings added | 13 |
| **Total open defects** | **62** (63 findings less one refuted) |

### Assessed severity after review

Severity below is *assessed*, not inherited. Where it differs from the audit's
own rating the change is stated in the finding.

| Assessed | Count | IDs |
| --- | --- | --- |
| P0 | 6 | P0-01, P0-02, P0-03, P0-04, IND-13, IND-01 |
| P1 | 31 | see sections 3 and 6 |
| P2 | 20 | |
| P3 | 5 | |

Three severity changes are worth stating here rather than burying:

- **P0-04** is worse than described. No crash is required to trigger it.
- **IND-13** (CombiPrecip asset selection) is rated P0 and is a superset of the
  audit's P1-17. The client is probably parsing the wrong product file today.
- **IND-01** (Model A blend weights) is rated P0 because it silently inverts
  the integration's core function for two of five measurements.

---

## 2. Cross-cutting root cause

Findings cluster by file in a way that points at one cause rather than fifty
independent mistakes.

| File | Audit findings citing it | Under test? |
| --- | --- | --- |
| `coordinator.py` (1,674 lines) | 26 | **No** — syntax only |
| `storage/db.py` | 8 | Yes |
| `models/model_b.py` | 8 | Yes |
| `__init__.py` | 7 | **No** — syntax only |
| `config_flow.py` | 7 | **No** — syntax only |
| `clients/combiprecip.py` | 5 | Partly |
| `clients/meteoblue.py` | 5 | Yes |
| `sensor.py` | 4 | **No** — syntax only |
| `const.py` | 4 | n/a |

`tests/conftest.py` stubs Home Assistant so the pure-logic modules
(`models/`, `clients/`, `storage/`, `health.py`, `redaction.py`) can be tested
without installing HA. The consequence, stated honestly in that file's own
docstring, is that `coordinator.py`, `__init__.py`, `config_flow.py`,
`sensor.py`, `weather.py` and `binary_sensor.py` are **only checked for
syntactic validity**. Forty of the sixty-three findings live in files with no
executable test coverage. That is the defect that produced the other defects.

Two stub defects will block remediation before it starts and are therefore
listed as findings in their own right — see **INFRA-01** and **INFRA-02** in
section 7.

---

## 3. P0 findings

### P0-01 — Model A EMA update is not atomic with the reconciliation status transition

- **Verdict:** CONFIRMED
- **Assessed severity:** P0 (unchanged)
- **Location:** `coordinator.py` `ModelALearningCoordinator._reconcile()`;
  `storage/db.py` `upsert_bucket_stats()`, `mark_forecast_snapshots_status()`

**Evidence.** `_reconcile()` calls `self._db.upsert_bucket_stats(...)` inside
the per-row loop, and `upsert_bucket_stats()` commits on every call. The two
`mark_forecast_snapshots_status()` calls happen once, after the loop
completes. A crash anywhere in between leaves `bucket_stats` already updated
for rows still marked `pending`.

**Impact.** Those rows are re-selected by
`get_pending_forecast_snapshots()` on the next cycle and folded into the EMA a
second time. This defeats the at-most-once learning guarantee that the entire
v0.1.23 `reconciliation_status` redesign exists to provide — reintroducing the
prior pass's L-01 bug through a crash boundary instead of watermark
arithmetic.

**Fix direction.** A single DB primitive
(`apply_reconciliation_batch(bucket_updates, reconciled_ids, skipped_ids)`)
that performs every EMA write and every status transition for one cycle in one
transaction. The reconstruction document's design is sound and should be
followed, including its two secondary corrections:

1. SQLite only auto-rolls-back on the next process start; an exception caught
   inside the same process leaves the transaction open. An explicit
   `try/except: self._conn.rollback(); raise` is required.
2. Two rows landing in the same `bucket_stats` key within one batch must build
   on each other's in-flight result. A naive "defer all writes, commit once"
   implementation silently drops one of them. A `pending_bucket_state` dict
   consulted before falling back to the DB is the stated remedy.

**Test.** Fault injection at the commit boundary; assert each forecast row
contributes to `bucket_stats` at most once. Plus an explicit same-batch,
same-bucket sequencing test — that case is not covered by the all-or-nothing
test.

---

### P0-02 — Crossing detection uses the refined probability as its state variable

- **Verdict:** CONFIRMED
- **Assessed severity:** P0 (unchanged). See **IND-04** — the trigger rate is
  far higher than the audit implies.
- **Location:** `coordinator.py` `ModelBCoordinator._async_update_data_inner()`;
  `models/model_b.py` `refine_with_meteonomiqs()`, `evaluate_cross_model_trigger()`

**Evidence.** `self._previous_probability = probability` stores the
post-refinement value. `evaluate_cross_model_trigger()` compares the next
cycle's *unrefined* `base_probability` against it. `refine_with_meteonomiqs()`
is a plain average with `meteonomiqs_risk_value / 9`, which can pull the stored
value below threshold while the base signal stays genuinely elevated.

**Impact.** One storm produces a repeated "upward crossing" every scoring
cycle. Quota is protected by the daily bonus caps, but `storm_predictions`
fills with duplicate pseudo-events and the trigger log becomes meaningless as
a record of distinct storms — which matters because that table is the intended
Model B v1 training input.

**Fix direction.** Persist `base_probability` as the crossing state variable;
keep `current_probability` refined for display and history. The v0.1.15
reasoning for why the *displayed* value should stay refined is still valid and
should not be reverted.

**Test.** Drive two real scoring cycles through the actual coordinator method
with `score_v0_graduated` patched to a fixed elevated value and a fake
Meteonomiqs returning a low risk; assert cycle 2 does not re-trigger.

---

### P0-03 — Database can be closed before coordinator shutdown on normal unload

- **Verdict:** CONFIRMED
- **Assessed severity:** P0 (unchanged)
- **Location:** `__init__.py` `async_unload_entry()` and the
  `entry.async_on_unload(coordinator.async_shutdown)` registration loop

**Evidence.** `async_unload_entry()` unloads platforms, pops the runtime dict,
and calls `runtime["db"].close()` directly. Coordinator shutdown is registered
via `entry.async_on_unload(...)`, and Home Assistant fires those callbacks from
`ConfigEntries.async_unload()` **after** the integration's own
`async_unload_entry` returns. `db.close()` therefore always runs before any
coordinator has actually shut down.

**Impact.** An in-flight or about-to-fire coordinator refresh can reach a
closed SQLite connection, producing intermittent `sqlite3.ProgrammingError`
during reload/unload. Every options change triggers a reload, so this is on a
routine path.

**Fix direction.** Remove the `async_on_unload(coordinator.async_shutdown)`
loop entirely; explicitly `await` every coordinator's `async_shutdown()` in
`async_unload_entry` before `db.close()`. The reconstruction document argues
for keeping the coordinator list duplicated between the unload path and the
setup-failure cleanup path rather than factoring it into a helper, on the
grounds that a silent divergence would be worse than the duplication. That is
a defensible call but should be paired with a test asserting the two lists are
identical.

**Test.** Call the real `async_unload_entry` with a call-order-recording fake
runtime; assert `db.close` is last.

---

### P0-04 — Open-Meteo fingerprint is committed before forecast rows

- **Verdict:** CONFIRMED, **severity raised**
- **Assessed severity:** P0. The audit describes a crash window; the actual
  defect needs no crash.
- **Location:** `coordinator.py` `OpenMeteoCoordinator._async_update_data()`

**Evidence.** In the per-source loop, both of these run before
`insert_forecast_snapshots_bulk(...)`:

```
self._last_run_fingerprint[source] = parsed.run_fingerprint
await ...(self._db.set_provider_run_fingerprint, source, parsed.run_fingerprint)
```

**Impact beyond the audit's claim.** Because the *in-memory* cache is also
updated first, an ordinary insert failure — a transient SQLite error, a
`ProgrammingError` from P0-03, a full disk — is enough. No process death is
required. The run is then treated as already-processed for the remainder of the
process lifetime, and permanently after restart because the fingerprint was
also persisted. A complete provider run is lost with no error surfaced.

**Independent verification of the negative claim.** SRF and Meteoblue were
checked directly and are correctly ordered (rows inserted first, fingerprint
set after). The audit's title already scoped this to Open-Meteo; the
reconstruction document slightly overstates by describing the audit's claim as
having been "general".

**Fix direction.** Reorder: insert first, then set fingerprint (both the
in-memory cache and the DB). Ideally both in one transaction, but ordering
alone closes the observed failure mode.

**Test.** Fault injection — a DB wrapper whose `insert_forecast_snapshots_bulk`
always raises; assert `set_provider_run_fingerprint` was never called *and*
that the next cycle re-attempts the same run.

---

### IND-13 — CombiPrecip asset selection is effectively arbitrary; the client is probably parsing the wrong product

- **Verdict:** NEW (independent). Supersedes and subsumes audit finding P1-17.
- **Assessed severity:** P0
- **Location:** `clients/combiprecip.py` `parse_stac_items_response()`
- **First raised:** initial evaluation pass (as "NEW-01")

**Evidence.** `parse_stac_items_response()` iterates every feature, takes
`properties.datetime` as the asset's `valid_at`, and appends **any** asset
whose `href` ends in `.h5`. It then sorts newest-first and the caller takes
`[0]`.

MeteoSwiss documents that the `ch.meteoschweiz.ogd-radar-precip` collection is
split by parameter **and calendar date**, with the individual files exposed as
assets of a per-date STAC item. The collection carries several distinct
products side by side:

| Product | File name pattern | Semantics |
| --- | --- | --- |
| PRECIP | `RZCyyjjjHHMM*.*01.h5` | instantaneous rain rate, mm/h |
| PRECIP-SV | `TZCyyjjjHHMM*.*01.h5` | instantaneous rain rate, mm/h |
| CombiPrecip 60-min total | `CPCyyjjjHHMMQ_00060.*01.h5` | accumulation over 1 hour, mm |

Consequences of the current logic:

1. **Product identity is not checked at all.** `RZC`, `TZC` and `CPC` files all
   end in `.h5` and all match. The client can and probably does download a
   non-CombiPrecip product.
2. **The sort key is a date, not a scan time.** All assets within one daily
   item share the same `properties.datetime`, so the "newest-first" sort
   degenerates to insertion order within the newest day. `[0]` is an arbitrary
   file from today, not the latest scan.
3. **`StacAsset.valid_at` is wrong** for the same reason. The genuine scan time
   is only recovered later, from the HDF5 `/what` `date`/`time` attributes in
   `extract_values_at_points()`.
4. MeteoSwiss further documents that when the quality flag changes, a second
   file is produced rather than overwriting the first — so multiple valid CPC
   files for one nominal time are expected, not exceptional.

**Impact.** Model B's radar evidence may be sampled from the wrong physical
quantity entirely (instantaneous mm/h vs. accumulated mm), from an arbitrary
time of day, with the freshness gate proposed in P1-13 computed against a
date-level timestamp. This is upstream of P1-13, P1-14, P1-16 and P1-18 — all
four of those fixes are only meaningful once the right file is being fetched.

**Fix direction.** Select by filename contract, not suffix:

- filter to assets whose basename starts with the `CPC` product code and
  contains the `_00060` accumulation-time segment;
- parse `yyjjjHHMM` from the filename to get the real product time, and sort on
  that rather than on `properties.datetime`;
- parse the single-digit quality code `Q` from the filename (see IND / P1-16);
- when two files share a product time (the documented quality-flag case),
  prefer the higher quality code deterministically.

**Do not apply the reconstruction document's fix for P1-17.** It raises
`ValueError` when a feature exposes more than one `.h5` asset. Multiple `.h5`
assets per feature is the documented normal case, so that change converts a
silent wrong-product bug into a hard, permanent outage of the radar source.

**Test.** A STAC fixture built from the documented naming convention
containing RZC, TZC and two CPC files (different quality codes) for the same
day; assert the CPC file with the latest `HHMM` and best quality is selected.

---

### IND-01 — Model A's learned blend weights are unnormalized and unit-dependent

- **Verdict:** NEW (independent)
- **Assessed severity:** P0
- **Location:** `models/model_a.py` `blend()`, `update_bucket_ema()`;
  `const.py` `EMA_WEIGHT_EPSILON`, `MIN_SAMPLES_TO_TRUST_BUCKET`

**Evidence.** `blend()` assigns two weights from two incompatible scales:

- a source below `MIN_SAMPLES_TO_TRUST_BUCKET` (5) contributes its raw value
  with weight **exactly `1.0`**;
- a trusted source contributes its debiased value with weight
  **`1 / (ema_abs_error + 0.01)`**, which carries the measurement's own units.

Executed against the real module:

```
humidity: trusted source, 200 samples, MAE 5%     -> weight 0.20
          blend(trusted=60%, cold_start=90%)      = 85.0%
pressure: trusted source, 200 samples, MAE 0.3hPa -> weight 3.23
          blend(trusted=1010, cold_start=1000)    = 1007.63
maximum achievable weight (1 / EMA_WEIGHT_EPSILON) = 100
```

**Impact.** For measurements whose absolute errors are numerically large —
humidity (%) and precipitation (mm) — every well-characterised source is
weighted *below* the neutral cold-start weight, so a source with 200 validated
samples is outvoted roughly 5:1 by a source with one. For pressure the bias
runs the other way. The blend therefore degrades as learning proceeds for two
of five measurements. There is also no upper bound: a bucket that happens to
reach a near-zero EMA error dominates the blend 100:1.

This is not a crash-boundary or quota defect; it silently inverts the
integration's stated core purpose, which is why it is rated P0 despite being
absent from both external audits.

**Fix direction.** Options, in rough order of preference:

1. Normalise weights within the contribution set each blend — e.g. weight each
   source by `median_error / (error + eps)` so the scale is dimensionless and
   the cold-start neutral value is genuinely neutral.
2. Give cold-start sources a weight drawn from the same scale (e.g. the group's
   worst observed error) rather than a hard-coded `1.0`.
3. Cap the weight ratio between the strongest and weakest contributor.

Whichever is chosen, it needs stating in `DEVELOPER.md` as a model decision,
not just as a code change.

**Test.** Property test asserting that adding samples to a source never moves
its blend influence in the wrong direction, run across all five measurements
with realistic per-measurement error magnitudes.

---

## 4. P1 findings — credentials and secrets

### P1-01 — Auth errors never trigger Home Assistant's reauth flow

- **Verdict:** CONFIRMED / evidence corrected
- **Assessed severity:** P1

**Correction to the audit's stated mechanism.** The audit claims the
`UpdateFailed` wrapper hides the HTTP status from `health.classify_exception()`.
That is **false**: `SrfCoordinator` calls `self.health.record_error(err)` on the
*original* exception before wrapping, so classification and diagnostics are
already correct. Building the fix around "preserve the status in the wrapper"
would address a problem that does not exist.

**The real defect.** `ConfigEntryAuthFailed` appears nowhere in the production
code (verified by grep across `custom_components/`). Concretely:

- `SrfCoordinator` has a `kind == "auth"` branch that only logs, then raises
  the generic `UpdateFailed`, which HA does not treat as an auth problem.
- `MeteoblueCoordinator` has no auth branch at all.
- `MeteonomiqsCoordinator._async_update_data` catches every exception from the
  keepalive path and only logs it — a revoked key produces no user-visible
  signal whatsoever.
- `OpenMeteoCoordinator`'s per-source loop `continue`s past every failure.

**Impact.** A revoked or rotated credential never surfaces a reauth prompt. The
integration degrades silently and indefinitely.

**Fix direction.** Raise `ConfigEntryAuthFailed` for `kind == "auth"` in SRF and
Meteoblue. For Meteonomiqs, classify with `classify_exception(err)` directly
rather than `health.record_error()` (which has already run earlier in the same
call chain — calling it again double-counts the failure), re-raise for auth
only, and preserve the deliberate silent degradation for every other kind. For
Open-Meteo, track auth failure across the loop and raise only after the loop
completes and only if no source returned usable data, preserving per-source
fault tolerance.

**Blocked by:** INFRA-01.

---

### P1-02 — Reauth writes to `entry.data` while runtime resolution is options-first

- **Verdict:** CONFIRMED
- **Location:** `config_flow.py` `async_step_reauth_confirm()`; `__init__.py`
  credential resolution

**Evidence.** Every runtime credential is resolved as
`options.get(KEY, data[KEY])` — options wins whenever the key is present.
`async_step_reauth_confirm()` calls
`async_update_entry(existing_entry, data=self._data)` and never touches
options. A stale SRF key previously set through the options flow keeps winning
after the UI reports "reauth successful".

**Fix direction.** Build `new_options = dict(existing_entry.options)` with the
two SRF keys popped, and pass `options=new_options` in the same
`async_update_entry` call — clearing the stale copy while preserving unrelated
option entries.

---

### P1-03 — API keys can reach ordinary Home Assistant logs via exception strings

- **Verdict:** CONFIRMED
- **Location:** `clients/open_meteo.py` `build_forecast_url()` /
  `build_elevation_url()`; `clients/meteoblue.py` `build_forecast_url()`;
  the `_LOGGER.warning(... err)` call sites in `coordinator.py`

**Evidence.** Both clients append `&apikey={key}` to the request URL.
`aiohttp`'s exception `str()` includes the full request URL. Coordinators log
the raw exception at WARNING level. Diagnostics redaction operates on the
diagnostics payload only, not on logger output.

**Fix direction.** Retain `self._api_key` in both coordinators (currently
constructed and discarded) and wrap exception text with
`redact_secret_values(str(err), secrets=[self._api_key])` before logging or
recording. Additionally, `raise X from err` preserves the unredacted original
as `__cause__`, so any logged traceback re-exposes it — change the relevant
raises to `from None`.

---

### P1-04 — Diagnostics redaction collects secrets from `entry.data` only

- **Verdict:** CONFIRMED
- **Location:** `diagnostics.py` `async_get_config_entry_diagnostics()`

**Evidence.** The `secrets` list is built from five `entry.data.get(...)` calls.
Runtime resolution is options-first, so a credential changed through the
options flow has its *active* value missing from the exact-value redaction set.

**Compounding.** This combines with P1-03: `DiagnosticsRecorder.record()`
stores `detail` and `extra` unredacted by design, relying entirely on
export-time redaction. An options-stored Open-Meteo key therefore survives into
the downloadable diagnostics file.

**Fix direction.** Include both `options.get(KEY)` and `entry.data.get(KEY)` for
every credential, matching `redaction.py`'s own stated over-redact philosophy.

**Related gap.** `latitude` / `longitude` for the coordinate-redaction pass are
also read from `entry.data` only. That is correct today because coordinates
exist nowhere else — but the P1-26 reconfigure flow must write coordinates to
`entry.data`, or this silently breaks.

---

### P1-05 — Station entity IDs are not redacted in diagnostics

- **Verdict:** CONFIRMED
- **Location:** `redaction.py` `SENSITIVE_KEY_SUBSTRINGS`; `diagnostics.py`

**Evidence.** `station_temp_entity`, `station_humidity_entity` and
`station_pressure_entity` match none of the substrings in
`SENSITIVE_KEY_SUBSTRINGS`, so values like `sensor.bedroom_temperature` appear
verbatim in diagnostic exports. Not a credential, but household-layout
information in a file intended to be shared.

**Fix direction.** Add `"entity"` to `SENSITIVE_KEY_SUBSTRINGS`. Verified safe:
no other config key in `const.py` contains that substring.

---

## 5. P1 findings — quota and restart state

### P1-06 — No annual credit ceiling for meteoblue

- **Verdict:** CONFIRMED
- **Location:** `const.py`; `coordinator.py` `MeteoblueCoordinator`

**Evidence and arithmetic.** Only per-day (3 scheduled) and per-event
(`METEOBLUE_MAX_BONUS_CALLS_PER_EVENT = 1`) caps exist. Using the module
docstring's own confirmed figures — 8,000 credits per call, 10M/year cap:

```
3 scheduled/day x 8,000 x 365 = 8.76M  (within budget)
4 calls/day     x 8,000 x 365 = 11.68M (exceeds the 10M cap)
```

The local control plane can authorise more than the provider budget allows.

**Fix direction.** `METEOBLUE_ANNUAL_CALL_BUDGET = 10_000_000 // 8_000` (1,250),
reusing the existing `AnnualCallBudget` class already built for Meteonomiqs.
Note the implied policy: 1,095 scheduled calls/year leaves 155 bonus calls/year.
That is a deliberate allocation and should be documented as one.

---

### P1-07 — Meteonomiqs bonus call uses a non-atomic annual-budget pre-check

- **Verdict:** CONFIRMED
- **Location:** `coordinator.py` `MeteonomiqsCoordinator.async_request_bonus_call()`,
  `_async_fetch_nowcast()`

**Evidence.** `async_request_bonus_call()` calls `self._budget.can_call(...)` as
an explicit pre-filter (the code comment says so), and the matching
`record_call()` happens inside `_async_fetch_nowcast()` *after* an awaited HTTP
call. Two paths share `self._budget`: the bonus path and the independent daily
keepalive path. Both can pass the check before either commits.

**Practical likelihood.** Low — the two paths are driven by different
coordinators on different schedules — but non-zero, and the daily bonus cap
does not protect the annual counter.

**Fix direction.** Reserve synchronously via `try_call()` at the caller, with no
`await` between check and reservation; remove `record_call()` from
`_async_fetch_nowcast` / `_async_fetch_hourly_forecast`. The keepalive path
should reserve unconditionally (`record_call()`, not `try_call()`), preserving
the documented "the keepalive must never be skipped" design — losing API access
to inactivity revocation is worse than a slightly tighter annual count.

---

### P1-08 — `_last_successful_call_date` is not persisted

- **Verdict:** CONFIRMED
- **Location:** `coordinator.py`
  `MeteonomiqsCoordinator._async_load_persisted_state_if_needed()`

**Evidence.** That method loads the annual budget state and the bonus tracker
state, but not `_last_successful_call_date`, which is initialised to `None` in
`__init__` and only ever set in memory. The daily gate
(`if self._last_successful_call_date == today: return None`) therefore fails to
recognise an already-serviced day after any same-day restart.

**Fix direction.** New `get/set_meteonomiqs_last_successful_call_date` DB
methods over `schema_meta`, loaded alongside the other persisted state and
saved after every successful fetch in both fetch paths.

---

### P1-09 — meteoblue retries a failed scheduled slot on every poll

- **Verdict:** CONFIRMED
- **Location:** `coordinator.py` `MeteoblueCoordinator._async_update_data()`

**Evidence.** `self._last_scheduled_call_hour = local_dt` executes only on the
success path; the `except` branch records health, records a diagnostic event
and raises `UpdateFailed` without marking the slot attempted. With
`CHECK_INTERVAL = 5 minutes`, a failing scheduled slot re-enters the call path
up to ~12 times within the same hour, each attempt spending a real API credit
against the ceiling P1-06 is about to introduce.

**Fix direction.** Track `_last_scheduled_attempt_at` separately from the
success-only marker and gate retries behind a cooldown
(`METEOBLUE_SCHEDULED_RETRY_COOLDOWN = 15 minutes`, i.e. 3× the poll interval).
Honour `Retry-After` if the client ever exposes it.

---

### P1-10 — meteoblue's dedup fingerprint omits real model-run identity

- **Verdict:** CONFIRMED
- **Location:** `clients/meteoblue.py` `parse_forecast_response()`;
  `fingerprint.py` `fingerprint_points()`

**Evidence.** `run_fingerprint=fingerprint_points(points)` hashes only the
sorted `(variable, valid_at, value)` tuples. `metadata.modelrun_updatetime_utc`
is parsed into `issued_at` but never enters the dedup key.

**Impact.** A genuinely new model run producing identical values — plausible in
a stable pattern — collides with the previous run and is discarded, silently
losing an independent training sample.

**Fix direction.** When `modelrun_updatetime_utc` is actually present, make the
fingerprint `f"{issued_at.isoformat()}|{content_hash}"` — real run identity as
the primary discriminator, content hash retained as a secondary integrity
check. When the field is genuinely absent, `issued_at` falls back to
`datetime.now()`, which changes every call and would defeat deduplication
entirely if embedded — so fall back to content-hash-only in that case.

---

### P1-11 — SRF has the same content-only collision risk, with no code fix available

- **Verdict:** CONFIRMED (documentation-only remediation)
- **Location:** `clients/srf.py`; `coordinator.py` `SrfCoordinator`

**Evidence.** `SrfCoordinator` fingerprints via `fingerprint_points(points)`,
and the `issued_at` it stores is `datetime.now(timezone.utc)`, not a provider
run identifier. Neither response shape this project has confirmed exposes a run
or publication identifier — checked directly against `clients/srf.py`'s field
maps, both of which were themselves verified against live responses.

**Fix direction.** Documentation only. State in `fingerprint_points()`'s
docstring and at the SRF call site that this is content-only *by necessity*,
and note that if SRF ever exposes a real identifier it should become the
primary key the way meteoblue's now is.

---

### P1-12 — Meteonomiqs risk is aggregated with no target-window filter

- **Verdict:** CONFIRMED
- **Location:** `coordinator.py` `ModelBCoordinator._async_update_data_inner()`

**Evidence.** The risk list comprehension takes every item with a non-null
`precip_risk_value` from `last_nowcast.items` and passes `max(risk_values)` into
refinement, with no horizon filter. A high-risk interval hours out can raise a
score that is presented as "storm in ~30 minutes".

**Fix direction.** `METEONOMIQS_NOWCAST_TARGET_WINDOW = 30 minutes`, filtering
to intervals that **overlap** `[now, now+30min)` —
`item.to_ts > now and item.from_ts < cutoff`. An overlap test, not a
"starts after now" test, which would wrongly exclude the currently-active
interval.

---

## 6. P1 findings — CombiPrecip and radar semantics

> Read **IND-13** (section 3) first. All five findings below assume the client
> is fetching the correct file, which it currently may not be.

### P1-13 — Radar readings are consumed with no freshness check

- **Verdict:** CONFIRMED — **FIX-DIVERGENT**
- **Location:** `coordinator.py` `ModelBCoordinator._async_update_data_inner()`;
  `models/model_b.py` `RadarPointReading`, `_radar_signal_probability()`

**Evidence.** `RadarPointReading` has exactly two fields (`label`,
`precip_rate_mmh`) — `valid_at` is captured in `RadarPixelValue` from the HDF5
scan-time metadata and then **dropped** at the construction site in
`coordinator.py`. Home Assistant's `DataUpdateCoordinator` serves the last
successful `.data` indefinitely across failed refreshes, so a stalled radar
feed influences the storm score forever.

**Why the proposed fix diverges.** The reconstruction document sets
`RADAR_FRESHNESS_LIMIT = 20 minutes`, justified as "2× the corrected 10-minute
poll interval from P1-19". **P1-19 is refuted** (below): the documented CPC
update frequency is 5 minutes. The limit should be derived from the correct
cadence — 10–15 minutes is the defensible range, not 20.

**Fix direction.** Add `valid_at: Optional[datetime]` to `RadarPointReading`,
thread it through from `RadarPixelValue`, and exclude any point whose
`valid_at` is missing or older than the limit before any threshold check.
Treat `None` as stale, not fresh.

**Test note.** Four existing tests construct `RadarPointReading` without a
`valid_at` and will need an explicit fresh timestamp, since they would
otherwise silently start testing the stale path.

---

### P1-14 — CombiPrecip's hourly accumulation is treated as an instantaneous rate

- **Verdict:** CONFIRMED — **FIX-DIVERGENT** (the proposed remediation is
  insufficient)
- **Location:** `clients/combiprecip.py` `RadarPixelValue.precip_rate_mmh`;
  `models/model_b.py` `_radar_signal_probability()`;
  `const.py` `RADAR_PRECIP_DETECTION_MMH_THRESHOLD`

**Evidence — now documented, not inferred.** MeteoSwiss's open-data
documentation states the temporal aggregation explicitly:

| Parameter | Long name | Unit | Temporal aggregation |
| --- | --- | --- | --- |
| `CPC` | Combiprecip 60-minute total | mm | Precipitation accumulation over 1 hour |
| `RZC` | PRECIP | mm/h | Instantaneous rain rate |

The field is named `precip_rate_mmh` throughout, and
`RADAR_PRECIP_DETECTION_MMH_THRESHOLD = 0.5` is applied to it as a current
intensity.

**Impact.** The threshold compares a one-hour accumulation against a value
chosen as a rate. For steady rain the two are numerically similar, so this is
not catastrophically wrong — but for convective onset, which is the entire
purpose of Model B, an hour-long accumulation lags badly and mis-states the
timing of arrival. The reported quantity is also simply not what the field name
says it is, which will mislead every future reader.

**Why the proposed fix is insufficient.** The reconstruction document decides
this is "deliberately documentation-only" because a field/column rename is
invasive. That reasoning treats it as a naming issue. It is a units issue: the
threshold's numeric meaning is wrong regardless of what the field is called.

**Fix direction.** Two viable paths, to be decided explicitly:

1. **Keep CPC, fix the semantics.** Rename the field to
   `precip_accum_mm_1h`, re-derive the detection threshold as an accumulation
   figure, and state the lag in the docstring. Correct but changes the model's
   sensitivity and needs re-tuning.
2. **Switch product to RZC.** `RZC` is genuinely an instantaneous rate in mm/h,
   updates every 5 minutes, and is in the same STAC collection — so the
   existing threshold semantics become correct as written. This is arguably the
   better fit for a nowcasting use case, at the cost of losing the gauge
   correction that makes CombiPrecip more accurate for accumulation.

This is a product decision, not a code decision, and is listed in section 10.

---

### P1-15 — Distance-to-lead-time labels are not supported by the data

- **Verdict:** CONFIRMED
- **Location:** `const.py` `UPWIND_POINT_LABELS` comment (`~20 / ~35 / ~60 min
  lead time`), `UPWIND_POINT_PROBABILITY`; `models/model_b.py`
  `_radar_signal_probability()` docstring

**Evidence.** Fixed distances (30/45/70 km) at a fixed 225° bearing are mapped
to specific lead times that were never validated against this project's own
data. Combined with P1-14, a spatial sample of a one-hour accumulation is being
presented as a forecast arrival time.

**Fix direction.** Documentation. Drop the specific minute figures; describe the
upwind points as distance-graded evidence. Keep the graduated probabilities —
they encode "closer is more imminent", which is defensible — but stop attaching
numbers to the timing claim.

---

### P1-16 — CombiPrecip quality information is unused

- **Verdict:** CONFIRMED — **FIX-DIVERGENT** (a better source exists)
- **Location:** `clients/combiprecip.py` `extract_values_at_points()`;
  `models/model_b.py` `_radar_signal_probability()`

**Evidence.** Nothing reads any quality indicator, and nothing gates scoring on
one.

**Why the proposed fix diverges.** The reconstruction document proposes
defensively parsing the optional ODIM `quality1/data1` sub-group. That is a
reasonable fallback, but MeteoSwiss documents the quality code **in the
filename**: `CPCyyjjjHHMMQ_nnnnn.XYZ.h5`, where `Q` is 0–9 and 9 is best. The
filename is always present; the in-file sub-group is optional even within the
spec.

**Fix direction.** Parse `Q` from the asset href during selection (this work is
already required by IND-13), carry it through `RadarPixelValue` and
`RadarPointReading`, and gate on a documented minimum. Optionally also read the
in-file sub-group as corroboration. Retain the reconstruction document's
asymmetry: exclude a *confirmed* low quality value, but do **not** exclude
`None`/unknown — treating unknown as bad risks silently disabling the radar
signal entirely.

---

### P1-17 — STAC asset selection is based only on the `.h5` suffix

- **Verdict:** CONFIRMED, **superseded by IND-13**
- **Assessed severity:** P0 as restated in IND-13

The finding is correct but understates the problem as a future risk ("a
collection change *can* silently switch the product"). It is the present state.
See IND-13 for evidence and the correct fix. **The reconstruction document's
proposed `ValueError`-on-ambiguity fix must not be applied.**

---

### P1-18 — Out-of-grid coordinates are clamped to edge pixels

- **Verdict:** CONFIRMED
- **Location:** `clients/combiprecip.py` `_pixel_indices()`

**Evidence.**

```
col = max(0, min(xsize - 1, col))
row = max(0, min(ysize - 1, row))
```

A sampling point genuinely outside the radar grid silently returns an
unrelated edge pixel's value. The 70 km `far` point makes this plausible near
the border.

**Fix direction.** Return `(None, None, xsize, ysize)` for an out-of-bounds
point and have `extract_values_at_points()` emit
`RadarPixelValue(precip_rate_mmh=None, ...)` — the same shape already used for
the file's own missing-data sentinel. Include an in-bounds corner sanity test to
catch an off-by-one in the new boundary check.

---

### P1-19 — "CombiPrecip is polled faster than its documented cadence"

- **Verdict:** **REFUTED**
- **Assessed severity:** none — no defect

**Evidence.** The audit asserts a 10-minute product cadence and the
reconstruction document duly changes `COMBIPRECIP_POLL_INTERVAL` to
`timedelta(minutes=10)`. MeteoSwiss's open-data documentation states the update
frequency for the CombiPrecip 60-minute total product as **5 minutes** — the
same cadence as PRECIP and PRECIP-SV. The 60-minute figure in the product name
is the accumulation window, not the publication interval; the two appear to
have been conflated.

The current `COMBIPRECIP_POLL_INTERVAL = timedelta(minutes=5)` is correct.

**Action required.** Do not apply this change. It would halve the radar update
rate for no benefit, and it propagates into P1-13's freshness limit. Add a test
pinning the interval to the documented cadence with a comment citing the
source, so this does not get "fixed" again.

---

## 7. P1 findings — input and provider validation

### P1-20 — Station sensor values accept NaN and Infinity

- **Verdict:** CONFIRMED
- **Location:** `coordinator.py` `StationCoordinator._read_float_state()`

**Evidence.**

```
try:
    return float(state.state)
except ValueError:
    return None
```

`float("nan")`, `float("inf")`, `float("-inf")` and `float("Infinity")` all
parse without raising. The value flows straight into `station_observations` and
from there into Model A's EMA and Model B's tendencies, where one bad sample
permanently poisons a bucket.

**Fix direction.** `math.isfinite()` check, treated the same as
`unknown`/`unavailable`. Parametrise the test over
`["nan", "inf", "-inf", "Infinity", "-Infinity", "NaN"]`.

---

### P1-21 — Station measurements are assumed to be in model units

- **Verdict:** CONFIRMED
- **Location:** `coordinator.py` `StationCoordinator._read_float_state()`

**Evidence.** The reader returns `float(state.state)` without inspecting
`unit_of_measurement`. A Fahrenheit or inHg sensor produces numerically
plausible but badly wrong values that the EMA will faithfully learn as provider
bias.

**Fix direction.** New `unit_conversion.py` with pure functions
(`convert_temperature_to_celsius`, `convert_pressure_to_hpa`), no HA imports,
matching the existing `models/` convention. Treat a missing unit as
"already canonical" to preserve behaviour for entities that don't populate it;
return `None` for an unrecognised unit — explicit rejection, not a silent
guess.

---

### P1-22 — Station pressure is not validated as sea-level pressure

- **Verdict:** PARTIAL — real, but the fix as scoped is incomplete
- **Location:** `config_flow.py` station step; `coordinator.py`
  `StationCoordinator`

**Evidence.** The entity selector validates device class only. A station-pressure
sensor at altitude creates a systematic, elevation-dependent offset that Model A
absorbs as forecast bias.

**Why partial.** The proposed fix addresses only the *local* station. See
**IND-12**: the provider side is equally unverified — Open-Meteo requests
`pressure_msl`, meteoblue maps `sealevelpressure`, and SRF's `PRESSURE_HPA` is
undocumented on this point. Reducing the station reading to sea level while one
provider may be publishing station pressure leaves a residual cross-source
offset.

**Fix direction.** `reduce_station_pressure_to_sea_level(station_pressure_hpa,
elevation_m, temperature_c)` using the standard barometric formula, defaulting
to a 15 °C reference when no temperature is available rather than refusing to
compute. Gate on a new `CONF_STATION_PRESSURE_IS_SEA_LEVEL` option defaulting to
`True` so existing installations are unaffected on upgrade. Note that adding
this key requires `async_migrate_entry` — see IND-05. Resolve IND-12 in the same
pass.

---

### P1-23 — No shared finite/range validation before values reach storage

- **Verdict:** CONFIRMED
- **Location:** all three provider coordinators, immediately before
  `insert_forecast_snapshots_bulk`

**Evidence.** Parsers perform structural checks only. No provider-independent
validation layer exists between parsing and persistence, so malformed provider
data becomes durable training data.

**Fix direction.** New `provider_validation.py` rejecting non-finite values and
values outside generous physical bounds per variable, applied to the row tuples
every coordinator already builds. Replace a bad value with `None` and keep the
row — consistent with how a provider's own "no data" is already handled — and
return a rejection count for diagnostics.

---

### P1-24 — meteoblue array-length mismatch is silently truncated

- **Verdict:** CONFIRMED
- **Location:** `clients/meteoblue.py` `parse_forecast_response()`

**Evidence.** `for t_str, value in zip(times, values)` truncates to the shorter
array with no trace. Open-Meteo already has `array_length_mismatches` tracking
from the v0.1.19 fix; meteoblue has no equivalent.

**Fix direction.** Add `array_length_mismatches: tuple[str, ...] = ()` to
`ParsedMeteoblueForecast`, populated the same way Open-Meteo's is, with a
warning and a diagnostics event when non-empty.

---

### P1-25 — `.replace(tzinfo=UTC)` mishandles already-aware input

- **Verdict:** CONFIRMED (low current risk)
- **Location:** `clients/meteoblue.py` (parsing `modelrun_updatetime_utc`);
  `clients/open_meteo.py` (parsing the hourly `time` array)

**Evidence.** `datetime.fromisoformat(s).replace(tzinfo=timezone.utc)` is only
correct for naive input. On aware input it relabels without converting, shifting
the instant by the offset.

**Current exposure.** Limited. meteoblue's hourly times are parsed with
`strptime("%Y-%m-%d %H:%M")`, which cannot produce an aware value, and both
clients now explicitly request UTC (`&tz=UTC` / `&timezone=UTC`). The remaining
exposure is meteoblue's `modelrun_updatetime_utc`, where a `Z` suffix is handled
correctly by chance but a numeric offset would not be. Defensive, not urgent.

**Fix direction.** Branch on `parsed.tzinfo is not None`: `.astimezone(utc)` for
aware, `.replace(tzinfo=utc)` for naive.

---

## 8. P1 findings — configuration flow

### P1-26 — No reconfigure flow for coordinates and elevation

- **Verdict:** CONFIRMED
- **Location:** `config_flow.py`

**Evidence.** Latitude and longitude are captured in `async_step_user` and never
exposed again. A relocated installation has no supported path except
remove-and-re-add, discarding all learned `bucket_stats` and accumulated
history.

**Fix direction.** `async_step_reconfigure` reusing `async_step_user`'s schema,
validators and elevation lookup, updating the existing entry in place via
`async_update_entry` + `async_reload`.

**Dependency.** Must write coordinates to `entry.data`, or `diagnostics.py`'s
coordinate redaction silently stops matching (see P1-04). Also needs new
translation keys — `translations/en.json` currently has only `user`, `station`,
`credentials`, `reauth_confirm`.

---

### P1-27 — Coordinates and elevation accept non-finite and out-of-range values

- **Verdict:** CONFIRMED
- **Location:** `config_flow.py` `async_step_user()` schema

**Evidence.** `vol.Coerce(float)` accepts `"nan"` and `"inf"` and applies no
geographic range check at all.

**Fix direction.** A `_finite_float` validator composed with `vol.Range` into
`_LATITUDE_VALIDATOR` (−90..90), `_LONGITUDE_VALIDATOR` (−180..180) and
`_ELEVATION_VALIDATOR` (−430 m to 9,000 m), applied across setup, options and
the new reconfigure flow.

**Blocked by:** INFRA-02. Without that fix these validators silently no-op under
test while working correctly in production — the worst possible failure mode for
a validation change.

---

### P1-28 — `purge_days` accepts negative values

- **Verdict:** CONFIRMED
- **Location:** `config_flow.py` options schema; `coordinator.py`
  `RetentionCoordinator._async_update_data()`

**Evidence.** The schema uses bare `vol.Coerce(int)`. The coordinator treats
`purge_days <= 0` as "keep forever", so a negative value silently becomes a
second spelling of forever.

**Fix direction.** `vol.All(vol.Coerce(int), vol.Range(min=0))`. See also
**IND-06** — the default value itself is the larger problem.

---

### P1-29 — The optional Open-Meteo key cannot be cleared

- **Verdict:** CONFIRMED
- **Location:** `config_flow.py` `SwissWeatherFusionOptionsFlow.async_step_init()`

**Evidence.** The "blank means keep existing" backfill loop covers all five
credentials uniformly. That is correct and necessary for the four required
credentials, since a masked password field cannot be pre-filled — but it makes
the one genuinely optional key impossible to remove, so a user cannot return to
the free tier.

**Fix direction.** A `CONF_CLEAR_OPEN_METEO_API_KEY` checkbox following the
tri-state pattern already established for `CONF_CLEAR_ELEVATION_OVERRIDE`,
applied after the backfill loop.

---

### P1-30 — Credential fields do not enforce non-empty values

- **Verdict:** CONFIRMED
- **Location:** `config_flow.py` credentials, reauth and options schemas

**Evidence.** `vol.Required` only requires the key to be present in the
submitted dict, not that its value be non-empty. An empty secret saves cleanly
and fails later at request time.

**Fix direction.** A `_non_empty_str` validator (strip, reject if empty)
composed with the existing password selector via `vol.All`.

**Blocked by:** INFRA-02.

---

## 9. P2 findings

### P2-01 — Schema initialisation trusts `schema_meta` over actual table shape

- **Verdict:** CONFIRMED — **FIX-DIVERGENT** (one part of the proposed fix
  addresses a bug that does not exist)
- **Location:** `storage/db.py` `_ensure_schema()`

**Evidence.** When no `schema_version` row is found, the code treats the
database as brand new and immediately runs `_PENDING_INDEX_SQL`, which creates
a partial index on `reconciliation_status`. Because `_SCHEMA_SQL` uses
`CREATE TABLE IF NOT EXISTS`, a v1-shaped `forecast_snapshots` that survived
while its `schema_meta` row was lost is left untouched — and the index creation
then fails with "no such column" on exactly the recovery path it exists to
support.

**Correction to the proposed fix.** The reconstruction document also reports
that the final `UPDATE schema_meta SET value=? WHERE key='schema_version'`
silently updates zero rows. In v0.1.23 that is **not reachable**: the
no-row branch returns early after an explicit `INSERT`. The `UPDATE` only runs
when a row already exists. That bug would be *introduced* by their own change to
the branch structure — worth fixing pre-emptively as an upsert, but it should
not be described as a pre-existing defect.

**Fix direction.** Base migration detection on `PRAGMA table_info` against
`forecast_snapshots`, using metadata absence only as a secondary signal. Write
the version with `INSERT ... ON CONFLICT DO UPDATE`.

**Test.** Hand-build a raw SQLite file with data tables and no `schema_meta`
row; assert migration runs and the `schema_version` row is created.

---

### P2-02 — Corrupted persisted state can prevent coordinator startup

- **Verdict:** CONFIRMED
- **Location:** `storage/db.py` `get_annual_call_budget_state()`,
  `get_bonus_call_tracker_state()`, `get_model_b_previous_probability()`

**Evidence.** These call `json.loads(raw)` and `float(raw)` directly against
whatever text is in `schema_meta`, with no handling for a truncated write or
manual tampering. A raised exception propagates out of
`_async_load_persisted_state_if_needed()` and prevents the owning coordinator
from starting.

**Fix direction.** A `_safe_parse_meta(key, parse)` helper catching
`ValueError` / `TypeError` / `json.JSONDecodeError`, logging a warning, clearing
the corrupt value so it does not fail on every subsequent restart, and returning
`None` — the same shape as "never persisted", which every caller already handles.

---

### P2-03 — Learning read-modify-write is only per-call locked

- **Verdict:** CONFIRMED
- **Location:** `coordinator.py` `ModelALearningCoordinator._reconcile()`;
  `storage/db.py` `get_bucket_stats()` / `upsert_bucket_stats()`

**Evidence.** Each DB method takes `self._lock` individually, but the logical
read-modify-write spans two separate locked calls. A manual refresh overlapping
a scheduled cycle can lose an EMA update.

**Note.** P0-01's `apply_reconciliation_batch` fix closes most of this by
collapsing the write side into one transaction. The read side still needs the
coordinator-level guard below.

---

### P2-04 — Learning reads and retention deletes are not coordinated

- **Verdict:** CONFIRMED
- **Location:** `coordinator.py` `ModelALearningCoordinator`,
  `RetentionCoordinator`; `storage/db.py` `purge_older_than()`

**Evidence.** `_reconcile()` performs several sequential reads
(`get_pending_forecast_snapshots`, `get_station_observations_between`,
`get_bucket_stats` per row) while `RetentionCoordinator` can independently
delete rows between them. Per-statement locking does not make the multi-step
read snapshot-consistent.

**Mitigating factor.** `purge_older_than()` already excludes rows with
`reconciliation_status = 'pending'`, so forecast rows in flight are protected;
the exposure is to `station_observations` disappearing mid-batch.

**Fix direction (P2-03 + P2-04).** One shared `asyncio.Lock()` constructed in
`__init__.py` on the event loop and injected into both coordinators. Both
constructors should fall back to a private lock when none is supplied so each
stays independently constructible for tests.

---

### P2-05 — Storm predictions are not independently reconstructable

- **Verdict:** CONFIRMED / evidence corrected
- **Location:** `coordinator.py` `ModelBCoordinator._async_update_data_inner()`;
  `storage/db.py` `insert_storm_prediction()`

**Correction.** The reconstruction document says the persisted blob captures
3 of 9 tendency delta fields. It captures **2**: `delta_pressure_30min` and
`delta_humidity_30min`. Radar points are stored as
`{label: precip_rate_mmh}` only — no `valid_at`, no quality, no coordinates.

**Impact.** What `score_v0_graduated` actually saw for a historical prediction
cannot be reconstructed, which undermines the table's only stated purpose
(training data for Model B v1).

**Fix direction.** Persist all 9 delta fields and each radar point's full detail
(`label`, value, `valid_at.isoformat()`, quality). Note the latent
`NameError`: `got_meteonomiqs` and `risk_values` are currently defined only
inside the `if decision.should_trigger:` block, so referencing them
unconditionally in a richer payload crashes in the common non-triggering case.
Initialise both before the trigger check.

---

### P2-06 / P2-07 — Entity naming implies a calibrated probability; combination rule undocumented

- **Verdict:** CONFIRMED (both)
- **Location:** `sensor.py` `StormOnsetProbabilitySensor`;
  `models/model_b.py` `refine_with_meteonomiqs()`

**Evidence.** The friendly name is "Storm onset probability" and the unit is
`%`, implying statistical validation the v0 heuristic does not have — the score
is `max()` of two hand-authored signals, linearly averaged with
`meteonomiqs_risk / 9`. No calibration layer exists and the 50/50 weighting has
no stated basis.

**Fix direction.** Leave the entity key and `unique_id`
(`storm_onset_probability`) **unchanged** — renaming orphans every existing
installation's entity IDs, automations and history, which is worse than the
labelling problem. Change what is user-visible: friendly name to "Storm onset
risk score", plus an `extra_state_attributes` property returning
`{"is_calibrated_probability": False, "methodology": "..."}` so the disclosure
reaches the UI and API rather than living in a source comment. Document the
weighting honestly in the docstring.

---

### P2-08 — Production code never populates `storm_events`

- **Verdict:** CONFIRMED
- **Location:** `storage/db.py` `insert_storm_event()`

**Evidence.** Verified by grep: `insert_storm_event` has **zero** callers
outside its own definition. The ground-truth table that the entire Model B v1
plan depends on can never fill from runtime operation.

**Fix direction.** A `StormEventReconciliationCoordinator` running every ~30
minutes: for any `storm_predictions` row whose follow-up window has fully
elapsed and whose probability exceeded the crossing threshold, fetch the real
station and radar observations across that window and test them against Model
B's own existing v0 thresholds — reusing the live scorer's definition of a storm
signature rather than inventing a second one. Promote confirmed predictions to
`storm_events` with *observed* peak values. Mark every checked prediction
reconciled so it is never re-checked.

Requires an additive `reconciled INTEGER NOT NULL DEFAULT 0` column on
`storm_predictions` (an ALTER-if-missing check is sufficient; unlike the
`reconciliation_status` migration there is no data decision to make), plus
`get_unreconciled_storm_predictions`, `mark_storm_predictions_reconciled` and
`get_radar_observations_between`.

**Caveat to carry forward.** The confirmation thresholds reuse v0 heuristics,
not an independently validated definition of "a real storm". Appropriate for
now; revisit once real events accumulate.

**See also IND-10** — this is one half of a wider problem.

---

### P2-09 — Future-dated station samples are not rejected

- **Verdict:** CONFIRMED
- **Location:** `coordinator.py` `ModelBCoordinator._async_update_data_inner()`;
  `storage/db.py` `get_station_observations_since()`

**Evidence.** The query bounds only the lower time edge. Nothing filters a
sample with `ts > now` (clock skew, a restored or replayed state), and the
sample list is passed straight into `compute_tendency_features`.

**Fix direction.** Filter `datetime.fromisoformat(r["ts"]) <= now` when building
the `StationSample` list, and record future-dated samples as a data-quality
fault rather than dropping them silently.

---

### P2-10 — Weather condition collapses every non-rain state to sunny

- **Verdict:** CONFIRMED
- **Location:** four call sites — `weather.py` `condition`;
  `coordinator.py` hourly forecast construction;
  `models/model_a.py` daily and twice-daily aggregation

**Evidence.** All four use `"rainy" if precip > threshold else "sunny"`. Snow,
cloud, overcast and fog all render as sunny in the UI and in automations.

**Fix direction.** A shared `model_a.derive_condition(precip, temperature,
humidity, precip_threshold=0.1)`. Two behavioural details must be preserved
rather than unified: the hourly sites use a 0.1 mm threshold and the
daily/twice-daily sites use 0.5 mm, and the two groups differ deliberately in
their `None` handling (`total_precip or 0` vs. raw `precip`). A humidity-based
"cloudy" inference is a plausible but uncalibrated proxy and must be disclosed
as such.

---

### P2-11 — "Active sources" counts never-successful sources as active

- **Verdict:** CONFIRMED — **scope understated**, see IND-03
- **Location:** `sensor.py` `ActiveSourcesSensor.native_value`

**Evidence.** `health.consecutive_failures == 0` is the initial state before any
poll, so a source that has never succeeded counts as active.

**Fix direction.** Also require `health.last_success_time is not None`
(only ever set inside `record_success()`). **Apply in all three places** — see
IND-03.

---

### P2-12 — Setup does not distinguish auth failure from transient failure

- **Verdict:** CONFIRMED
- **Location:** `__init__.py` source-coordinator first-refresh loop

**Evidence.** `asyncio.gather(..., return_exceptions=True)` followed by a loop
that logs and continues for every failure kind. Since
`ConfigEntryAuthFailed` only triggers HA's reauth flow when raised *out of*
`async_setup_entry`, a bad credential entered during initial setup produces a
successful-looking setup with no reauth prompt.

**Fix direction.** Check `isinstance(result, ConfigEntryAuthFailed)` explicitly.
This raise happens before the file's one pre-existing cleanup `try/except`, so
it must explicitly shut down every already-constructed coordinator and close the
DB before re-raising, or it leaks the connection and all ten coordinators. Every
other exception kind keeps degrading gracefully.

**Blocked by:** P1-01 and INFRA-01.

---

### P2-13 — "No clear unique-id strategy"

- **Verdict:** PARTIAL — the entity-identity half is **REFUTED**; the
  duplicate-entry half is CONFIRMED
- **Location:** `sensor.py` `_BaseSensor.__init__`, `weather.py`,
  `binary_sensor.py`, `config_flow.py` `async_step_user()`

**Refuted half.** Every entity class already sets a durable,
`entry.entry_id`-anchored `unique_id`:

```
sensor.py:92        self._attr_unique_id = f"{entry.entry_id}_{unique_suffix}"
weather.py:58       self._attr_unique_id = f"{entry.entry_id}_weather"
binary_sensor.py:32 self._attr_unique_id = f"{entry.entry_id}_degraded"
```

`device.py` `build_device_info()` anchors the device on the same identifier.
Entity identity is stable across reloads. The audit's claim does not hold.

**Confirmed half.** `async_step_user()` never calls `async_set_unique_id()` or
`_abort_if_unique_id_configured()`. Nothing prevents adding the same physical
location twice as two independent entries, each spinning up a full set of ten
coordinators and independently consuming provider quota against the same
account limits.

**Fix direction.** `await self.async_set_unique_id(f"{round(lat,4)}_{round(lon,4)}")`
followed by `self._abort_if_unique_id_configured()`, immediately after capturing
coordinates. Four decimal places (~11 m) so floating-point noise between two
submissions of the same coordinates cannot defeat the check. Needs a new
`abort` translation key.

---

## 10. P3 findings

### P3-01 — `last_learning_b` sensor is permanently `None` and looks broken

- **Verdict:** CONFIRMED
- **Location:** `sensor.py` `LastLearningBSensor.native_value`

**Fix direction.** Keep the entity key and `unique_id` (same orphaning concern
as P2-06). Change the friendly name to make non-applicability explicit, add
`extra_state_attributes` stating `{"not_applicable": True, "reason": "..."}`,
and set `_attr_entity_registry_enabled_default = False` so new installations do
not show it at all.

---

### P3-02 — Forecast accuracy sensor is a permanent stub

- **Verdict:** CONFIRMED
- **Location:** `sensor.py` `ForecastAccuracySensor.native_value`

**Evidence.** Returns `None` unconditionally, with a docstring saying a rolling
MAE is "best done once real data exists" — while `bucket_stats.ema_abs_error` has
been real, durable, continuously-updated error data all along.

**Fix direction.** Compute a sample-count-weighted average of `ema_abs_error`
across temperature buckets from `get_all_bucket_stats()`. Return `None` only
when nothing has been learned yet. Rename from "Forecast accuracy (7d MAE)" — a
methodology never implemented — to something describing what is actually
computed, with the methodology in `extra_state_attributes`.

**Depends on IND-01.** `ema_abs_error` is only meaningful as a headline accuracy
figure once the weight/error scale question is settled.

---

### P3-03 — Manifest contains template placeholder metadata

- **Verdict:** CONFIRMED
- **Location:** `manifest.json`

**Evidence.** `"codeowners": ["@your-github-username"]` and matching
`documentation` / `issue_tracker` URLs — HACS's literal known-bad template
defaults.

**Fix direction.** Replace with non-template values. These still need real
values from whoever owns the repository, but they will no longer match HACS's
"never configured" detection pattern.

---

## 11. Independent findings

IND-13 and IND-01 are documented in section 3 with the other P0s.

### IND-02 — A single missing station reading zeroes the entire Model B score

- **Severity:** P1
- **Location:** `coordinator.py` `StationCoordinator._async_update_data()`;
  `models/model_b.py` `compute_tendency_features()`

**Evidence.** `StationCoordinator` inserts a row every 5 minutes
unconditionally, including when all three readings are `None` (sensor
`unavailable`, or rejected by `_read_float_state`). In
`compute_tendency_features`, `latest = samples[-1]`, and every `delta()` returns
`None` when `latest_val is None`. `score_v0()` then returns `0.0` because both
its inputs are `None`.

**Impact.** One 5-minute dropout on any one of the three station sensors blanks
all nine tendency features and drops the storm score to zero, discarding 55
minutes of good data — during exactly the conditions in which sensor dropouts
are most likely. The radar half of `score_v0_graduated` still contributes, so
this presents as an unexplained score collapse rather than an outage.

**Fix direction.** Two independent changes, both worth making:

1. In `compute_tendency_features`, resolve `latest` per measurement — the most
   recent sample with a non-`None` value for that attribute — rather than
   taking the last row wholesale. `_nearest_sample_at_or_before` needs the same
   treatment for the window endpoint.
2. In `StationCoordinator`, skip the insert when all three values are `None`.
   This also reduces the row volume feeding IND-06.

**Test.** A sample series with good data throughout and an all-`None` final row;
assert the 30-minute deltas are still computed.

---

### IND-03 — The never-succeeded health bug exists in three places, not one

- **Severity:** P1
- **Location:** `sensor.py` `ActiveSourcesSensor.native_value` (audited as
  P2-11), `sensor.py` `StatusSensor.native_value`,
  `binary_sensor.py` `DegradedBinarySensor.is_on`

**Evidence.** All three derive health solely from `consecutive_failures`.
`StatusSensor` reports `"Active"` when no source has a non-zero failure count —
true on a cold start where nothing has succeeded. `DegradedBinarySensor.is_on`
returns `any(health.consecutive_failures > 0 ...)`, so it reads "not degraded"
in the same state. Both are the entities users and automations actually watch.

**Fix direction.** Introduce one shared helper (e.g.
`_is_source_healthy(health)`) requiring both `consecutive_failures == 0` and
`last_success_time is not None`, and route all three call sites through it.
Fixing only the site P2-11 names leaves the two headline entities wrong.

---

### IND-04 — P0-02's spurious re-trigger is the common case, not an edge case

- **Severity:** P1 (evidence supporting P0-02's priority; no separate fix)
- **Location:** `const.py`; `models/model_b.py` `refine_with_meteonomiqs()`

**Evidence.** With `V0_TRIGGER_PROBABILITY = 0.65`,
`STORM_PREDICTION_UPPER_CROSSING_THRESHOLD = 0.5`, and refinement computed as
`(base + risk/9) / 2`:

| Meteonomiqs risk | Refined value stored | Below threshold? |
| --- | --- | --- |
| 0 | 0.325 | yes |
| 1 | 0.381 | yes |
| 2 | 0.436 | yes |
| 3 | 0.492 | yes |

Risk values of 0–1 are ordinary weather. So for any sustained elevated signal,
the stored previous value lands below threshold nearly every time and the next
cycle's unrefined base score reads as a fresh upward crossing — every
`MODEL_B_SCORING_INTERVAL` (5 minutes).

**Impact.** Not a quota problem — the daily bonus caps hold. It is a data
integrity problem: `storm_predictions` accumulates duplicate pseudo-events for
one storm, which is the exact table Model B v1 is meant to train on, and the
trigger log stops being a record of distinct storms.

**Action.** No separate fix; this raises P0-02 to the highest-value item in the
P0 set and should be cited in its regression test.

---

### IND-05 — No `async_remove_entry` and no `async_migrate_entry`

- **Severity:** P1
- **Location:** `__init__.py`

**Evidence.** Verified by grep: neither handler exists.

**Impact — removal.** The database lives at
`.storage/{DOMAIN}_{entry_id}_{DB_FILENAME}`. Removing the integration leaves
that file plus its WAL and shared-memory sidecars orphaned permanently, holding
the full station observation history and (implicitly) the configured location.
Re-adding the integration creates a new `entry_id` and therefore a new database,
so the old one is unreachable as well as undeleted.

**Impact — migration.** `SwissWeatherFusionConfigFlow.VERSION = 1` with no
migration handler. The P1-22 remediation adds `CONF_STATION_PRESSURE_IS_SEA_LEVEL`
to the entry, so a migration path is needed as part of this work, not later.

**Fix direction.** Implement `async_remove_entry` to delete the database and its
sidecars via an executor job. Implement `async_migrate_entry` before any change
to the entry schema, and bump `VERSION` in the same commit.

---

### IND-06 — Retention defaults to "keep forever" and is never surfaced at setup

- **Severity:** P1
- **Location:** `config_flow.py` `async_step_credentials()`; `const.py`
  `DEFAULT_PURGE_DAYS`; `storage/db.py` `purge_older_than()`

**Evidence.** `async_step_credentials` sets
`self._data[CONF_PURGE_DAYS] = DEFAULT_PURGE_DAYS`, and `DEFAULT_PURGE_DAYS = 0`,
documented as "keep forever". The setup flow never asks. The field appears only
in the options flow, which a user has no reason to open.

**Volume.** Each changed Open-Meteo run inserts roughly 5 variables × up to 168
hours ≈ 840 rows per model, across three models, plus SRF and meteoblue. On the
Raspberry Pi and HA-Green class hardware this integration targets, that is
unbounded SQLite growth on flash storage by default.

**Compounding.** `purge_older_than()` never issues `VACUUM`, so even after a
purge the file does not shrink; and nothing reports database size or row counts
anywhere, so growth is invisible until the disk fills.

**Fix direction.** Ask for the retention window during setup with a bounded,
non-zero default (90 days is a reasonable starting point given the 14-day
`INITIAL_LOOKBACK` and the 168-hour forecast horizon). Keep `0` as an explicit
opt-in to unlimited. Add row counts and file size to diagnostics, and consider
an occasional `VACUUM` after a large purge.

---

### IND-07 — SRF's OAuth token and geolocation ID are memory-only

- **Severity:** P2
- **Location:** `clients/srf.py` `_async_ensure_token()`,
  `_async_ensure_geolocation_id()`

**Evidence.** `self._token` and `self._geolocation_id` are plain instance
attributes on `SrfClient`, which is constructed fresh inside `SrfCoordinator`
on every setup. Every reload — and every options change triggers a reload —
re-runs the OAuth token exchange and the geolocation lookup.

**Impact.** Two extra quota-consuming SRG-SSR calls per reload, on the one
source with a rotating credential. This is the same "state resets on restart"
class as the L-07/L-08 fixes, which were applied to meteoblue and Meteonomiqs
but never to SRF.

**Fix direction.** Persist the geolocation ID (keyed by rounded coordinates, so
it invalidates on relocation) via `schema_meta`, alongside the existing durable
runtime state. The bearer token can also be persisted with its expiry, though
the geolocation ID is the higher-value half since it never changes for a fixed
location.

---

### IND-08 — No `device_class`, `state_class` or `entity_category` on any entity

- **Severity:** P1
- **Location:** `sensor.py` (all 30+ entities), `binary_sensor.py`

**Evidence.** Verified by grep across the integration: the only `device_class`
occurrences are `EntitySelectorConfig` arguments in `config_flow.py`. No entity
sets `device_class`, `state_class` or `entity_category`.

**Four distinct consequences:**

1. **Timestamp sensors are malformed.** `LastSuccessSensor`,
   `LastLearningASensor` and `LastLearningBSensor` return `datetime` objects.
   Home Assistant expects `SensorDeviceClass.TIMESTAMP` for datetime states;
   without it these do not render or store as timestamps.
2. **No long-term statistics anywhere.** Without
   `state_class = SensorStateClass.MEASUREMENT`, none of the numeric telemetry —
   storm onset score, expert weights, forecast accuracy, poll durations,
   consecutive failures — is recorded into HA's statistics tables. `sensor.py`'s
   own module docstring states the build request explicitly required "sensors
   showing learning progress and forecast accuracy". Those sensors exist but
   cannot be charted over time, which is the form in which learning progress is
   actually legible.
3. **Dashboard clutter.** Roughly 29 of the 36 entities are pure diagnostics
   (per-source last success, poll duration, data error, auth error, consecutive
   failures) with no `entity_category = EntityCategory.DIAGNOSTIC`, so all of
   them land on the primary device card alongside the weather entity.
4. **`DegradedBinarySensor`** has no `BinarySensorDeviceClass.PROBLEM`, so it
   renders as generic on/off rather than OK/Problem.

**Fix direction.** Add the metadata. This is mechanical, low-risk, and
disproportionately improves the product surface. Item 2 in particular converts
existing-but-unusable telemetry into the feature that was actually requested.

---

### IND-09 — No `available` property on any entity

- **Severity:** P2
- **Location:** `sensor.py`, `binary_sensor.py`

**Evidence.** Only `SwissWeatherFusionWeather` and `ExpertWeightSensor` are
`CoordinatorEntity` subclasses (and therefore inherit availability from
`last_update_success`). Every other sensor is a plain `SensorEntity` reading
live runtime objects in a property, with no `available` override.

**Impact.** `StormOnsetProbabilitySensor` keeps publishing
`round(current_probability * 100, 1)` indefinitely after `ModelBCoordinator`
stops updating — the value an automation consumes to close blinds. This is the
same stale-data class as P1-13, but at the entity boundary, where it is visible
to users and automations rather than internal to the model.

**Fix direction.** An `available` property on `_BaseSensor` derived from the
backing coordinator's `last_update_success` and staleness, overridden where a
sensor is deliberately meaningful while its source is down (the health/error
sensors are exactly that case and should stay available).

---

### IND-10 — Model B's durable output has no read path in either direction

- **Severity:** P2
- **Location:** `storage/db.py`; `coordinator.py`

**Evidence.** Verified by grep for production callers:

| Method | Production callers |
| --- | --- |
| `insert_storm_event` | 0 (P2-08) |
| `get_all_storm_events` | 0 |
| `get_storm_predictions_since` | 0 |
| `get_latest_radar_observation` | 0 |
| `get_latest_station_observation` | 0 |
| `get_reconciliation_watermark` / `set_...` | 0 (dead since v0.1.23) |
| `get_forecast_values_for_valid_at` | 0 (comments only) |

So `storm_predictions` is written and never read; `radar_observations` is
written and never read; `storm_events` is neither written nor read. Three
tables, one of them on the 5-minute radar path, exist solely to be purged.

**Impact.** The v0 → v1 story ("accumulate predictions and events, then train")
has no pipe at either end. P2-08 fixes the write side of `storm_events`; nothing
addresses the read side or the two orphaned tables.

**Fix direction.** Decide per table rather than in bulk:

- `storm_predictions` — becomes readable via the P2-08 reconciliation
  coordinator. Keep.
- `storm_events` — same. Keep, and expose a count as a sensor so the training
  set's growth is visible.
- `radar_observations` — either give it a consumer (radar history is genuinely
  useful for the P2-08 confirmation logic, which needs
  `get_radar_observations_between` anyway) or stop writing it. The P2-08 design
  already implies the former.
- `get_reconciliation_watermark` / `set_reconciliation_watermark` — dead since
  the v0.1.23 status-column redesign. Delete, with a note in the migration
  docstring.
- `get_forecast_values_for_valid_at` — retained deliberately as a test
  convenience per its own docstring. Leave, but say so in the docstring rather
  than leaving it looking like production code.

---

### IND-11 — No repairs / issue-registry integration and no service actions

- **Severity:** P2
- **Location:** integration-wide

**Evidence.** `async_create_issue` and `issue_registry` appear nowhere. No
`services.yaml` and no `hass.services.async_register` call exists.

**Impact.** Several conditions are known to the integration but cannot reach the
user except as a log line: annual quota exhausted, a credential revoked (once
P1-01 lands, HA's reauth covers the auth case but not the quota case), the
unverified CombiPrecip HDF5 layout, a database that has grown past a sensible
size. Equally, there is no supported way to intervene — no way to force a
learning run, reset a poisoned bucket, or export `bucket_stats` for inspection
without opening the SQLite file by hand.

**Fix direction.** Raise repair issues for quota exhaustion and for a persistent
provider auth failure. Add a small set of service actions —
`force_learning_run`, `reset_learning` (with a confirmation field), and
`export_bucket_stats` — which also make several of the findings above testable
by hand on a live install.

---

### IND-12 — Cross-source pressure semantics are unverified

- **Severity:** P3
- **Location:** `clients/open_meteo.py` `HOURLY_VARIABLES`;
  `clients/meteoblue.py` `_FIELD_MAP`; `clients/srf.py`
  `_HOURLY_SIMPLE_FIELD_MAP`

**Evidence.** Open-Meteo requests `pressure_msl` and meteoblue maps
`sealevelpressure` — both explicitly mean-sea-level. SRF's `PRESSURE_HPA` is
mapped to the same internal `pressure` variable, and nothing in this project's
notes establishes whether SRF publishes MSL or station pressure.

**Impact.** If SRF publishes station pressure, its `bucket_stats` bias silently
absorbs a fixed elevation offset. The EMA will learn it and the blend will
produce correct numbers, so nothing breaks visibly — but SRF's learned bias
becomes physically meaningless as a diagnostic, and P1-22's station-side fix
addresses only half the problem.

**Fix direction.** Verify against a live SRF response (the existing
`srf_probe.py` already prints raw responses and can answer this directly).
Document the finding either way. If SRF turns out to publish station pressure,
apply the same sea-level reduction using the response's own station elevation.

---

## 12. Test-infrastructure blockers

These are not defects in the shipped integration, but they block remediation and
must be fixed first.

### INFRA-01 — `tests/conftest.py` has no `homeassistant.exceptions` stub

**Evidence.** The stub module list in `_install_homeassistant_stubs()` covers
`core`, `config_entries`, `const`, `data_entry_flow`, `helpers.selector`,
`helpers.aiohttp_client`, `helpers.update_coordinator`, `util.dt`,
`helpers.entity_platform` and three `components.*` modules. There is no
`homeassistant.exceptions`.

**Impact.** P1-01, P2-12 and P1-03's `from None` changes all require importing
`ConfigEntryAuthFailed`. With no stub, importing `coordinator.py` fails at
collection time and the entire suite errors out.

**Fix.** Add `ConfigEntryAuthFailed` and `ConfigEntryNotReady` stubs (both
subclassing `Exception`) to the fake `homeassistant.exceptions` module.

---

### INFRA-02 — `tests/conftest.py` unconditionally shadows the real `voluptuous`

**Evidence.** The `voluptuous` stub is created inside
`_install_homeassistant_stubs()` and provides only `Schema`, `Required`,
`Optional` and `Coerce` — all as `lambda *a, **kw: None`. It does not provide
`All`, `Range`, `Invalid` or `In`. Unlike every other module stubbed there,
`voluptuous` is an ordinary PyPI package with **no Home Assistant dependency**
and can simply be installed.

**Impact.** Every validator introduced by P1-27, P1-28 and P1-30 would silently
evaluate to `None` under test while working correctly in production — tests
would pass against validators that were never actually exercised. For a set of
findings that are entirely about validation, this is the worst available failure
mode.

**Fix.** Try `import voluptuous` for real first and fall back to the minimal
stub only if that genuinely fails — the same pattern already used correctly for
`homeassistant` two lines above in the same file. Add `voluptuous` to
`requirements-test.txt`.

---

### INFRA-03 — The files carrying 40 of 63 findings have no executable coverage

**Evidence.** See section 2. `coordinator.py`, `__init__.py`, `config_flow.py`,
`sensor.py`, `weather.py` and `binary_sensor.py` are covered only by
`test_syntax.py`.

**Fix direction.** This does not require a full
`pytest-homeassistant-custom-component` adoption. The existing stub approach is
sufficient to drive most of the affected code paths directly — the
reconstruction document's own test list demonstrates that (calling the real
`async_unload_entry` with a fake runtime, the real `async_step_reauth_confirm`
with a fake entry, the real `_reconcile()` against a temp-file database). What
is needed is to extend the stubs enough to construct coordinators and flows,
then test the real methods rather than mocks of them.

---

## 13. Remediation sequence

Ordered by dependency, not by severity. Each stage should end with a full suite
run, not just the final stage — the reconstruction document reports that running
the suite after every individual change is what caught all seven of its
self-inflicted bugs.

**Stage 0 — unblock the test harness.** INFRA-01, INFRA-02. Nothing downstream
can be verified without these.

**Stage 1 — P0 correctness.** P0-01, P0-02 (cite IND-04 in the test), P0-03,
P0-04. These are independent of each other and of everything below.

**Stage 2 — radar product identity.** IND-13 first, then P1-16 (quality code
comes from the filename parsing IND-13 introduces), then P1-18, then P1-13 with
a freshness limit derived from the correct 5-minute cadence. P1-19 is a
deliberate no-change with a pinning test. P1-14/P1-15 depend on the product
decision in section 14. Nothing in this stage is meaningful before IND-13.

**Stage 3 — the model itself.** IND-01, then IND-02. IND-01 changes what
`ema_abs_error` means as a headline figure, so P3-02 must follow it, not precede
it.

**Stage 4 — auth and credential propagation.** P1-01, P1-02, P1-03, P1-04,
P1-05, P2-12. P2-12 depends on P1-01.

**Stage 5 — quota and restart state.** P1-06, P1-07, P1-08, P1-09, P1-10, P1-11,
P1-12, IND-07.

**Stage 6 — validation.** P1-20, P1-21, P1-22 (with IND-12), P1-23, P1-24,
P1-25, P2-02, P2-09.

**Stage 7 — lifecycle and configuration.** IND-05 (migration handler must land
before P1-22's new config key), P1-26, P1-27, P1-28, P1-29, P1-30, P2-13's
confirmed half, IND-06.

**Stage 8 — persistence hardening.** P2-01, P2-03, P2-04, P2-05.

**Stage 9 — ground truth and telemetry.** P2-08, IND-10, P2-06/07, P2-10,
P2-11 with IND-03, IND-08, IND-09, IND-11, P3-01, P3-02, P3-03.

---

## 14. Decisions required before implementation

These cannot be resolved from the code and need an explicit call.

1. **CombiPrecip vs. PRECIP (P1-14).** Keep CPC and correct the accumulation
   semantics, or switch to RZC, which is genuinely an instantaneous rate and
   makes the existing threshold semantics correct as written. RZC is the better
   fit for nowcasting; CPC is more accurate for accumulation because of the
   gauge correction. This choice determines the scope of P1-14, P1-15 and the
   detection threshold's value.

2. **Field rename scope (P1-14).** If CPC is retained, does `precip_rate_mmh`
   get renamed through `RadarPixelValue`, `RadarPointReading` and the
   `radar_observations` column, or does the correction stay in documentation
   and threshold values only? The reconstruction document chose
   documentation-only; this log's assessment is that the threshold's numeric
   meaning must change regardless.

3. **Blend weight normalisation (IND-01).** Which of the three approaches in
   IND-01, and is the change acceptable given it alters every existing
   installation's blend output on upgrade?

4. **Retention default (IND-06).** What non-zero default, and is retention asked
   during setup or left to the options flow?

5. **Treatment of the reconstruction document.** Re-apply it as a specification,
   or re-derive each fix independently using it as a reference? This log's
   recommendation is the latter: it is demonstrably wrong on P1-19, would cause
   a production outage on P1-17, understates P1-14, misattributes the mechanism
   on P1-01, and reports a P2-01 sub-bug that does not exist in v0.1.23. Its
   value is real but it is not safe to apply mechanically.

---

## 15. Release gate

**NO-GO for reliability-sensitive automations.**

Minimum bar for reconsidering:

- Stage 0 through Stage 3 complete, each with regression tests that reproduce
  the original failure mode rather than merely exercising the new code.
- P1-19 explicitly *not* applied, with the pinning test in place.
- IND-13 resolved and verified against a real downloaded file — this also
  discharges the standing caveat that the CombiPrecip HDF5 layout has never been
  checked against actual data.
- The Stage 4 auth findings complete, since a silently-degraded integration is
  the failure mode most likely to go unnoticed.
- Test count and pyflakes state recorded against the 198 / 5 baseline in this
  document.

---

## Appendix A — verification environment

```
python 3.12
pytest, pyflakes, aiohttp, h5py, numpy, pyproj installed
cd <extracted zip root>
python -m pytest tests/ -q                                  # 198 passed
python -m pyflakes custom_components/swissweather_fusion/    # 5 warnings
```

The five pyflakes warnings, all pre-existing and cosmetic:

```
__init__.py:14     '.const.CONF_ELEVATION_EFFECTIVE' imported but unused
config_flow.py:22  'homeassistant.core.HomeAssistant' imported but unused
coordinator.py:20  'tempfile' imported but unused
sensor.py:11       'datetime.timedelta' imported but unused
sensor.py:11       'datetime.timezone' imported but unused
```

## Appendix B — external sources consulted

- MeteoSwiss, *Precipitation radar products* (open data documentation) —
  product codes, file naming convention including the quality code, temporal
  aggregation table, and the 5-minute update frequency for CombiPrecip.
  Decisive for IND-13, P1-14, P1-16, P1-17 and P1-19.
- MeteoSwiss, *CombiPrecip product page* — product family description.
- EUMETNET OPERA, *ODIM_H5 v2.4* — referenced by the MeteoSwiss documentation as
  the file format, corroborating the layout `clients/combiprecip.py` assumes.
- Home Assistant developer documentation — config entry unloading,
  `ConfigEntryAuthFailed` handling, config flow reauth and reconfigure.

## Appendix C — finding index

| ID | Verdict | Assessed | Area |
| --- | --- | --- | --- |
| P0-01 | CONFIRMED | P0 | Learning atomicity |
| P0-02 | CONFIRMED | P0 | Model B trigger state |
| P0-03 | CONFIRMED | P0 | Lifecycle |
| P0-04 | CONFIRMED (raised) | P0 | Dedup/ingestion |
| IND-13 | NEW | P0 | Radar product identity |
| IND-01 | NEW | P0 | Model A blend weights |
| P1-01 | CONFIRMED (evidence corrected) | P1 | Auth |
| P1-02 | CONFIRMED | P1 | Auth |
| P1-03 | CONFIRMED | P1 | Secrets |
| P1-04 | CONFIRMED | P1 | Diagnostics privacy |
| P1-05 | CONFIRMED | P1 | Diagnostics privacy |
| P1-06 | CONFIRMED | P1 | Quota |
| P1-07 | CONFIRMED | P1 | Quota |
| P1-08 | CONFIRMED | P1 | Restart state |
| P1-09 | CONFIRMED | P1 | Scheduling |
| P1-10 | CONFIRMED | P1 | Run identity |
| P1-11 | CONFIRMED | P1 | Run identity (docs only) |
| P1-12 | CONFIRMED | P1 | Model B refinement |
| P1-13 | CONFIRMED / FIX-DIVERGENT | P1 | Radar freshness |
| P1-14 | CONFIRMED / FIX-DIVERGENT | P1 | Radar semantics |
| P1-15 | CONFIRMED | P2 | Radar semantics |
| P1-16 | CONFIRMED / FIX-DIVERGENT | P1 | Radar quality |
| P1-17 | CONFIRMED (superseded) | P0 | Radar selection |
| P1-18 | CONFIRMED | P1 | Radar sampling |
| P1-19 | **REFUTED** | — | Radar polling |
| P1-20 | CONFIRMED | P1 | Input validation |
| P1-21 | CONFIRMED | P1 | Input units |
| P1-22 | PARTIAL | P1 | Pressure semantics |
| P1-23 | CONFIRMED | P1 | Provider validation |
| P1-24 | CONFIRMED | P1 | Parsing |
| P1-25 | CONFIRMED | P2 | Timestamps |
| P1-26 | CONFIRMED | P1 | Reconfigure |
| P1-27 | CONFIRMED | P1 | Config validation |
| P1-28 | CONFIRMED | P2 | Config validation |
| P1-29 | CONFIRMED | P2 | Config UX |
| P1-30 | CONFIRMED | P1 | Config validation |
| P2-01 | CONFIRMED / FIX-DIVERGENT | P2 | Migration |
| P2-02 | CONFIRMED | P2 | State integrity |
| P2-03 | CONFIRMED | P2 | Concurrency |
| P2-04 | CONFIRMED | P2 | Concurrency |
| P2-05 | CONFIRMED (evidence corrected) | P2 | Reconstructability |
| P2-06 | CONFIRMED | P2 | Model B semantics |
| P2-07 | CONFIRMED | P2 | Model B fusion |
| P2-08 | CONFIRMED | P2 | Ground truth |
| P2-09 | CONFIRMED | P2 | Time semantics |
| P2-10 | CONFIRMED | P2 | Weather entity |
| P2-11 | CONFIRMED (scope widened) | P2 | Telemetry |
| P2-12 | CONFIRMED | P2 | Lifecycle |
| P2-13 | PARTIAL | P2 | Entry identity |
| P3-01 | CONFIRMED | P3 | Telemetry |
| P3-02 | CONFIRMED | P3 | Telemetry |
| P3-03 | CONFIRMED | P3 | Metadata |
| IND-02 | NEW | P1 | Model B robustness |
| IND-03 | NEW | P1 | Telemetry correctness |
| IND-04 | NEW | P1 | Model B trigger rate |
| IND-05 | NEW | P1 | Lifecycle |
| IND-06 | NEW | P1 | Storage growth |
| IND-07 | NEW | P2 | Restart state |
| IND-08 | NEW | P1 | HA entity surface |
| IND-09 | NEW | P2 | HA entity surface |
| IND-10 | NEW | P2 | Data lifecycle |
| IND-11 | NEW | P2 | Missing functionality |
| IND-12 | NEW | P3 | Pressure semantics |
| INFRA-01 | NEW | Blocker | Test harness |
| INFRA-02 | NEW | Blocker | Test harness |
| INFRA-03 | NEW | Blocker | Test coverage |
