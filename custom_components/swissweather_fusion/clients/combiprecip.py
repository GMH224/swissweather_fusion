"""MeteoSwiss CombiPrecip radar precipitation client.

**Read this before trusting this file.** Everything else in this project
was verified against a real live response before being relied on (meteoblue
via an actual test call, MeteoSwiss/Open-Meteo docs quoted directly, SRF's
API shape from official documentation). This client is the one exception:
I do not have network access to data.geo.admin.ch from the environment
that built this, so the HDF5 group/dataset layout below is based on the
ODIM_H5 format (the standard European weather radar HDF5 format used
across the EUMETNET OPERA network, which MeteoSwiss participates in), not
a downloaded file. Treat `_extract_value_at_point` especially as a
best-effort starting point — verify the actual group names, the
gain/offset/nodata attributes, and the nodata sentinel value against a
real downloaded file before trusting the numbers this returns. This is
flagged in DEVELOPER.md and the plan doc as the one piece of real
verification work still outstanding.

Coordinate handling is the one part of this file with higher confidence:
WGS84 -> LV95 (EPSG:2056) is a standard, well-defined transform via pyproj,
and reading the grid's actual geographic extent from the file's own
metadata (rather than hardcoding assumed grid dimensions) means the pixel
lookup should be correct even if some ODIM group names above turn out
wrong — worth fixing incrementally against a real file rather than
guessing further.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

STAC_COLLECTION = "ch.meteoschweiz.ogd-radar-precip"
STAC_ITEMS_URL = (
    f"https://data.geo.admin.ch/api/stac/v1/collections/{STAC_COLLECTION}/items"
)

# WGS84 (lat/lon) -> Swiss LV95, standard EPSG codes, not project-specific.
WGS84_EPSG = "EPSG:4326"
LV95_EPSG = "EPSG:2056"


def wgs84_to_lv95(*, latitude: float, longitude: float) -> tuple[float, float]:
    """Returns (easting, northing) in meters. Requires pyproj (manifest.json
    requirement). Kept as a thin, separately-testable function so the
    coordinate math can be verified in isolation from HDF5 parsing.
    """
    from pyproj import Transformer

    transformer = Transformer.from_crs(WGS84_EPSG, LV95_EPSG, always_xy=True)
    easting, northing = transformer.transform(longitude, latitude)
    return easting, northing


def compute_upwind_point(
    *, latitude: float, longitude: float, bearing_degrees: float, distance_km: float
) -> tuple[float, float]:
    """Geodesic destination point: given a start point, a bearing, and a
    distance, returns (latitude, longitude) of the point that far away in
    that direction. Uses pyproj's Geod for a proper geodesic calculation
    (not flat-earth trig) even though these distances are small enough
    that the difference would be negligible either way.
    """
    from pyproj import Geod

    geod = Geod(ellps="WGS84")
    dest_lon, dest_lat, _ = geod.fwd(
        longitude, latitude, bearing_degrees, distance_km * 1000.0
    )
    return dest_lat, dest_lon


@dataclass(frozen=True)
class SamplingPoint:
    label: str  # 'local' | 'near' | 'mid' | 'far'
    latitude: float
    longitude: float
    easting: float
    northing: float


def build_sampling_points(
    *,
    latitude: float,
    longitude: float,
    bearing_degrees: float,
    distances_km: tuple[float, ...],
    labels: tuple[str, ...],
) -> list[SamplingPoint]:
    """The local point plus one upwind point per configured distance.

    Computing all of these costs nothing extra over the single-point
    version — it's the same downloaded HDF5 grid, just indexed 4 times
    instead of once. See DEVELOPER.md ("Upwind radar sampling").
    """
    local_easting, local_northing = wgs84_to_lv95(latitude=latitude, longitude=longitude)
    points = [
        SamplingPoint(
            label="local",
            latitude=latitude,
            longitude=longitude,
            easting=local_easting,
            northing=local_northing,
        )
    ]
    for label, distance_km in zip(labels, distances_km):
        dest_lat, dest_lon = compute_upwind_point(
            latitude=latitude,
            longitude=longitude,
            bearing_degrees=bearing_degrees,
            distance_km=distance_km,
        )
        easting, northing = wgs84_to_lv95(latitude=dest_lat, longitude=dest_lon)
        points.append(
            SamplingPoint(
                label=label,
                latitude=dest_lat,
                longitude=dest_lon,
                easting=easting,
                northing=northing,
            )
        )
    return points


@dataclass(frozen=True)
class StacAsset:
    href: str
    valid_at: datetime


def parse_stac_items_response(payload: dict[str, Any]) -> list[StacAsset]:
    """Parse the STAC /items listing to find the most recent asset href.

    Returns items sorted newest-first; caller takes [0] for "latest".
    """
    features = payload.get("features", [])
    assets: list[StacAsset] = []
    for feature in features:
        properties = feature.get("properties", {})
        datetime_str = properties.get("datetime")
        if not datetime_str:
            continue
        valid_at = datetime.fromisoformat(datetime_str.replace("Z", "+00:00"))
        feature_assets = feature.get("assets", {})
        for asset in feature_assets.values():
            href = asset.get("href")
            if href and href.endswith(".h5"):
                assets.append(StacAsset(href=href, valid_at=valid_at))
    assets.sort(key=lambda a: a.valid_at, reverse=True)
    return assets


@dataclass(frozen=True)
class RadarPixelValue:
    label: str
    precip_rate_mmh: Optional[float]
    valid_at: datetime


def _pixel_indices(
    *, where_attrs: Any, easting: float, northing: float
) -> tuple[int, int, int, int]:
    """Shared row/col math, factored out so extract_values_at_points doesn't
    repeat the corner-reading/conversion for every point.
    """
    xsize = int(where_attrs["xsize"])
    ysize = int(where_attrs["ysize"])
    ll_lon = float(where_attrs["LL_lon"])
    ll_lat = float(where_attrs["LL_lat"])
    ur_lon = float(where_attrs["UR_lon"])
    ur_lat = float(where_attrs["UR_lat"])

    ll_easting, ll_northing = wgs84_to_lv95(latitude=ll_lat, longitude=ll_lon)
    ur_easting, ur_northing = wgs84_to_lv95(latitude=ur_lat, longitude=ur_lon)

    col = int((easting - ll_easting) / (ur_easting - ll_easting) * xsize)
    row = int((ur_northing - northing) / (ur_northing - ll_northing) * ysize)
    col = max(0, min(xsize - 1, col))
    row = max(0, min(ysize - 1, row))
    return row, col, xsize, ysize


def extract_values_at_points(
    *, hdf5_path: str, points: list[SamplingPoint]
) -> list[RadarPixelValue]:
    """Extract one pixel per sampling point from a single opened file.

    Opening the file once and indexing it N times (rather than opening it
    N times) is the whole reason the 4-point upwind sampling is nearly free
    — the expensive part (download + parse) happens once regardless of how
    many points are sampled from the resulting grid.

    **Best-effort against the ODIM_H5 standard, not yet verified against a
    real MeteoSwiss file** — see the module docstring. The structure
    assumed here:
      /where            attrs: LL_lon, LL_lat, UR_lon, UR_lat, xsize, ysize
      /dataset1/data1/data       the 2D array (ysize x xsize)
      /dataset1/data1/what      attrs: gain, offset, nodata, undetect
      /what             attrs: date, time (scan validity)
    """
    import h5py

    with h5py.File(hdf5_path, "r") as f:
        where = f["where"].attrs
        dataset = f["dataset1"]["data1"]
        data = dataset["data"]
        what = dataset["what"].attrs
        gain = float(what.get("gain", 1.0))
        offset = float(what.get("offset", 0.0))
        nodata = what.get("nodata")
        undetect = what.get("undetect")

        what_root = f["what"].attrs
        date_str = what_root.get("date")
        time_str = what_root.get("time")
        valid_at = datetime.now(timezone.utc)
        if date_str and time_str:
            date_str = date_str.decode() if isinstance(date_str, bytes) else date_str
            time_str = time_str.decode() if isinstance(time_str, bytes) else time_str
            valid_at = datetime.strptime(date_str + time_str, "%Y%m%d%H%M%S").replace(
                tzinfo=timezone.utc
            )

        results: list[RadarPixelValue] = []
        for point in points:
            row, col, _, _ = _pixel_indices(
                where_attrs=where, easting=point.easting, northing=point.northing
            )
            raw_value = data[row, col]

            if nodata is not None and raw_value == nodata:
                results.append(
                    RadarPixelValue(label=point.label, precip_rate_mmh=None, valid_at=valid_at)
                )
                continue
            if undetect is not None and raw_value == undetect:
                results.append(
                    RadarPixelValue(label=point.label, precip_rate_mmh=0.0, valid_at=valid_at)
                )
                continue

            value = float(raw_value) * gain + offset
            results.append(
                RadarPixelValue(label=point.label, precip_rate_mmh=value, valid_at=valid_at)
            )
        return results


class CombiPrecipClient:
    """Requires an aiohttp.ClientSession (HA's shared session) and a
    writable temp directory for the downloaded HDF5 file (deleted after
    each poll — this client doesn't keep raw radar files around, only the
    extracted pixel values go into storage/db.py).
    """

    def __init__(
        self,
        session: Any,
        latitude: float,
        longitude: float,
        *,
        bearing_degrees: float,
        distances_km: tuple[float, ...],
        labels: tuple[str, ...],
    ) -> None:
        self._session = session
        self._points = build_sampling_points(
            latitude=latitude,
            longitude=longitude,
            bearing_degrees=bearing_degrees,
            distances_km=distances_km,
            labels=labels,
        )

    async def async_fetch_latest_values(self, *, tmp_dir: str) -> list[RadarPixelValue]:
        import os

        async with self._session.get(
            STAC_ITEMS_URL, params={"limit": 1, "sortby": "-datetime"}
        ) as resp:
            resp.raise_for_status()
            payload = await resp.json()

        assets = parse_stac_items_response(payload)
        if not assets:
            raise ValueError("No CombiPrecip assets found in STAC response")
        latest = assets[0]

        local_path = os.path.join(tmp_dir, "combiprecip_latest.h5")
        async with self._session.get(latest.href) as resp:
            resp.raise_for_status()
            with open(local_path, "wb") as f:
                f.write(await resp.read())

        try:
            return extract_values_at_points(hdf5_path=local_path, points=self._points)
        finally:
            os.remove(local_path)
