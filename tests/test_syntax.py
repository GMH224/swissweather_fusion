"""Syntax-only checks for the files that import Home Assistant directly.

Honest limitation, stated plainly rather than glossed over: config_flow.py,
coordinator.py, weather.py, sensor.py, __init__.py, and binary_sensor.py
cannot be functionally exercised in this test suite without a running
Home Assistant instance and its test harness (pytest-homeassistant-custom-
component), which wasn't installed when this was built. This test only
confirms they're syntactically valid Python — it is NOT equivalent to
confirming they work correctly against a real HA core. Treat this as a
minimum bar cleared, not a substitute for testing inside an actual HA dev
environment before relying on this in production. See DEVELOPER.md.
"""
import ast
import os

INTEGRATION_DIR = os.path.join(
    os.path.dirname(__file__), "..", "custom_components", "swissweather_fusion"
)

HA_DEPENDENT_FILES = [
    "__init__.py",
    "config_flow.py",
    "coordinator.py",
    "weather.py",
    "sensor.py",
    "binary_sensor.py",
]


def test_ha_dependent_files_are_syntactically_valid():
    for filename in HA_DEPENDENT_FILES:
        path = os.path.join(INTEGRATION_DIR, filename)
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        # Raises SyntaxError on failure, which pytest reports clearly with
        # the offending file and line — no manual assertion needed.
        ast.parse(source, filename=filename)
