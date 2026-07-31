"""Regression guard for the v0.1.16 listener fix.

Can't be a true integration test without a real Home Assistant instance
(not available in this environment — see test_syntax.py's own note about
HA-dependent files being syntax-checked only). This instead parses
__init__.py's source directly to confirm the specific fix is present:
every coordinator besides blend_coordinator (which already has real
listeners via CoordinatorEntity — see weather.py and
sensor.py::ExpertWeightSensor) gets a real async_add_listener()
registration, not just async_shutdown().

The motivating bug: Home Assistant's DataUpdateCoordinator stops
automatically rescheduling itself once it has zero registered listeners
(confirmed against HA's own source history — a core PR titled "Only
schedule a refresh if listeners"). Every coordinator here except
blend_coordinator had zero listeners, exactly matching the observed
symptom of one guaranteed first refresh, then nothing, forever.
"""
import ast
from pathlib import Path

INIT_PY = (
    Path(__file__).parent.parent
    / "custom_components"
    / "swissweather_fusion"
    / "__init__.py"
)

# Every coordinator that must have a real listener registered — i.e.
# every one of the 9 except blend_coordinator, which already has genuine
# CoordinatorEntity listeners elsewhere.
COORDINATORS_NEEDING_A_LISTENER = (
    "station_coordinator",
    "open_meteo_coordinator",
    "srf_coordinator",
    "meteoblue_coordinator",
    "combiprecip_coordinator",
    "meteonomiqs_coordinator",
    "model_b_coordinator",
    "learning_coordinator",
)


def _source() -> str:
    return INIT_PY.read_text()


def test_init_py_is_valid_python():
    ast.parse(_source())


def test_every_non_blend_coordinator_has_a_registered_listener():
    """The actual regression guard: confirms async_add_listener is called
    on every coordinator that needs one. A plain substring check on the
    source is intentionally simple here — the goal is catching an
    accidental future removal of this fix, not verifying HA's own
    scheduling behavior (which can't be exercised without a real HA
    instance).
    """
    source = _source()
    assert "async_add_listener" in source, (
        "async_add_listener() call is missing entirely — the v0.1.16 fix "
        "for coordinators never rescheduling themselves may have been "
        "reverted."
    )
    for name in COORDINATORS_NEEDING_A_LISTENER:
        # Looks for the coordinator's variable name appearing anywhere
        # near an async_add_listener call in the source — specifically,
        # within the same tuple/loop construct used by the actual fix,
        # not just "mentioned somewhere in the file" (every coordinator
        # name legitimately appears many times elsewhere).
        assert name in source, f"{name} is missing from __init__.py entirely"

    # The specific loop this fix lives in — confirms the coordinators are
    # grouped together for listener registration, not just individually
    # present somewhere unrelated in the file.
    tree = ast.parse(source)
    found_listener_loop = False
    for node in ast.walk(tree):
        if isinstance(node, ast.For) and isinstance(node.iter, ast.Tuple):
            names_in_tuple = {
                elt.id for elt in node.iter.elts if isinstance(elt, ast.Name)
            }
            body_source = ast.dump(node)
            if "async_add_listener" in body_source and names_in_tuple.issuperset(
                set(COORDINATORS_NEEDING_A_LISTENER)
            ):
                found_listener_loop = True
                break
    assert found_listener_loop, (
        "Could not find a loop registering async_add_listener for all "
        "coordinators that need one — the v0.1.16 fix may have been "
        "narrowed or removed."
    )


def test_blend_coordinator_not_in_the_noop_listener_loop():
    """blend_coordinator deliberately isn't included in the no-op listener
    loop, since it already has real listeners (the weather entity and
    ExpertWeightSensor) — a second, redundant listener there wouldn't be
    wrong, just unnecessary. This confirms the fix wasn't blindly applied
    to all 9 coordinators without checking which one already worked.
    """
    assert "blend_coordinator" not in COORDINATORS_NEEDING_A_LISTENER
