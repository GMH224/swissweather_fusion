# SwissWeather Fusion v0.3.6 — Release Audit (ICS)

**Release type:** defect fix (database performance). Forecast storage,
indexes, purge and coordinator timeout handling.
**Shipped during:** the Class A accuracy trial (closes end of November 2026).
Checked against the trial rule — *it must not alter what the trial measures* —
and against the version rule (v0.4.0 is reserved for changes to learning).
Nothing here changes what is learned, how it is learned or what the blend
outputs; this was proven on a copy of the owner's real database (§4), so it is
a v0.3.x release.
**Verification:** 956 tests passing (913 carried, 2 of them adapted to the new
index set, 43 new), package pyflakes clean, 20 targeted mutations all caught
(§5). Schema version 6, no shape change. One-time background maintenance; no
learning reset, no configuration change.

---

## 1. Problem

On 31 Oct 2026 the owner's weather card showed "unavailable". The log showed
the blend (120 s), learning (120 s) and station (30 s) coordinators timing out
together, repeatedly. Database: 3.3 GB, 14,518,321 forecast rows.

Measured on a copy of that database:

| Query | What it read | Time |
|---|---|---|
| Learning: pending rows | partial index covering 12.4 M rows; 1,599 returned | 15–45 s |
| Station reference value (3 per 5-minute poll) | whole table: `substr(valid_at, 1, 13) = ?` uses no index | 1.2–2.3 s each |
| Blend window | 1.28 M rows | 6–8 s |

On small home-server hardware (file not in cache)
these are several times slower. The connection is shared under one lock, so
the jobs queued behind each other.

**Causes.**

1. **Rows without a value: 8,469,826 (58 %).** Each model returns a 7-day grid
   with NULLs beyond its own horizon, and all of them were stored. The blend
   skips a NULL before keying (it cannot mask an older real value), and
   learning marks it 'skipped' without updating a bucket. All 763,898 skipped
   temperature/humidity/pressure rows on the real database are NULL rows.
2. **'pending' rows that can never be reconciled: 12.4 M (86 %).** Learning
   reconciles only temperature, humidity and pressure. Every other row was
   still inserted 'pending', and the purge protects 'pending' rows, so those
   rows were never purged and the "pending" index covered nearly the whole
   table.
3. **A timeout does not stop an executor job.** `asyncio.timeout` cancels the
   await; the thread keeps running and keeps the database lock. The next
   cycle queued another job behind it. Learning also released its shared lock
   on timeout while its thread was still working.

## 2. What changed

| Item | Detail |
|---|---|
| Insert | `insert_forecast_snapshots_bulk` drops rows whose value is None. Status at insert: 'pending' for `LEARNED_VARIABLES` (temperature, humidity, pressure), else `'not_learned'`. |
| Indexes | New `idx_forecast_valid(valid_at)` and partial `idx_forecast_learn_pending(valid_at) WHERE reconciliation_status='pending' AND variable IN (learned)`. They replace `idx_forecast_pending`. `idx_forecast_source_ts` kept. |
| Learning query | Carries the partial index's WHERE terms literally (SQLite uses a partial index only then). Same rows; refuses a variable that is not learned. |
| Reference queries | `get_reference_value`, `get_reference_pressure_hpa` and the pressure check in `reset_all_learning`: hour range `valid_at >= prefix AND valid_at < prefix||'~'` instead of `substr()`. |
| Purge | Forecast rows deleted in batches of 20,000, each its own transaction. Protected: pending AND learned. |
| Reset | `reset_all_learning` re-opens only learned variables. |
| Maintenance | One-time, in the background, 10 minutes after setup, never at startup: delete NULL rows in id batches of 50,000, build the two indexes, drop the old one. Progress in `schema_meta`; resumes after a restart. Purge waits until it is done, then runs once. Empty or fresh databases get the indexes immediately. |
| Executor guard | `_SingleFlight`: job awaited through `asyncio.shield`; no second job while one runs. Blend serves its previous result up to 2 h old; learning holds its lock until the job ends; Model B keeps its last score; the station write is shielded and the readings are returned. |
| Database size sensor | New attribute `storage_maintenance` (progress); row counts refreshed when the maintenance ends. |
| Schema | `SCHEMA_VERSION` 6, no shape change. |
| Test setup | `voluptuous-serialize` added to `requirements-test.txt` (missing; a clean environment failed collection). |
| Version | manifest 0.3.6. |

## 3. Trial protection

The trial measures learned bias correction and the paired blend comparison.
Each change was checked for an effect on them:

- **Removed rows.** Only rows whose value is NULL. The blend skips them before
  keying; learning marks them 'skipped' and updates no bucket. Not storing
  them removes nothing either reads.
- **'not_learned' status.** Applied only to variables the learning query never
  selects. The learning query itself selects the same rows (§4).
- **Purge.** Learned pending rows are protected exactly as before. Newly
  purgeable: rows of unlearned variables older than `purge_days`, which
  learning could never read.
- **Timeout handling.** Changes when a cycle reports failure, not what a
  completed cycle computes. Learning's lock now covers the whole job, which
  makes the P2-04 protection (no purge mid-batch) hold also on timeout.

## 4. Equivalence proof on the owner's database

Two copies of the real database, frozen time
2026-10-31T10:05Z. Copy A: v0.3.5 code. Copy B: v0.3.6 code, maintenance run
to completion first. Then on each: pending query, 196 reference values, blend,
full learning cycle, blend again.

| Compared | Result |
|---|---|
| Learning selection (ids of rows with a value) | identical (690 rows; v0.3.5 also returned 909 NULL rows) |
| Reference values, 49 hours × 4 queries | identical (193 non-null) |
| Blend before learning | identical (full output) |
| bucket_stats after learning | identical (1,062 rows) |
| Status of every learned row with a value | identical |
| blend_comparison rows | identical |
| Comparison report, learning progress, temperature MAE | identical |
| Blend after learning | identical (full output) |
| All rows with a value | identical |

Timings (this build machine; both versions ran at the same time on 2 cores,
so v0.3.5 figures are somewhat inflated):

| Step | v0.3.5 | v0.3.6 |
|---|---|---|
| Learning pending query | 45.1 s | 0.003 s |
| 196 reference values | 235.7 s | 1.07 s |
| Blend | 7.4 s | 1.0 s |
| Learning cycle | 15.0 s | 0.4 s |
| Blend after learning | 9.4 s | 1.2 s |

Query plans after maintenance: pending → `idx_forecast_learn_pending`; blend
window and reference → `idx_forecast_valid`.

Maintenance on the copy: 301 steps, 64 s total, 8,469,826 rows removed; worst
deletion batch 0.28 s; index builds 6.95 s and 4.46 s (each holds the lock).
The first purge (90 days) afterwards: 27,810 forecast rows, 0.44 s.

Restart after maintenance: open 0.001 s, old index not recreated. Downgrade
to v0.3.5 on a maintained database: opens (rebuilds its old index, 13 s here)
and works.

## 5. Tests and mutations

43 new tests (`tests/test_v0_3_6_storage.py`). Two existing tests adapted:
`test_every_index_can_be_created_on_an_upgraded_database` (a populated
upgraded database keeps the old index until maintenance) and
`test_retention_first_refresh_runs` (purge runs after maintenance).

| # | Mutation | Caught |
|---|---|---|
| S1 | NULL rows stored again | ✓ |
| S2 | every row 'pending' | ✓ |
| S3 | purge protects every 'pending' row (old rule) | ✓ |
| S4 | reset re-opens every variable | ✓ |
| S5 | hour range upper bound wrong (`:` instead of `~`) | ✓ |
| S6 | maintenance deletes rows with values | ✓ |
| S7 | purge stops after one batch | ✓ |
| S8 | old index not dropped | ✓ |
| S9 | populated database gets indexes at startup | ✓ |
| S10 | maintenance skips id batches | ✓ |
| C1 | blend starts a second job while busy | ✓ |
| C2 | blend fails on timeout instead of serving previous | ✓ |
| C3 | learning lock never released | ✓ |
| C4 | purge not deferred during maintenance | ✓ |
| C5 | second maintenance task started | ✓ |
| C6 | purge not run after maintenance | ✓ |
| C7 | job not shielded (timeout cancels it) | ✓ |
| C8 | maintenance task not cancelled on shutdown | ✓ |
| C9 | station write not shielded (late failure lost) | ✓ |
| C10 | storage figures not refreshed after maintenance | ✓ |

## 6. Risk and residuals

- **Index builds hold the database lock** (7 s and 4.5 s here; estimated
  30–60 s each on the owner's hardware, not measured). During them the blend
  serves its previous result, station readings stay available, and no job is
  queued twice. Learning may report one slow cycle.
- **The file does not shrink.** About 1.8 GB becomes free pages inside the
  file and is reused. Compacting (VACUUM) needs about 3.3 GB free temporarily
  and is left as an optional later step.
- **Legacy 'pending' rows of unlearned variables** are not rewritten; they age
  out through the purge.
- **Stale blend.** At most 2 hours, and only when cycles are failing; the log
  says so on every served cycle.
- **Not verifiable here:** timings on the owner's hardware; Home Assistant's real
  `async_create_background_task` (the test environment stubs it).

## 7. Post-install check

1. About 10 minutes after the restart the log shows `Storage maintenance
   (v0.3.6) starting`, and a few minutes later `finished in … s: … empty rows
   removed`.
2. The weather entity stays available; no more `Timeout` lines for blend,
   learning or station.
3. The **Database size** sensor (diagnostic): attribute
   `storage_maintenance` moves from `delete_nulls:…` to `done`, and
   `forecast_snapshots_rows` drops by roughly half. The size in MB stays about
   the same.

## 8. Upgrade

Replace the `custom_components/swissweather_fusion` folder and restart Home
Assistant. No configuration change, no learning reset. For anything reading
the integration's entities or attributes, nothing changed apart from the new
`storage_maintenance` attribute on the Database size sensor.
