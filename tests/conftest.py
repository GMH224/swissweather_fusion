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
    update_coordinator.DataUpdateCoordinator = type("DataUpdateCoordinator", (), {"__init__": lambda self, *a, **kw: None})
    update_coordinator.UpdateFailed = type("UpdateFailed", (Exception,), {})
    update_coordinator.CoordinatorEntity = type("CoordinatorEntity", (), {"__init__": lambda self, *a, **kw: None})

    util = _module("homeassistant.util")
    util.__path__ = []
    dt_util = _module("homeassistant.util.dt")
    from datetime import datetime as _datetime, timezone as _timezone
    dt_util.now = lambda: _datetime.now(_timezone.utc)

    entity_platform = _module("homeassistant.helpers.entity_platform")
    entity_platform.AddEntitiesCallback = object

    components = _module("homeassistant.components")
    components.__path__ = []
    weather_component = _module("homeassistant.components.weather")
    weather_component.WeatherEntity = type("WeatherEntity", (), {})
    weather_component.WeatherEntityFeature = type("WeatherEntityFeature", (), {"FORECAST_HOURLY": 1})
    sensor_component = _module("homeassistant.components.sensor")
    sensor_component.SensorEntity = type("SensorEntity", (), {})
    binary_sensor_component = _module("homeassistant.components.binary_sensor")
    binary_sensor_component.BinarySensorEntity = type("BinarySensorEntity", (), {})

    voluptuous = _module("voluptuous")
    voluptuous.Schema = lambda *a, **kw: None
    voluptuous.Required = lambda *a, **kw: None
    voluptuous.Optional = lambda *a, **kw: None
    voluptuous.Coerce = lambda *a, **kw: None


_install_homeassistant_stubs()

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "custom_components")
)

