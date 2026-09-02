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
import math
import re
from datetime import datetime, timedelta, timezone
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
    """A selected CombiPrecip file.

    ``valid_at`` is the product time parsed from the FILENAME, not the
    STAC feature's properties.datetime — see parse_stac_items_response
    for why the latter is a calendar date here and therefore useless as
    a scan timestamp. ``quality`` is MeteoSwiss's own quality code
    (0-9, 9 best), also from the filename.
    """

    href: str
    valid_at: datetime
    quality: Optional[int] = None


# ---------------------------------------------------------------------------
# CombiPrecip file-naming contract (v0.1.24, IND-13 / P1-16 / P1-17)
# ---------------------------------------------------------------------------
# MeteoSwiss documents the radar file naming convention as:
#
#     CPCyyjjjHHMMQ_nnnnn.XYZ.h5
#      |  | |  |  | |
#      |  | |  |  | +-- accumulation time in minutes, 5 digits (00060 = 1h)
#      |  | |  |  +---- quality code, single digit, 0-9 (9 = best)
#      |  | |  +------- HHMM, product time UTC
#      |  | +---------- jjj, day of year
#      |  +------------ yy, two-digit year
#      +--------------- product code
#
# Three facts about the collection make suffix-based selection unsafe,
# and all three are why this function was rewritten:
#
# 1. ch.meteoschweiz.ogd-radar-precip carries SEVERAL products side by
#    side. RZC (PRECIP, instantaneous mm/h), TZC (PRECIP-SV, also mm/h)
#    and CPC (CombiPrecip, 1-hour accumulation in mm) are all .h5 files
#    in the same collection. Accepting any href ending in .h5 meant this
#    client could and probably did download a product it was not written
#    to interpret.
#
# 2. STAC items here are per CALENDAR DATE, not per scan. Every asset
#    within one item shares the same properties.datetime, so sorting
#    assets by that field degenerates to "some arbitrary file from the
#    newest day" rather than "the latest scan". The real product time is
#    in the filename and nowhere else.
#
# 3. MeteoSwiss states that if the quality flag changes, the file name
#    changes and a SECOND file is produced rather than the first being
#    overwritten. Multiple valid files for one product time is therefore
#    the documented normal case — which is exactly why this function
#    must NOT treat "more than one .h5 asset" as an ambiguity error, as
#    was at one point proposed. That would convert a silent
#    wrong-product bug into a permanent hard outage of the radar source.
CPC_PRODUCT_CODE = "CPC"
CPC_ACCUMULATION_SEGMENT = "_00060"  # 60-minute accumulation
_CPC_FILENAME_RE = re.compile(
    r"^CPC(?P<yy>\d{2})(?P<jjj>\d{3})(?P<hhmm>\d{4})(?P<quality>\d)"
    r"_(?P<accum>\d{5})\."
)


def parse_cpc_filename(href: str) -> Optional[tuple[datetime, int, str]]:
    """Extract (product_time_utc, quality_code, accumulation) from a CPC href.

    Returns None for anything that is not a CombiPrecip file matching the
    documented convention — including RZC/TZC assets from the same
    collection, which is the point.
    """
    basename = href.rsplit("/", 1)[-1]
    match = _CPC_FILENAME_RE.match(basename)
    if match is None:
        return None
    try:
        year = 2000 + int(match.group("yy"))
        day_of_year = int(match.group("jjj"))
        hhmm = match.group("hhmm")
        hour = int(hhmm[:2])
        minute = int(hhmm[2:])
        if not (1 <= day_of_year <= 366) or hour > 23 or minute > 59:
            return None
        product_time = datetime(year, 1, 1, hour, minute, tzinfo=timezone.utc) + timedelta(
            days=day_of_year - 1
        )
    except (ValueError, TypeError):
        return None
    return product_time, int(match.group("quality")), match.group("accum")


def parse_stac_items_response(payload: dict[str, Any]) -> list[StacAsset]:
    """Parse the STAC /items listing into candidate CombiPrecip assets.

    Returns only genuine CPC 60-minute-accumulation files, sorted by the
    product time parsed from the FILENAME (newest first), with the
    highest quality code winning any tie at the same product time. The
    caller takes [0] for "latest".

    Assets that are not CPC, are a different accumulation window, or do
    not match the documented naming convention are skipped rather than
    raising — an unrecognised file alongside the ones we want is not an
    error, it is just not ours.
    """
    features = payload.get("features", [])
    assets: list[StacAsset] = []

    for feature in features:
        # Collection filter, when the feature declares one. Belt and
        # braces alongside the filename contract below.
        collection = feature.get("collection")
        if collection and collection != STAC_COLLECTION:
            continue

        for asset in feature.get("assets", {}).values():
            href = asset.get("href")
            if not href or not href.endswith(".h5"):
                continue
            parsed = parse_cpc_filename(href)
            if parsed is None:
                continue
            product_time, quality, accumulation = parsed
            if accumulation != CPC_ACCUMULATION_SEGMENT.lstrip("_"):
                continue
            assets.append(
                StacAsset(href=href, valid_at=product_time, quality=quality)
            )

    # Newest product time first; better quality first within a tie.
    assets.sort(key=lambda a: (a.valid_at, a.quality if a.quality is not None else -1), reverse=True)
    return assets


@dataclass(frozen=True)
class RadarPixelValue:
    """One sampled pixel.

    **v0.1.24 (P1-14)**: ``precip_rate_mmh`` renamed to
    ``precip_accum_mm_1h``. MeteoSwiss documents CPC as "Combiprecip
    60-minute total", unit mm, temporal aggregation "precipitation
    accumulation over 1 hour" — explicitly distinct from RZC/PRECIP,
    which is the instantaneous mm/h rate. The old name asserted a
    physical quantity this product does not report, and the detection
    threshold applied to it was chosen as if it were a rate.

    ``quality`` is MeteoSwiss's radar quality code for the file this
    pixel came from (0-9, 9 best), passed down from the filename by the
    caller. None means it could not be determined.
    """

    label: str
    precip_accum_mm_1h: Optional[float]
    valid_at: datetime
    quality: Optional[int] = None


def _pixel_indices(
    *, where_attrs: Any, easting: float, northing: float
) -> tuple[Optional[int], Optional[int], int, int]:
    """Shared row/col math, factored out so extract_values_at_points doesn't
    repeat the corner-reading/conversion for every point.

    **v0.1.24 fix (P1-18)**: this used to CLAMP an out-of-range (row, col)
    into the valid array bounds:

        col = max(0, min(xsize - 1, col))
        row = max(0, min(ysize - 1, row))

    which silently returned an unrelated edge pixel's value for a point
    genuinely outside the radar's coverage — entirely plausible for the
    farthest (70 km) upwind sampling point near the border, and
    indistinguishable downstream from a real reading at the intended
    location. Now returns (None, None, ...) so the caller can represent
    it honestly as "no data here".
    """
    # v0.1.27 fix (SWF-P2-003): validate the raster metadata before doing
    # arithmetic with it. A malformed or truncated HDF5 product could
    # otherwise produce a division by zero (zero extent), a silently
    # mirrored grid (inverted extent), or nonsense indices from
    # non-finite bounds. Rejecting the file as unusable is the honest
    # outcome; returning a plausible-looking pixel from a broken grid is
    # not.
    try:
        xsize = int(where_attrs["xsize"])
        ysize = int(where_attrs["ysize"])
        ll_lon = float(where_attrs["LL_lon"])
        ll_lat = float(where_attrs["LL_lat"])
        ur_lon = float(where_attrs["UR_lon"])
        ur_lat = float(where_attrs["UR_lat"])
    except (KeyError, TypeError, ValueError):
        return None, None, 0, 0

    if xsize <= 0 or ysize <= 0:
        return None, None, max(xsize, 0), max(ysize, 0)
    if not all(math.isfinite(v) for v in (ll_lon, ll_lat, ur_lon, ur_lat)):
        return None, None, xsize, ysize

    ll_easting, ll_northing = wgs84_to_lv95(latitude=ll_lat, longitude=ll_lon)
    ur_easting, ur_northing = wgs84_to_lv95(latitude=ur_lat, longitude=ur_lon)

    easting_extent = ur_easting - ll_easting
    northing_extent = ur_northing - ll_northing
    if easting_extent <= 0 or northing_extent <= 0:
        # Zero or inverted extent: the corners are not what they claim.
        return None, None, xsize, ysize

    # v0.1.27 fix (SWF-P1-002), CRITICAL for boundary correctness: the
    # containment test is performed on the CONTINUOUS coordinate, before
    # any conversion to an integer index.
    #
    # The v0.1.24 fix (P1-18) replaced edge-clamping with a bounds check,
    # but placed that check AFTER `int(...)`. Python's int() truncates
    # toward zero, so a point just outside the lower-left edge computes a
    # continuous column of, say, -0.1, becomes column 0, and then passes
    # `0 <= col < xsize` — returning the edge pixel it was meant to
    # reject. Only the two boundaries where the value goes negative were
    # affected (left and top); the right and bottom edges happened to be
    # caught, which is precisely the kind of partial correctness that
    # survives casual testing.
    #
    # The consequence is the same one P1-18 set out to eliminate: an
    # out-of-coverage sampling point silently yielding real-looking radar
    # data, and therefore a false storm signal.
    col_continuous = (easting - ll_easting) / easting_extent * xsize
    row_continuous = (ur_northing - northing) / northing_extent * ysize
    if not math.isfinite(col_continuous) or not math.isfinite(row_continuous):
        return None, None, xsize, ysize
    if not (0.0 <= col_continuous < xsize) or not (0.0 <= row_continuous < ysize):
        return None, None, xsize, ysize

    # floor() rather than int() so the conversion is direction-independent.
    # Inside the validated domain the two agree, but using floor makes the
    # code correct on its own terms rather than correct-by-precondition.
    return math.floor(row_continuous), math.floor(col_continuous), xsize, ysize


def extract_values_at_points(
    *, hdf5_path: str, points: list[SamplingPoint], quality: Optional[int] = None
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
            if row is None or col is None:
                # v0.1.24 (P1-18): genuinely outside the radar grid.
                # Reported with the same shape already used for the
                # file's own missing-data sentinel, so no downstream
                # consumer needs a new code path.
                results.append(
                    RadarPixelValue(
                        label=point.label,
                        precip_accum_mm_1h=None,
                        valid_at=valid_at,
                        quality=quality,
                    )
                )
                continue
            raw_value = data[row, col]

            if nodata is not None and raw_value == nodata:
                results.append(
                    RadarPixelValue(label=point.label, precip_accum_mm_1h=None, valid_at=valid_at, quality=quality)
                )
                continue
            if undetect is not None and raw_value == undetect:
                results.append(
                    RadarPixelValue(label=point.label, precip_accum_mm_1h=0.0, valid_at=valid_at, quality=quality)
                )
                continue

            value = float(raw_value) * gain + offset
            results.append(
                RadarPixelValue(label=point.label, precip_accum_mm_1h=value, valid_at=valid_at, quality=quality)
            )
        return results


class CombiPrecipClient:
    """Requires an aiohttp.ClientSession (HA's shared session).

    **v0.1.7 fix**: this used to write the downloaded file and clean up
    its temp directory directly inside an async method — HA's own
    loop-blocking detector caught both the `open(..., "wb")` write and
    the temp-directory cleanup's `scandir` call happening directly on the
    event loop. h5py itself has no async support at all regardless, so
    the fix is a clean split: `async_fetch_latest_bytes` does only the
    async STAC query + HTTP download (both genuinely non-blocking via
    aiohttp) and returns raw bytes; `write_temp_and_extract` does
    everything blocking (temp dir, file write, h5py parse, cleanup) as a
    single plain synchronous method, meant to be called via
    `hass.async_add_executor_job()` by the coordinator — the same pattern
    every other blocking operation in this project already uses.
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
        self._last_asset_quality: Optional[int] = None
        self._points = build_sampling_points(
            latitude=latitude,
            longitude=longitude,
            bearing_degrees=bearing_degrees,
            distances_km=distances_km,
            labels=labels,
        )

    async def async_fetch_latest_bytes(self) -> bytes:  # noqa: D401
        """Async-only: STAC query + HTTP download. No file I/O here at
        all — that's handled separately by write_temp_and_extract, which
        must be called via an executor job, not awaited directly.
        """
        import aiohttp

        # v0.1.14: no explicit timeout existed on either call here — same
        # fix as every other client, caught by an outside code review
        # checked directly against the source. This is arguably the most
        # important one to fix of the four: the second call downloads an
        # actual binary file, not just a small JSON response, so a
        # stalled connection here would hang the longest of any client in
        # this project. A longer allowance (60s) than the other clients'
        # 30s, since a genuine slow-but-working download of a real file
        # shouldn't get killed as if it were a stuck connection.
        async with self._session.get(
            STAC_ITEMS_URL,
            params={"limit": 1, "sortby": "-datetime"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            resp.raise_for_status()
            payload = await resp.json()

        assets = parse_stac_items_response(payload)
        if not assets:
            raise ValueError("No CombiPrecip assets found in STAC response")
        latest = assets[0]
        # v0.1.24 (P1-16): remember the quality code parsed from the
        # selected filename so write_temp_and_extract can stamp it onto
        # every pixel it produces. Instance state rather than a return
        # value because the async fetch and the blocking parse are
        # deliberately split across an executor-job boundary.
        self._last_asset_quality = latest.quality

        async with self._session.get(
            latest.href, timeout=aiohttp.ClientTimeout(total=60)
        ) as resp:
            resp.raise_for_status()
            return await resp.read()

    def write_temp_and_extract(self, data: bytes) -> list[RadarPixelValue]:
        """Synchronous — must only ever be called via
        hass.async_add_executor_job(), never awaited/called directly from
        async code. Handles the entire blocking sequence (temp directory,
        file write, h5py parse, cleanup) in one place.
        """
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            local_path = os.path.join(tmp_dir, "combiprecip_latest.h5")
            with open(local_path, "wb") as f:
                f.write(data)
            return extract_values_at_points(
                hdf5_path=local_path,
                points=self._points,
                quality=self._last_asset_quality,
            )
