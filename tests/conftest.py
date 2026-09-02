"""Shared pytest configuration.

Adds custom_components/ to sys.path so tests can import
`swissweather_fusion.*` without needing Home Assistant installed.

One real wrinkle worth being explicit about: importing ANY submodule (e.g.
`swissweather_fusion.clients.combiprecip`, which has zero Home Assistant
dependencies itself) still triggers Python to execute the *package's*
`__init__.py` first, which does import Home Assistant (as it must, for
real operation). Rather than install the full `homeassistant` package —
heavy, and not what this test suite is trying to verify — this stubs the
small set of HA symbols that `__init__.py` and friends import, just
enough that the import chain succeeds. The stubs have no real behavior;
they exist purely to satisfy `import` statements during collection.

This means: the pure business logic (models/, clients/, storage/) is
genuinely exercised by these tests. config_flow.py, coordinator.py,
weather.py, sensor.py, __init__.py, and binary_sensor.py are NOT — they're
only confirmed syntactically valid (see test_syntax.py). Testing those for
real requires either a full Home Assistant install or the
pytest-homeassistant-custom-component plugin, neither of which was
available when this was built. Flagged here rather than glossed over.
"""
import os
import sys
import types


def _install_homeassistant_stubs() -> None:
    if "homeassistant" in sys.modules:
        return  # real package is installed; don't shadow it

    def _module(name: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
        return mod

    ha = _module("homeassistant")
    ha.__path__ = []  # mark as a package so submodule imports work

    core = _module("homeassistant.core")
    core.HomeAssistant = type("HomeAssistant", (), {})

    config_entries = _module("homeassistant.config_entries")
    config_entries.ConfigEntry = type("ConfigEntry", (), {})
    config_entries.ConfigFlow = type("ConfigFlow", (), {"__init_subclass__": classmethod(lambda cls, **kw: None)})
    config_entries.OptionsFlow = type("OptionsFlow", (), {})

    const = _module("homeassistant.const")
    const.Platform = type("Platform", (), {"WEATHER": "weather", "SENSOR": "sensor", "BINARY_SENSOR": "binary_sensor"})
    const.UnitOfPressure = type("UnitOfPressure", (), {"HPA": "hPa"})
    const.UnitOfTemperature = type("UnitOfTemperature", (), {"CELSIUS": "°C"})
    const.UnitOfPrecipitationDepth = type(
        "UnitOfPrecipitationDepth", (), {"MILLIMETERS": "mm"}
    )
    const.UnitOfSpeed = type("UnitOfSpeed", (), {"METERS_PER_SECOND": "m/s"})
    const.PERCENTAGE = "%"

    data_entry_flow = _module("homeassistant.data_entry_flow")
    data_entry_flow.FlowResult = dict

    helpers = _module("homeassistant.helpers")
    helpers.__path__ = []

    selector = _module("homeassistant.helpers.selector")

    class _Selector:
        def __init__(self, *a, **kw):
            pass

    selector.EntitySelector = _Selector
    selector.EntitySelectorConfig = lambda **kw: kw
    selector.TextSelector = _Selector
    selector.TextSelectorConfig = lambda **kw: kw
    selector.TextSelectorType = type("TextSelectorType", (), {"PASSWORD": "password"})

    aiohttp_client = _module("homeassistant.helpers.aiohttp_client")
    aiohttp_client.async_get_clientsession = lambda hass: None

    update_coordinator = _module("homeassistant.helpers.update_coordinator")
    # v0.1.26 (INFRA-03): this stub used to be a no-op
    # `lambda self, *a, **kw: None`, so a coordinator constructed through
    # its real __init__ came out without `self.hass`, `self.name` or
    # `self.update_interval` — attributes the REAL DataUpdateCoordinator
    # always sets and that coordinator methods legitimately rely on.
    #
    # The consequence was that a genuinely-constructed coordinator could
    # not have any of its methods called, which pushed every coordinator
    # test toward object.__new__() and hand-set attributes — and that in
    # turn is why __init__ went untested and why v0.1.25 shipped with a
    # TypeError on a constructor call. Raising the stub's fidelity here
    # is what makes tests/test_v0_1_26_construction.py able to do more
    # than check that construction does not raise.
    def _duc_init(self, hass=None, logger=None, *, name=None,
                  update_interval=None, **kwargs):
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
        self.data = None
        self.last_update_success = True

    update_coordinator.DataUpdateCoordinator = type(
        "DataUpdateCoordinator", (), {"__init__": _duc_init}
    )
    update_coordinator.UpdateFailed = type("UpdateFailed", (Exception,), {})
    update_coordinator.CoordinatorEntity = type("CoordinatorEntity", (), {"__init__": lambda self, *a, **kw: None})

    util = _module("homeassistant.util")
    util.__path__ = []
    dt_util = _module("homeassistant.util.dt")
    from datetime import datetime as _datetime, timezone as _timezone
    dt_util.now = lambda: _datetime.now(_timezone.utc)

    # v0.1.24 fix (INFRA-01): homeassistant.exceptions was missing from
    # this stub set entirely. ConfigEntryAuthFailed is what actually
    # drives Home Assistant's reauth flow, and the v0.1.24 auth-
    # propagation fixes (P1-01, P2-12) import it in coordinator.py and
    # __init__.py — without these stubs, importing either module fails at
    # collection time and the whole suite errors out.
    exceptions = _module("homeassistant.exceptions")
    exceptions.HomeAssistantError = type("HomeAssistantError", (Exception,), {})
    exceptions.ConfigEntryAuthFailed = type(
        "ConfigEntryAuthFailed", (exceptions.HomeAssistantError,), {}
    )
    exceptions.ConfigEntryNotReady = type(
        "ConfigEntryNotReady", (exceptions.HomeAssistantError,), {}
    )

    entity_registry = _module("homeassistant.helpers.entity")
    entity_registry.DeviceInfo = dict
    entity_registry.EntityCategory = type(
        "EntityCategory", (), {"DIAGNOSTIC": "diagnostic", "CONFIG": "config"}
    )

    entity_platform = _module("homeassistant.helpers.entity_platform")
    entity_platform.AddEntitiesCallback = object

    components = _module("homeassistant.components")
    components.__path__ = []
    weather_component = _module("homeassistant.components.weather")
    weather_component.WeatherEntity = type("WeatherEntity", (), {})
    weather_component.WeatherEntityFeature = type("WeatherEntityFeature", (), {"FORECAST_HOURLY": 1})
    sensor_component = _module("homeassistant.components.sensor")
    sensor_component.SensorEntity = type("SensorEntity", (), {})
    # v0.1.24 (IND-08): the entity-metadata fixes reference these enums by
    # attribute, so the stub needs the specific members sensor.py uses.
    sensor_component.SensorDeviceClass = type(
        "SensorDeviceClass",
        (),
        {"TIMESTAMP": "timestamp", "DURATION": "duration",
         "TEMPERATURE": "temperature", "ATMOSPHERIC_PRESSURE": "atmospheric_pressure"},
    )
    sensor_component.SensorStateClass = type(
        "SensorStateClass",
        (),
        {"MEASUREMENT": "measurement", "TOTAL_INCREASING": "total_increasing"},
    )
    binary_sensor_component = _module("homeassistant.components.binary_sensor")
    binary_sensor_component.BinarySensorEntity = type("BinarySensorEntity", (), {})
    binary_sensor_component.BinarySensorDeviceClass = type(
        "BinarySensorDeviceClass", (), {"PROBLEM": "problem"}
    )

    _install_voluptuous_stub_only_if_really_missing()


def _install_voluptuous_stub_only_if_really_missing() -> None:
    """v0.1.24 fix (INFRA-02): this stub used to be installed
    unconditionally, shadowing the real, independently-installed
    voluptuous package.

    voluptuous is an ordinary PyPI dependency with ZERO Home Assistant
    dependency of its own — unlike every other module stubbed above,
    which genuinely cannot be imported without installing Home Assistant.
    The stub only ever provided Schema/Required/Optional/Coerce, all as
    no-op lambdas returning None, so every validator built with vol.All /
    vol.Range / vol.Invalid (the v0.1.24 config-flow validation fixes)
    would have silently evaluated to None under test while working
    correctly in production — tests passing against validators that were
    never actually executed.

    Fixed by trying the real import first and falling back to the minimal
    stub only if that genuinely fails, matching the exact pattern already
    used correctly for `homeassistant` itself at the top of
    _install_homeassistant_stubs().
    """
    try:
        import voluptuous  # noqa: F401
        return
    except ImportError:
        pass

    mod = types.ModuleType("voluptuous")
    sys.modules["voluptuous"] = mod
    mod.Schema = lambda *a, **kw: None
    mod.Required = lambda *a, **kw: None
    mod.Optional = lambda *a, **kw: None
    mod.Coerce = lambda *a, **kw: None
    mod.All = lambda *a, **kw: None
    mod.Range = lambda *a, **kw: None
    mod.In = lambda *a, **kw: None
    mod.Invalid = type("Invalid", (Exception,), {})


_install_homeassistant_stubs()

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "custom_components")
)

