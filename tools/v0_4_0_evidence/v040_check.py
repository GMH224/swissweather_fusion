"""Run one blend (and one learning cycle) of a given code tree on a copy of
the real database at a frozen time; dump the result for comparison.

Usage: python3 wrap.py <tree> v040_check.py <db copy> <out.json>
"""
import json, sys, time
from datetime import datetime, timezone
from freezegun import freeze_time

sys.path.insert(0, sys.argv[1])
dbpath, outpath = sys.argv[2], sys.argv[3]
NOW = datetime(2026, 11, 15, 17, 5, tzinfo=timezone.utc)
_pc = (lambda f: (lambda: f()))(time.perf_counter)
out = {"t": {}}
with freeze_time(NOW):
    from swissweather_fusion.storage.db import SwissWeatherDB
    from swissweather_fusion import coordinator as coord
    from tests.test_v0_2_1_fixes import FakeHass
    a = _pc(); db = SwissWeatherDB(dbpath); out["t"]["open"] = round(_pc() - a, 3)
    out["schema"] = dict(db._conn.execute("select key, value from schema_meta where key like 'schema_version' or key like 'v0_4%'").fetchall())
    out["seasons"] = [tuple(r) for r in db._conn.execute("select season, count(*) from bucket_stats group by 1")]
    blend = coord.ModelABlendCoordinator(FakeHass(), db)
    a = _pc(); b = blend._compute_blend(); out["t"]["blend"] = round(_pc() - a, 3)
    out["blend"] = json.loads(json.dumps(b, default=str))
    learn = coord.ModelALearningCoordinator(FakeHass(), db)
    a = _pc(); learn._reconcile(); out["t"]["learn"] = round(_pc() - a, 3)
    out["comparison_rows_now"] = [tuple(r) for r in db._conn.execute(
        "select valid_at, measurement, lead_hours, blend_value, " +
        ("persistence_delta" if "persistence_delta" in {c[1] for c in db._conn.execute("pragma table_info(blend_comparison)")} else "0") +
        " from blend_comparison where issued_at = '2026-11-15T17:00:00+00:00' order by 2, 3")]
    db.close()
json.dump(out, open(outpath, "w"), indent=1, default=str)
print(json.dumps(out["t"]), out["schema"], out["seasons"])
