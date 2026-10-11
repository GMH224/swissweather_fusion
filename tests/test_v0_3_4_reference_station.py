"""Regression tests for v0.3.4 — MeteoSwiss reference station (SwissMetNet).

Requested by the solar team: the complete current 10-minute dataset of the
nearest MeteoSwiss station (Taenikon for the owner), with the key values as
sensors.

The fixture rows are real: three records of Taenikon (TAE) from 11 Oct 2026,
as captured by the owner's collector, in MeteoSwiss's exact column layout
(32 parameter columns, empty fields included).

Trial guard: the station is observation only. It must not reach the Class A
learning, the reconciliation or the blend.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from swissweather_fusion import coordinator as coord
from swissweather_fusion import config_flow
from swissweather_fusion.clients import meteoswiss_smn as smn
from swissweather_fusion.const import (
    ALL_FORECAST_SOURCES,
    REFERENCE_STATION_MAX_AGE,
    SOURCE_HEALTH_OWNER,
    SOURCE_REFERENCE_STATION,
)

UTC = timezone.utc
HOME = (47.5524, 8.9148)  # the owner's site, Frauenfeld

HEADER = (
    "station_abbr;reference_timestamp;tre200s0;tre005s0;tresurs0;xchills0;ure200s0;"
    "tde200s0;pva200s0;prestas0;pp0qnhs0;pp0qffs0;ppz850s0;ppz700s0;fkl010z1;fve010z0;"
    "fkl010z0;dkl010z0;wcc006s0;fu3010z0;fkl010z3;fu3010z1;fu3010z3;rre150z0;htoauts0;"
    "gre000z0;ods000z0;oli000z0;olo000z0;osr000z0;sre000z0;tso005s0;tso010s0;tso020s0"
)
ROWS = (
    "TAE;11.10.2026 11:20;12.1;14.7;13.9;12.1;59.9;4.6;8.5;952.9;1016.2;1015.8;;;4.6;2.3;"
    "2.5;269;;9;4.4;16.6;15.8;0;0;250;244;346;392;44;0;13.3;13.6;14.6",
    # deliberately out of order: the newest record is NOT the last line
    "TAE;11.10.2026 11:40;12.3;14.7;13.9;12.3;59.8;4.7;8.6;952.7;1016;1015.6;;;5.3;2.3;"
    "2.4;264;;8.6;5;19.1;18;0;0;250;245;346;393;44;0;13.4;13.7;14.6",
    "TAE;11.10.2026 11:30;12.2;14.5;13.7;12.2;60.8;4.9;8.6;952.8;1016.1;1015.7;;;5.1;2.7;"
    "2.8;263;;10.1;4.9;18.4;17.6;0;0;235;231;346;391;42;0;13.3;13.6;14.6",
)
NOW_FILE = "\n".join((HEADER,) + ROWS) + "\n"
NEWEST = datetime(2026, 10, 11, 11, 40, tzinfo=UTC)

META_HEADER = (
    "station_abbr;station_name;station_canton;station_wigos_id;station_type_de;"
    "station_type_fr;station_type_it;station_type_en;station_dataowner;station_data_since;"
    "station_height_masl;station_height_barometer_masl;station_coordinates_lv95_east;"
    "station_coordinates_lv95_north;station_coordinates_wgs84_lat;"
    "station_coordinates_wgs84_lon;station_exposition_de;station_exposition_fr;"
    "station_exposition_it;station_exposition_en;station_url_de;station_url_fr;"
    "station_url_it;station_url_en"
)


def _meta_row(abbr, name, lat, lon, height):
    return (f"{abbr};{name};TG;;;;;;MeteoSchweiz;01.01.1981;{height};;;;{lat};{lon};"
            ";;;;;;;")


# A nearer, precipitation-only station must be skipped in favour of TAE.
META = "\n".join([
    META_HEADER,
    _meta_row("XRF", "Nearby rain gauge", 47.5550, 8.9200, 400),     # ~0.5 km, no radiation
    _meta_row("TAE", "Aadorf / Tänikon", 47.479892, 8.904928, 539),  # ~8 km
    _meta_row("KLO", "Zürich / Kloten", 47.479580, 8.536110, 426),   # ~29 km
    _meta_row("SBO", "Far away", 46.0, 7.0, 500),                    # far
]) + "\n"
RAIN_ONLY_FILE = "station_abbr;reference_timestamp;rre150z0\nXRF;11.10.2026 11:40;0\n"

PARAMS = (
    "parameter_shortname;parameter_description_de;parameter_description_fr;"
    "parameter_description_it;parameter_description_en;parameter_group_de;"
    "parameter_granularity;parameter_decimals;parameter_datatype;parameter_unit\n"
    "tre200s0;Lufttemperatur;;;Air temperature 2 m above ground, current value;"
    "Temperatur;T;1;Float;°C\n"
    "gre000z0;Globalstrahlung;;;Global radiation, ten minutes mean;Strahlung;T;0;Integer;W/m²\n"
    "zzz999z0;Unused;;;Not reported by this station;X;T;0;Integer;x\n"
)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def test_latest_record_is_the_newest_timestamp_not_the_last_line():
    obs = smn.parse_latest(NOW_FILE)
    assert obs.measured_at == NEWEST
    assert obs.station == "TAE"
    assert obs.values["tre200s0"] == 12.3
    assert obs.values["gre000z0"] == 250.0
    assert obs.values["ods000z0"] == 245.0     # diffuse radiation, measured at TAE
    assert obs.values["oli000z0"] == 346.0


def test_every_parameter_column_is_kept_and_empty_fields_are_none():
    obs = smn.parse_latest(NOW_FILE)
    assert len(obs.values) == 32
    assert "station_abbr" not in obs.values and "reference_timestamp" not in obs.values
    assert obs.values["ppz850s0"] is None and obs.values["wcc006s0"] is None
    # never filled from an older record
    assert obs.values["dkl010z0"] == 264.0


def test_windows_1252_names_decode():
    raw = META.encode("cp1252")
    stations = smn.parse_stations(smn.decode(raw))
    assert {s.abbr: s.name for s in stations}["TAE"] == "Aadorf / Tänikon"
    assert smn.decode("Tänikon".encode("utf-8")) == "Tänikon"


def test_station_metadata_parses_with_empty_fields_kept():
    stations = {s.abbr: s for s in smn.parse_stations(META)}
    tae = stations["TAE"]
    assert (tae.latitude, tae.longitude, tae.elevation_m) == (47.479892, 8.904928, 539.0)


def test_stations_are_ranked_by_distance():
    ranked = smn.stations_by_distance(smn.parse_stations(META), *HOME)
    assert [m.abbr for m, _ in ranked][:3] == ["XRF", "TAE", "KLO"]
    tae_km = dict((m.abbr, d) for m, d in ranked)["TAE"]
    assert 7.5 < tae_km < 9.0


def test_parameter_descriptions_and_units():
    params = smn.parse_parameters(PARAMS)
    assert params["gre000z0"].unit == "W/m²"
    assert "Global radiation" in params["gre000z0"].description
    assert smn.parse_parameters("no;useful;columns\n1;2;3\n") == {}


def test_a_row_with_a_shifted_field_count_is_skipped_not_mislabelled():
    bad = PARAMS + "ods000z0;Diffus;;;Diffuse; ten minutes mean;Strahlung;T;0;Integer;W/m²\n"
    params = smn.parse_parameters(bad)
    assert "ods000z0" not in params
    assert params["gre000z0"].unit == "W/m²"


@pytest.mark.parametrize("text", ["", "a;b\n", "station_abbr;reference_timestamp;x\nTAE;garbage;1\n"])
def test_unusable_files_give_no_observation(text):
    assert smn.parse_latest(text) is None


def test_the_key_sensors_read_parameters_the_station_actually_reports():
    from swissweather_fusion.sensor import REFERENCE_VALUE_SENSORS

    header = HEADER.split(";")
    for spec in REFERENCE_VALUE_SENSORS:
        assert spec[1] in header, spec[0]


def test_now_url_and_host():
    assert smn.now_url("TAE") == (
        "https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/tae/ogd-smn_tae_t_now.csv"
    )


def test_client_refuses_other_hosts():
    client = smn.SmnClient(session=None)
    with pytest.raises(ValueError):
        asyncio.run(client.async_fetch_text("https://evil.example.com/x.csv"))
    with pytest.raises(ValueError):
        asyncio.run(client.async_fetch_text("http://data.geo.admin.ch/x.csv"))


class _Content:
    def __init__(self, data, chunk=1000):
        self._data, self._chunk = data, chunk

    async def iter_chunked(self, n):
        for i in range(0, len(self._data), self._chunk):
            yield self._data[i:i + self._chunk]


class _Response:
    def __init__(self, data):
        self.content = _Content(data)

    def raise_for_status(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, data):
        self._data = data

    def get(self, url, **kwargs):
        return _Response(self._data)


def test_client_reassembles_chunks_and_caps_size(monkeypatch):
    body = NOW_FILE.encode("cp1252")
    text = asyncio.run(smn.SmnClient(_Session(body)).async_fetch_text(smn.now_url("tae")))
    assert text == NOW_FILE
    monkeypatch.setattr(smn, "MAX_DOWNLOAD_BYTES", 100)
    with pytest.raises(ValueError):
        asyncio.run(smn.SmnClient(_Session(body)).async_fetch_text(smn.now_url("tae")))


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------
class FakeHass:
    def __init__(self):
        self.data = {}
        self.config = SimpleNamespace(latitude=HOME[0], longitude=HOME[1])

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class FakeClient:
    def __init__(self, files, fail=()):
        self.files = files
        self.fail = set(fail)
        self.calls = []

    async def async_fetch_text(self, url):
        self.calls.append(url)
        if url in self.fail:
            raise ConnectionError(f"down: {url}")
        if url not in self.files:
            raise ValueError(f"404 {url}")
        return self.files[url]


def _files(**extra):
    files = {
        smn.META_STATIONS_URL: META,
        smn.META_PARAMETERS_URL: PARAMS,
        smn.now_url("XRF"): RAIN_ONLY_FILE,
        smn.now_url("TAE"): NOW_FILE,
        smn.now_url("KLO"): NOW_FILE.replace("TAE;", "KLO;"),
    }
    files.update(extra)
    return files


def _coordinator(client, **kw):
    c = coord.ReferenceStationCoordinator(FakeHass(), *HOME, **kw)
    c._client = client
    return c


def test_auto_selection_skips_stations_without_radiation():
    client = FakeClient(_files())
    c = _coordinator(client)
    data = asyncio.run(c._async_update_data())
    assert data["station"]["abbr"] == "TAE"
    assert data["station"]["name"] == "Aadorf / Tänikon"
    assert data["station"]["selection"] == "nearest with radiation"
    assert 7.5 < data["station"]["distance_km"] < 9.0
    # the probe's download is reused: TAE's file fetched once on the first poll
    assert client.calls.count(smn.now_url("TAE")) == 1


def test_data_shape_and_parameter_metadata():
    c = _coordinator(FakeClient(_files()))
    data = asyncio.run(c._async_update_data())
    assert data["measured_at"] == NEWEST.isoformat()
    assert data["values"]["gre000z0"] == 250.0
    # descriptions only for parameters the station actually reported
    assert set(data["parameters"]) == {"tre200s0", "gre000z0"}
    assert data["parameters"]["gre000z0"]["unit"] == "W/m²"
    assert json.loads(json.dumps(data)) == data


def test_selection_is_kept_and_later_polls_fetch_one_file():
    client = FakeClient(_files())
    c = _coordinator(client)
    asyncio.run(c._async_update_data())
    client.calls.clear()
    asyncio.run(c._async_update_data())
    assert client.calls == [smn.now_url("TAE")]   # parameters cached for a day


def test_configured_station_is_used_even_without_metadata():
    client = FakeClient(_files(), fail={smn.META_STATIONS_URL})
    c = _coordinator(client, station_abbr="klo")
    data = asyncio.run(c._async_update_data())
    assert data["station"] == {"abbr": "KLO", "selection": "configured"}
    assert smn.now_url("XRF") not in client.calls


def test_no_station_with_radiation_nearby_fails_and_retries():
    from homeassistant.helpers.update_coordinator import UpdateFailed

    files = _files()
    files[smn.now_url("TAE")] = RAIN_ONLY_FILE
    files[smn.now_url("KLO")] = RAIN_ONLY_FILE
    client = FakeClient(files)
    c = _coordinator(client)
    with pytest.raises(UpdateFailed):
        asyncio.run(c._async_update_data())
    assert c.station is None
    assert c.health.consecutive_failures == 1
    # SBO (far away) is never probed
    assert smn.now_url("SBO") not in client.calls
    # next poll tries again and succeeds once TAE has radiation again
    files[smn.now_url("TAE")] = NOW_FILE
    data = asyncio.run(c._async_update_data())
    assert data["station"]["abbr"] == "TAE"
    assert c.health.consecutive_failures == 0


def test_a_failed_poll_records_health_and_raises():
    from homeassistant.helpers.update_coordinator import UpdateFailed

    client = FakeClient(_files())
    c = _coordinator(client)
    asyncio.run(c._async_update_data())
    client.fail = {smn.now_url("TAE")}
    with pytest.raises(UpdateFailed):
        asyncio.run(c._async_update_data())
    assert c.health.consecutive_failures == 1
    assert c.health.last_success_time is not None


def test_missing_parameter_descriptions_do_not_fail_the_poll():
    c = _coordinator(FakeClient(_files(), fail={smn.META_PARAMETERS_URL}))
    data = asyncio.run(c._async_update_data())
    assert data["values"]["gre000z0"] == 250.0
    assert data["parameters"] == {}


# ---------------------------------------------------------------------------
# Sensors
# ---------------------------------------------------------------------------
def _runtime(measured_at, values):
    data = {"station": {"abbr": "TAE"}, "measured_at": measured_at.isoformat(),
            "values": values, "parameters": {}}
    return {"reference_station_coordinator": SimpleNamespace(data=data)}


def _value_sensor(runtime, key):
    from swissweather_fusion.sensor import REFERENCE_VALUE_SENSORS, ReferenceValueSensor

    spec = next(s for s in REFERENCE_VALUE_SENSORS if s[0] == key)
    entity = object.__new__(ReferenceValueSensor)
    entity._runtime, entity._parameter = runtime, spec[1]
    return ReferenceValueSensor, entity


def test_value_sensor_reports_the_current_value():
    cls, entity = _value_sensor(
        _runtime(datetime.now(UTC) - timedelta(minutes=15), {"gre000z0": 250.0}),
        "global_radiation",
    )
    assert cls.native_value.fget(entity) == 250.0
    attrs = cls.extra_state_attributes.fget(entity)
    assert attrs["station"] == "TAE" and attrs["meteoswiss_parameter"] == "gre000z0"


def test_value_sensor_is_unknown_when_the_record_is_stale():
    cls, entity = _value_sensor(
        _runtime(datetime.now(UTC) - REFERENCE_STATION_MAX_AGE - timedelta(minutes=1),
                 {"gre000z0": 250.0}),
        "global_radiation",
    )
    assert cls.native_value.fget(entity) is None


def test_value_sensor_is_unknown_without_data_or_value():
    cls, entity = _value_sensor({}, "temperature")
    assert cls.native_value.fget(entity) is None
    cls, entity = _value_sensor(_runtime(datetime.now(UTC), {"tre200s0": None}), "temperature")
    assert cls.native_value.fget(entity) is None


def test_dataset_sensor_carries_everything_and_is_not_recorded():
    from swissweather_fusion.sensor import ReferenceStationSensor as S

    measured = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=12)
    entity = object.__new__(S)
    entity._runtime = _runtime(measured, smn.parse_latest(NOW_FILE).values)
    assert S.native_value.fget(entity) == measured
    attrs = S.extra_state_attributes.fget(entity)
    assert len(attrs["values"]) == 32
    assert attrs["stale"] is False and 11 <= attrs["age_minutes"] <= 13
    assert {"values", "parameters", "station"} <= S._unrecorded_attributes


def test_reference_station_has_health_telemetry():
    from swissweather_fusion.sensor import ALL_TELEMETRY_SOURCES

    assert SOURCE_REFERENCE_STATION in ALL_TELEMETRY_SOURCES
    assert SOURCE_HEALTH_OWNER[SOURCE_REFERENCE_STATION] == "reference_station_coordinator"


# ---------------------------------------------------------------------------
# Trial guard and lifecycle
# ---------------------------------------------------------------------------
def test_reference_station_never_reaches_learning_or_blend():
    assert SOURCE_REFERENCE_STATION not in ALL_FORECAST_SOURCES
    params = inspect.signature(coord.ReferenceStationCoordinator.__init__).parameters
    assert "db" not in params, "observation only: no database access at all"
    for cls in (coord.ModelABlendCoordinator, coord.ModelALearningCoordinator,
                coord.StationCoordinator):
        assert "reference_station" not in inspect.getsource(cls)
    assert coord.ModelABlendCoordinator.LEARNED_MEASUREMENTS == ("temperature", "humidity", "pressure")


def test_setup_registers_shuts_down_and_unloads_the_coordinator():
    import swissweather_fusion as integration

    setup = inspect.getsource(integration.async_setup_entry)
    # constructed, in the auth-failure cleanup, first-refreshed, kept
    # scheduled by a listener, in the platform-failure cleanup, in runtime
    assert setup.count("reference_station_coordinator") >= 6
    runtime = {"reference_station_coordinator": object()}
    assert integration._all_coordinators(runtime) == [runtime["reference_station_coordinator"]]


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("given,expected", [
    (None, ("", True)), ("", ("", True)), ("  ", ("", True)),
    ("tae", ("TAE", True)), (" Klo ", ("KLO", True)),
    ("TA", ("TA", False)), ("TAE1", ("TAE1", False)), ("T-E", ("T-E", False)),
    ("TÄE", ("TÄE", False)),
])
def test_reference_station_option_validation(given, expected):
    assert config_flow.normalize_reference_station(given) == expected


def test_option_has_label_and_error_text():
    import pathlib

    path = pathlib.Path(config_flow.__file__).parent / "translations" / "en.json"
    strings = json.loads(path.read_text(encoding="utf-8"))
    assert "reference_station" in strings["options"]["step"]["init"]["data"]
    assert "invalid_reference_station" in strings["options"]["error"]


def test_every_listenerless_coordinator_is_kept_scheduled():
    """**Scheduling, not cosmetics.** DataUpdateCoordinator stops
    rescheduling itself when it has no listener (v0.1.16 root cause).
    Every coordinator in the runtime dict except the blend (which has
    CoordinatorEntity listeners) must be in the _noop listener loop."""
    import re

    import swissweather_fusion as integration

    setup = inspect.getsource(integration.async_setup_entry)
    loop = re.search(r"for coordinator in \((.*?)\):\s*entry\.async_on_unload\("
                     r"coordinator\.async_add_listener\(_noop\)\)", setup, re.S)
    assert loop, "listener loop not found"
    listened = set(re.findall(r"(\w+_coordinator)", loop.group(1)))
    runtime_keys = set(re.findall(r'"(\w+_coordinator)":', setup))
    assert "reference_station_coordinator" in listened
    assert runtime_keys - {"blend_coordinator"} == listened
