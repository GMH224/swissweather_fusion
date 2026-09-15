# SwissWeather Fusion v0.3.2 — Release Audit (ICS)

**Release:** v0.3.2
**Prior release:** v0.3.1 (deployed, running)
**Schema:** v5 — **unchanged. No migration, no learning reset.**
**Date:** 15 September 2026
**Verdict:** **PASS — one defect class closed, two field-reported defects fixed**
**Tests:** 795 passing (from 783), pyflakes clean

---

## 1. The defect that matters

### SWF-031-004 (Critical) — every configuration form returned 500, for eighteen releases

**Reported from the field** as *"Config flow could not be loaded: 500
Internal Server Error"*, while the maintainer was checking an elevation
setting at this project's request.

**Root cause.** Home Assistant does not hand a voluptuous schema to the
frontend. It serialises it to JSON with `voluptuous_serialize`, which
understands a fixed set of voluptuous constructs and raises
`ValueError: Unable to convert schema` on anything else — including an
ordinary Python function inside `vol.All`.

Two validators were plain functions, both introduced in v0.1.24:

| Validator | Introduced as | Used by |
| --- | --- | --- |
| `_finite_float` | P1-27 (reject "nan"/"inf" strings) | user step, reconfigure step |
| `_non_empty_str` | P1-30 (reject empty credentials) | **options step** |

So from v0.1.24 onward **setup, reconfigure and Configure all returned
500**. Not degraded — unopenable.

**Why it survived eighteen releases and three audits.** The only form
anyone opens after installation is Configure, and nobody opened it. The
test suite passed throughout, because a unit test calls a validator
directly and never performs the serialisation step. 783 green tests, and
the integration could not be configured.

**Fix.** Both validators are now `vol.Coerce` subclasses. Validation is
byte-identical — `"nan"`, `"inf"`, `"-inf"`, non-finite floats, empty and
whitespace-only strings are all still rejected, `"471"` and `"  secret  "`
still coerce — and `voluptuous_serialize` dispatches on
`isinstance(schema, vol.Coerce)` and reads `.type`, so the schema renders.

**Two tests, deliberately.** One reproduces HA's serialisation step over
every validator discovered by inspection. The other asserts the SHAPE —
no validator may be built from a plain function — so a future validator
written the old way fails with a message naming the real problem rather
than a serialisation traceback.

### Correction to the v0.3.1 triage

The initial diagnosis was that v0.3.1's own fix for SWF-ICS-003/064
(applying `_ELEVATION_VALIDATOR` to the options flow) had broken a
working form. That was wrong, and it was corrected on evidence:
`_non_empty_str` has the identical defect and was already in the options
flow. v0.3.1 did not cause the outage; it was one of eighteen releases
that carried it.

**This also retires SWF-ICS-001 as a live risk.** The reconfigure step's
elevation default of `0.0` is a real defect and remains fixed — but that
step has never rendered on any installation since v0.1.24, so it could
not have been triggered. The reported ~3 degC exposure was zero.

---

## 2. Field-reported forecast defects

### SWF-032-001 (High) — the final forecast day collapsed by eight to ten degrees

**Reported from the field**, with the maintainer's own hypothesis —
*"not all forecast fields have the same forecast duration and the
algorithm thinks all have the same"* — which was correct and understated.

Sources have different horizons, so the hourly series stops partway
through the final calendar day. `aggregate_daily_forecast` then took
`max()`/`min()` over whatever survived, with **no coverage check of any
kind**.

Confirmed from the live entity dump of 2026-09-15: the final day held
exactly three entries, 02:00 / 05:00 / 08:00 local at 12.0, 11.0 and
11.4 degC, published as a 12 deg high beside genuine forecasts in the low
twenties. Those are the coldest hours of the day, so the reported high
was the overnight minimum.

**Fix.** A day is published only if its samples reach the local afternoon
window, where a daily maximum actually occurs. This is a statement about
*which* hours are present, not how many — three afternoon samples
describe a maximum; twelve overnight samples do not. The first day is
exempt, because it is partial by construction and Home Assistant expects
today in the list even at 23:00.

### SWF-032-002 (Medium) — daily precipitation summed three-hourly samples as hourly

Same dump: past the shorter sources' horizons the series drops to
three-hourly. The daily total was a plain sum, understating such a day by
roughly a factor of three.

**Fix.** No total is published below 75% hourly coverage. `None` rather
than a corrected estimate, because whether a three-hourly value is an
hourly rate or a three-hour accumulation is a per-provider question this
project has not established, and multiplying by three on an assumption
replaces a visibly-low number with a confidently wrong one. The
temperature range is still reported — a max over available samples is a
real statement about those samples in a way a sum is not.

---

## 3. Field reports investigated and found NOT to be defects

Recorded because a report dismissed without a reason gets re-reported.

**The 26.6 degC daily high.** Queried as implausible against a 13.6 degC
current reading. The hourly dump shows a genuine sunny afternoon peak at
14:00Z. The screenshot that prompted the question was from a different
day. No defect.

**meteoblue and meteonomiqs showing `last_success_time: null`.** Flagged
during analysis as "never fetched", with the inference that the blend was
running on four sources instead of five. **That inference was wrong.**
meteoblue polls only at 12:00/16:00/20:00 local under its annual call
budget; the diagnostics were captured at 10:57 local, after a restart,
before the day's first slot. The field is in-memory and resets on
restart. Not a defect — but see §5.

---

## 4. Bugs introduced during this pass

### SWF-032-003 (Medium) — a scripted edit removed an unrelated validator

The `_finite_float` conversion was done by replacing a source slice
between two anchors. `_non_empty_str` sat between them and was deleted
with it. Caught immediately by `test_syntax.py::test_no_undefined_names`
and two lifecycle tests.

Recorded because this is the second scripted refactor in two releases to
damage something adjacent to its target (see SWF-031-003). Anchor-based
source edits over a range are not safe when the range contains anything
the author did not enumerate.

---

## 5. Open items, not fixed here

**Blend humidity MAE of 27 percentage points.** Present in the live
comparison data with **five samples**, immediately after a cold-start
reset, so it may be noise. It may also be real: the hourly dump contains
hours where humidity and dew point contradict each other — 10.9 degC with
a 9.0 degC dew point reported as 63% RH, where the psychrometric value is
near 88%. That is the signature of humidity and dew point arriving from
different source sets with no cross-check between them.

Not diagnosed here because the sample is too small to act on, and because
a wrong fix to a fusion rule is worse than a known-open question. To be
re-examined when the comparison has accumulated real data.

**Station entity configuration.** Worth the maintainer confirming that
`station_humidity_entity` points at an outdoor sensor. A blend humidity
MAE of 27 points is also what an indoor reference would produce, and this
project has no way to tell the difference.

**`station_pressure_is_sea_level` disagrees between `config_data`
(False) and `config_options` (True)**, as does `purge_days` (0 against
90). Options take precedence, so behaviour follows the options value —
but a physical datum recorded two ways in one entry is a latent trap. To
be reconciled in a release that touches the config flow, now that the
config flow can actually be opened.

**Source health cannot distinguish "not scheduled yet" from "broken".**
`last_success_time: null` means both, which is what produced the false
alarm in §3. A scheduled source should report its next slot.

---

## 6. Verification record

**Tests.** 795 passing, up from 783. 12 new. No schema change, no
migration, no learning reset — v0.3.2 preserves everything v0.3.1
accumulated.

**Mutation testing**, four mutations, all caught:

| Mutation | Tests failed |
| --- | --- |
| Partial-day coverage rule removed | 2 |
| Coverage rule also applied to today | 3 |
| Precipitation coverage rule removed | 2 |
| Validator no longer a `vol.Coerce` subclass | 3 (errors) |

**The defect class, now under test.** Three Home Assistant runtime
surfaces that no unit test previously exercised:

1. Schema serialisation — every validator, discovered by inspection.
2. Diagnostics JSON encoding — the comparison report must encode, since
   the diagnostics download is the only route by which this project's
   measurement reaches anyone.
3. The structural rule behind (1), so the next occurrence fails with a
   message that names the cause.

**Still not covered by any test:** translation-key completeness for
config-flow steps and errors, and entity attribute contracts. Both are
the same class. Neither is closed, and saying so is the point.

**Not done:** no live Home Assistant run. The serialisation test
reproduces HA's step using the same library HA uses, which is stronger
than inspection and weaker than running it.
