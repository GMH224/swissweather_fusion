"""Run a script with the test suite's Home Assistant stubs loaded.

Usage: python3 wrap.py <tree root> <script> [args...]
"""
import importlib, runpy, sys
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1]+"/custom_components")
importlib.import_module("tests.conftest")  # installs the Home Assistant stubs
script=sys.argv[2]; sys.argv=[script, sys.argv[1]+"/custom_components"]+sys.argv[3:]
runpy.run_path(script, run_name="__main__")
