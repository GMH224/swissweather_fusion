"""Syntax and static-analysis checks for the files that import Home
Assistant directly.

Honest limitation, stated plainly rather than glossed over: config_flow.py,
coordinator.py, weather.py, sensor.py, __init__.py, and binary_sensor.py
cannot be functionally exercised in this test suite without a running
Home Assistant instance and its test harness (pytest-homeassistant-custom-
component), which wasn't installed when this was built. These checks only
confirm the files are syntactically valid and free of undefined names /
duplicate definitions — that is NOT equivalent to confirming they work
correctly against a real HA core. Treat this as a minimum bar cleared,
not a substitute for testing inside an actual HA dev environment before
relying on this in production. See DEVELOPER.md.

**v0.1.22**: added the pyflakes-based check after a real production
crash — a duplicate `async def async_get_config_entry_diagnostics`
definition in diagnostics.py (an artifact of an imprecise edit; Python
silently keeps the LAST definition of a module-level function, and that
second copy was missing a variable the first one had) caused
`NameError: name 'secrets' is not defined` on every single "Download
Diagnostics" click. `ast.parse`-based syntax checking is fundamentally
unable to catch this — duplicate function names, and references to
undefined names inside a function body, are both syntactically legal
Python; the error only manifests when that specific code path actually
executes. pyflakes does real flow analysis and would have caught this
immediately, with no HA install and no live execution required — added
here as a fast, dependency-light net for exactly this class of bug
going forward, functionally the same gap this file's docstring already
call out is not covered by the AST parse checks above it.
"""
import ast
import os

import pyflakes.checker

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
    "device.py",
    "diagnostics.py",
]

# Every .py file in the package, not just the HA-dependent ones above —
# pyflakes doesn't need a real HA install to analyze a file (unlike the
# functional tests), so there's no reason to skip the plain files too.
ALL_PACKAGE_FILES = [
    os.path.join(root, f)
    for root, _dirs, files in os.walk(INTEGRATION_DIR)
    for f in files
    if f.endswith(".py")
]


def test_ha_dependent_files_are_syntactically_valid():
    for filename in HA_DEPENDENT_FILES:
        path = os.path.join(INTEGRATION_DIR, filename)
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        # Raises SyntaxError on failure, which pytest reports clearly with
        # the offending file and line — no manual assertion needed.
        ast.parse(source, filename=filename)


def test_no_duplicate_top_level_definitions():
    """v0.1.22 regression test: this is specifically what ast.parse alone
    cannot catch — a second `def`/`async def`/`class` with the same name
    as an earlier one at the same scope is syntactically valid Python
    (the later one silently wins), which is exactly how the real
    diagnostics.py NameError bug shipped. Checks every .py file, both
    at module level and recursively within each class body.
    """
    problems = []
    for path in ALL_PACKAGE_FILES:
        with open(path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)

        def check_scope(node, scope_label, *, _path=path):
            seen = {}
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if child.name in seen:
                        problems.append(
                            f"{_path}: '{child.name}' defined twice in {scope_label} "
                            f"(lines {seen[child.name]} and {child.lineno}) — the "
                            f"second definition silently wins and the first is dead code"
                        )
                    seen[child.name] = child.lineno
                if isinstance(child, ast.ClassDef):
                    check_scope(child, f"{scope_label}.{child.name}", _path=_path)

        check_scope(tree, "<module>")

    assert not problems, "\n" + "\n".join(problems)


def test_no_undefined_names():
    """v0.1.22 regression test: pyflakes does real flow analysis (unlike
    ast.parse, which only checks grammar) — this is the check that would
    have caught `NameError: name 'secrets' is not defined` in
    diagnostics.py before it ever shipped, with no HA install needed.
    Only fails on undefined-name-class errors; pyflakes' other opinions
    (unused imports, etc.) are informational, not correctness bugs, and
    aren't what this test is guarding against.
    """
    problems = []
    for path in ALL_PACKAGE_FILES:
        for message in _collect_messages(path):
            text = str(message)
            if "undefined name" in text or "may be undefined" in text:
                problems.append(text)

    assert not problems, "\n" + "\n".join(problems)


def _collect_messages(path: str) -> list:
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source, filename=path)
    checker = pyflakes.checker.Checker(tree, filename=path)
    return checker.messages
