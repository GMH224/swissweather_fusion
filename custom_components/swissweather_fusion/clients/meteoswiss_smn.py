"""MeteoSwiss SwissMetNet (SMN) 10-minute observations — reference station.

**v0.3.4.** Measured weather from the nearest MeteoSwiss automatic station,
exposed for downstream consumers (and, later, as a second reference
for verification). Measured global radiation,
sunshine and the sky's longwave radiation show what the forecasts get wrong
*now*, and air temperature and wind feed a panel-temperature model.

Source: MeteoSwiss open government data, collection ``ch.meteoschweiz.ogd-smn``
on ``data.geo.admin.ch`` — the same host this project already trusts for
CombiPrecip. Files used:

- ``ogd-smn_meta_stations.csv``   station list (abbreviation, name, WGS84, height)
- ``ogd-smn_meta_parameters.csv`` parameter descriptions and units
- ``<abbr>/ogd-smn_<abbr>_t_now.csv`` 10-minute data, recent period, updated
  every 10 minutes

All three are semicolon-separated and Windows-1252 encoded (station names
such as "Aadorf / Tänikon"). ``reference_timestamp`` is UTC, format
``dd.mm.yyyy HH:MM``; for 10-minute means and sums it marks the END of the
interval. Verified against live files by the owner's collector
(swf_collector.py, 10 Oct 2026): the TAE file carries 32 parameter columns.

Pure parsing functions are separate from the HTTP client so they can be
tested without a network.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

_LOGGER = logging.getLogger(__name__)

SMN_BASE_URL = "https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn"
META_STATIONS_URL = f"{SMN_BASE_URL}/ogd-smn_meta_stations.csv"
META_PARAMETERS_URL = f"{SMN_BASE_URL}/ogd-smn_meta_parameters.csv"
TRUSTED_HOST = "data.geo.admin.ch"
# The 10-minute "now" file is ~30 kB, the metadata files well under 1 MB.
# Anything far larger is not what we asked for.
MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024

TIMESTAMP_COLUMN = "reference_timestamp"
STATION_COLUMN = "station_abbr"
# The parameter a station must report to be chosen automatically:
# 10-minute global radiation.
REQUIRED_PARAMETER = "gre000z0"


def now_url(abbr: str) -> str:
    a = abbr.strip().lower()
    return f"{SMN_BASE_URL}/{a}/ogd-smn_{a}_t_now.csv"


def decode(raw: bytes) -> str:
    """MeteoSwiss serves Windows-1252. UTF-8 is tried first because the
    order matters: a Windows-1252 "ä" is the single byte 0xE4, which is
    invalid UTF-8, so a 1252 file falls through to the second decoder;
    decoding as 1252 first would never fail and would garble a file that
    ever switched to UTF-8."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252")


def split_csv(text: str) -> tuple[list[str], list[list[str]]]:
    """Header and rows of a semicolon CSV. Empty fields are KEPT — a
    missing measurement is an empty field, and collapsing it would shift
    every later column."""
    lines = [ln.rstrip("\r") for ln in text.split("\n") if ln.strip()]
    if not lines:
        return [], []
    header = [h.strip().lstrip("﻿") for h in lines[0].split(";")]
    rows = [ln.split(";") for ln in lines[1:]]
    return header, rows


def parse_timestamp(value: str) -> Optional[datetime]:
    try:
        return datetime.strptime(value.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


def _float(value: str) -> Optional[float]:
    value = value.strip()
    if value in ("", "-"):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class StationMeta:
    abbr: str
    name: str
    latitude: float
    longitude: float
    elevation_m: Optional[float]


def parse_stations(text: str) -> list[StationMeta]:
    header, rows = split_csv(text)
    try:
        i_abbr = header.index("station_abbr")
        i_name = header.index("station_name")
        i_lat = header.index("station_coordinates_wgs84_lat")
        i_lon = header.index("station_coordinates_wgs84_lon")
    except ValueError:
        return []
    i_elev = header.index("station_height_masl") if "station_height_masl" in header else None
    stations = []
    for p in rows:
        if len(p) <= max(i_abbr, i_name, i_lat, i_lon):
            continue
        lat, lon = _float(p[i_lat]), _float(p[i_lon])
        if lat is None or lon is None or not p[i_abbr].strip():
            continue
        elev = _float(p[i_elev]) if i_elev is not None and len(p) > i_elev else None
        stations.append(StationMeta(p[i_abbr].strip().upper(), p[i_name].strip(), lat, lon, elev))
    return stations


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def stations_by_distance(
    stations: list[StationMeta], latitude: float, longitude: float
) -> list[tuple[StationMeta, float]]:
    return sorted(
        ((s, distance_km(latitude, longitude, s.latitude, s.longitude)) for s in stations),
        key=lambda item: item[1],
    )


@dataclass(frozen=True)
class ParameterMeta:
    description: str
    unit: Optional[str]


def parse_parameters(text: str) -> dict[str, ParameterMeta]:
    """Parameter shortname -> English description and unit.

    Column names are located by content rather than position (the file's
    exact column set is not documented field by field); a file without a
    recognisable shortname column yields an empty mapping and the values
    are still published, just without descriptions.
    """
    header, rows = split_csv(text)
    lower = [h.lower() for h in header]

    def find(*needles: str) -> Optional[int]:
        for i, h in enumerate(lower):
            if all(n in h for n in needles):
                return i
        return None

    i_short = find("shortname")
    i_desc = find("description", "en")
    i_unit = find("unit")
    if i_short is None:
        return {}
    result: dict[str, ParameterMeta] = {}
    for p in rows:
        # A row with a different field count (e.g. a stray semicolon inside
        # a description) would put every later field under the wrong
        # heading — a description shown as a unit. Skip it instead.
        if len(p) != len(header):
            continue
        short = p[i_short].strip()
        if not short:
            continue
        desc = p[i_desc].strip() if i_desc is not None and len(p) > i_desc else ""
        unit = p[i_unit].strip() if i_unit is not None and len(p) > i_unit else ""
        result[short] = ParameterMeta(desc, unit or None)
    return result


@dataclass
class Observation:
    """The newest 10-minute record of one station."""

    station: str
    measured_at: datetime
    values: dict[str, Optional[float]] = field(default_factory=dict)


def _parse_rows(text: str) -> tuple[list[str], int, Optional[int], list[tuple[datetime, list[str]]]]:
    """Header, timestamp and station column indices, and every row with a
    parsable timestamp, in file order."""
    header, rows = split_csv(text)
    if TIMESTAMP_COLUMN not in header:
        return header, -1, None, []
    i_ts = header.index(TIMESTAMP_COLUMN)
    i_st = header.index(STATION_COLUMN) if STATION_COLUMN in header else None
    timed = []
    for p in rows:
        if len(p) <= i_ts:
            continue
        ts = parse_timestamp(p[i_ts])
        if ts is not None:
            timed.append((ts, p))
    return header, i_ts, i_st, timed


def _observation(header: list[str], i_ts: int, i_st: Optional[int],
                 ts: datetime, p: list[str]) -> Observation:
    values: dict[str, Optional[float]] = {}
    for i, name in enumerate(header):
        if i in (i_ts, i_st) or not name:
            continue
        values[name] = _float(p[i]) if i < len(p) else None
    station = p[i_st].strip().upper() if i_st is not None and i_st < len(p) else ""
    return Observation(station=station, measured_at=ts, values=values)


def parse_latest(text: str) -> Optional[Observation]:
    """The newest record in a 10-minute file, every parameter column.

    Uses the row with the latest timestamp (not the last line: order is
    not guaranteed). Parameters empty in that row are reported as None —
    never filled from an older row, so every value shares one timestamp.
    Of two rows with the same timestamp the first in the file is used.
    """
    header, i_ts, i_st, timed = _parse_rows(text)
    newest: Optional[tuple[datetime, list[str]]] = None
    for ts, p in timed:
        if newest is None or ts > newest[0]:
            newest = (ts, p)
    if newest is None:
        return None
    return _observation(header, i_ts, i_st, *newest)


def parse_records(text: str, window: timedelta) -> list[Observation]:
    """v0.3.5: every 10-minute record of the last `window`, oldest first.

    The "now" file holds every record from yesterday 12 UTC to the
    present (MeteoSwiss download documentation), so a record that a
    10-minute poll never saw as the newest one is still in it. The
    window is anchored on the newest record, not on the clock: the list
    always ends with the record `parse_latest` returns.

    Nothing is invented: a record absent from the file stays absent, an
    empty field is None. Rows off the 10-minute grid are skipped (none
    are expected in a 10-minute file). Of two rows with the same
    timestamp the first in the file is kept, the same rule as
    parse_latest, so the last entry always equals the `values` record.
    """
    header, i_ts, i_st, timed = _parse_rows(text)
    if not timed:
        return []
    newest = max(ts for ts, _ in timed)
    start = newest - window
    chosen: dict[datetime, list[str]] = {}
    for ts, p in timed:
        if ts <= start or ts.minute % 10 or ts in chosen:
            continue
        chosen[ts] = p
    return [_observation(header, i_ts, i_st, ts, chosen[ts]) for ts in sorted(chosen)]


def header_has(text: str, parameter: str) -> bool:
    header, _ = split_csv(text)
    return parameter in header


class SmnClient:
    """HTTP access, HTTPS to data.geo.admin.ch only, size-capped."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def async_fetch_text(self, url: str) -> str:
        import aiohttp

        if not url.startswith(f"https://{TRUSTED_HOST}/"):
            raise ValueError(f"refusing to fetch from an untrusted URL: {url}")
        chunks: list[bytes] = []
        total = 0
        async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as response:
            response.raise_for_status()
            # Read in chunks: a single read(n) may return less than the
            # whole body, and an unbounded read would let an unexpected
            # response exhaust memory.
            async for chunk in response.content.iter_chunked(64 * 1024):
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise ValueError(f"response from {url} exceeds {MAX_DOWNLOAD_BYTES} bytes")
                chunks.append(chunk)
        return decode(b"".join(chunks))
