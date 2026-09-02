import os
import tempfile

import h5py
import numpy as np
import pytest
from pyproj import Geod

from swissweather_fusion.clients import combiprecip as cp

TEST_LAT, TEST_LON = 46.9480, 7.4474


def test_wgs84_to_lv95_sane_magnitude():
    easting, northing = cp.wgs84_to_lv95(latitude=TEST_LAT, longitude=TEST_LON)
    # Switzerland's LV95 easting/northing both fall in these broad ranges
    assert 2_400_000 < easting < 2_800_000
    assert 1_000_000 < northing < 1_300_000


def test_compute_upwind_point_moves_southwest():
    dest_lat, dest_lon = cp.compute_upwind_point(
        latitude=TEST_LAT, longitude=TEST_LON, bearing_degrees=225.0, distance_km=30.0
    )
    assert dest_lat < TEST_LAT
    assert dest_lon < TEST_LON


def test_compute_upwind_point_distance_accuracy():
    dest_lat, dest_lon = cp.compute_upwind_point(
        latitude=TEST_LAT, longitude=TEST_LON, bearing_degrees=225.0, distance_km=30.0
    )
    geod = Geod(ellps="WGS84")
    _, _, distance_m = geod.inv(TEST_LON, TEST_LAT, dest_lon, dest_lat)
    assert abs(distance_m - 30000) < 50  # within 50m of the requested 30km


def test_build_sampling_points():
    points = cp.build_sampling_points(
        latitude=TEST_LAT, longitude=TEST_LON, bearing_degrees=225.0,
        distances_km=(30.0, 45.0, 70.0), labels=("near", "mid", "far"),
    )
    assert len(points) == 4
    assert points[0].label == "local"
    assert [p.label for p in points[1:]] == ["near", "mid", "far"]


def test_parse_stac_items_response_sorts_newest_first():
    """v0.1.24 (IND-13): this test previously used invented filenames
    ("old.h5" / "new.h5") and asserted that sorting by the STAC feature's
    properties.datetime picked the newer one. Both halves of that were
    wrong, which is precisely why the bug survived: MeteoSwiss items are
    per CALENDAR DATE, so properties.datetime is a date and every asset
    in an item shares it, and real filenames follow a documented
    convention that carries the actual product time. Rewritten against
    that convention.
    """
    payload = {
        "features": [
            {
                "collection": cp.STAC_COLLECTION,
                "properties": {"datetime": "2026-07-25T00:00:00Z"},
                "assets": {
                    "old": {"href": "https://example.com/CPC2620612009_00060.801.h5"},
                    "new": {"href": "https://example.com/CPC2620612059_00060.801.h5"},
                },
            }
        ]
    }
    assets = cp.parse_stac_items_response(payload)
    assert assets[0].href.endswith("CPC2620612059_00060.801.h5")
    # Product time comes from the filename, not from properties.datetime.
    assert assets[0].valid_at.hour == 12 and assets[0].valid_at.minute == 5
    assert assets[0].quality == 9


@pytest.fixture
def synthetic_odim_file():
    """A small, deliberately synthetic ODIM_H5-shaped file (not real
    MeteoSwiss data) with a known marker value at a known grid cell, used
    to verify the pixel-lookup math independent of whether MeteoSwiss's
    actual file layout matches — see combiprecip.py's module docstring for
    why that layout is unverified.
    """
    path = tempfile.mktemp(suffix=".h5")
    with h5py.File(path, "w") as f:
        where = f.create_group("where")
        where.attrs["xsize"] = 10
        where.attrs["ysize"] = 10
        where.attrs["LL_lon"] = 7.0
        where.attrs["LL_lat"] = 46.5
        where.attrs["UR_lon"] = 8.0
        where.attrs["UR_lat"] = 47.5

        what_root = f.create_group("what")
        what_root.attrs["date"] = "20260725"
        what_root.attrs["time"] = "120000"

        dataset1 = f.create_group("dataset1")
        data1 = dataset1.create_group("data1")
        arr = np.zeros((10, 10), dtype="float32")
        arr[2, 3] = 42.0
        data1.create_dataset("data", data=arr)
        what = data1.create_group("what")
        what.attrs["gain"] = 1.0
        what.attrs["offset"] = 0.0
        what.attrs["nodata"] = -999.0
        what.attrs["undetect"] = -1.0
    yield path
    os.remove(path)


def test_extract_values_at_points_finds_marker_cell(synthetic_odim_file):
    lon_at_col3 = 7.0 + (3 + 0.5) / 10.0 * (8.0 - 7.0)
    lat_at_row2 = 47.5 - (2 + 0.5) / 10.0 * (47.5 - 46.5)
    easting, northing = cp.wgs84_to_lv95(latitude=lat_at_row2, longitude=lon_at_col3)
    point = cp.SamplingPoint(label="test", latitude=lat_at_row2, longitude=lon_at_col3, easting=easting, northing=northing)

    results = cp.extract_values_at_points(hdf5_path=synthetic_odim_file, points=[point])

    assert results[0].precip_accum_mm_1h == 42.0
    assert results[0].valid_at.year == 2026
    assert results[0].valid_at.hour == 12


def test_write_temp_and_extract_handles_bytes_end_to_end(synthetic_odim_file):
    """v0.1.7 regression test: production logs showed HA's own
    loop-blocking detector catching the file write and temp-directory
    cleanup happening directly inside an async method (the same class of
    bug fixed for weather.py in v0.1.5, in different code this time).
    Confirms the replacement — a plain synchronous method meant to be
    called via an executor job, taking raw bytes rather than doing any
    async I/O of its own — still produces the correct extracted values
    end to end, and doesn't leave the temp file behind afterward.
    """
    with open(synthetic_odim_file, "rb") as f:
        raw_bytes = f.read()

    lon_at_col3 = 7.0 + (3 + 0.5) / 10.0 * (8.0 - 7.0)
    lat_at_row2 = 47.5 - (2 + 0.5) / 10.0 * (47.5 - 46.5)

    client = cp.CombiPrecipClient(
        session=None,  # not used by write_temp_and_extract at all
        latitude=lat_at_row2,
        longitude=lon_at_col3,
        bearing_degrees=225.0,
        distances_km=(30.0,),
        labels=("near",),
    )
    results = client.write_temp_and_extract(raw_bytes)

    local_result = next(r for r in results if r.label == "local")
    assert local_result.precip_accum_mm_1h == 42.0


def test_extract_values_at_points_nodata_sentinel():
    path = tempfile.mktemp(suffix=".h5")
    with h5py.File(path, "w") as f:
        where = f.create_group("where")
        where.attrs["xsize"] = 2
        where.attrs["ysize"] = 2
        where.attrs["LL_lon"] = 7.0
        where.attrs["LL_lat"] = 46.5
        where.attrs["UR_lon"] = 8.0
        where.attrs["UR_lat"] = 47.5
        what_root = f.create_group("what")
        what_root.attrs["date"] = "20260725"
        what_root.attrs["time"] = "120000"
        dataset1 = f.create_group("dataset1")
        data1 = dataset1.create_group("data1")
        arr = np.array([[-999.0, -1.0], [5.0, 5.0]], dtype="float32")
        data1.create_dataset("data", data=arr)
        what = data1.create_group("what")
        what.attrs["gain"] = 1.0
        what.attrs["offset"] = 0.0
        what.attrs["nodata"] = -999.0
        what.attrs["undetect"] = -1.0

    try:
        easting, northing = cp.wgs84_to_lv95(latitude=47.4, longitude=7.1)
        point = cp.SamplingPoint(label="nd", latitude=47.4, longitude=7.1, easting=easting, northing=northing)
        results = cp.extract_values_at_points(hdf5_path=path, points=[point])
        assert results[0].precip_accum_mm_1h is None
    finally:
        os.remove(path)


# ---------------------------------------------------------------------------
# v0.1.24 additions
# ---------------------------------------------------------------------------
def test_extract_values_at_points_returns_none_for_out_of_grid_point(
    synthetic_odim_file,
):
    """v0.1.24 fix (P1-18). _pixel_indices used to CLAMP an out-of-range
    (row, col) into the valid array bounds, so a sampling point genuinely
    outside the radar's coverage silently returned an unrelated EDGE
    pixel's value — indistinguishable downstream from a real reading at
    the intended location, and entirely plausible for the farthest
    (70 km) upwind point near the border.

    Reported now with the same shape already used for the file's own
    missing-data sentinel, so no downstream consumer needs a new path.
    """
    far_outside = cp.SamplingPoint(
        label="far", latitude=0.0, longitude=0.0,
        easting=-5_000_000.0, northing=-5_000_000.0,
    )
    values = cp.extract_values_at_points(
        hdf5_path=synthetic_odim_file, points=[far_outside]
    )
    assert len(values) == 1
    assert values[0].precip_accum_mm_1h is None


def test_extract_values_at_points_in_grid_point_still_works(synthetic_odim_file):
    """Sanity check alongside the boundary change above: an in-bounds
    corner point must still resolve, which is what would catch an
    off-by-one introduced by the new bounds test."""
    import h5py as _h5py

    with _h5py.File(synthetic_odim_file, "r") as f:
        where = f["where"].attrs
        ll_e, ll_n = cp.wgs84_to_lv95(
            latitude=float(where["LL_lat"]) + 0.01,
            longitude=float(where["LL_lon"]) + 0.01,
        )
    corner = cp.SamplingPoint(
        label="local", latitude=0.0, longitude=0.0, easting=ll_e, northing=ll_n
    )
    values = cp.extract_values_at_points(
        hdf5_path=synthetic_odim_file, points=[corner]
    )
    assert values[0].precip_accum_mm_1h is not None


def test_extract_values_at_points_stamps_the_quality_code(synthetic_odim_file):
    """v0.1.24 (P1-16): the quality code comes from the CPC FILENAME,
    which is always present, rather than from the optional ODIM
    quality1/data1 sub-group, which is optional even within the spec and
    may never be populated in practice."""
    point = cp.SamplingPoint(
        label="local", latitude=0.0, longitude=0.0,
        easting=2_600_000.0, northing=1_200_000.0,
    )
    values = cp.extract_values_at_points(
        hdf5_path=synthetic_odim_file, points=[point], quality=7
    )
    assert values[0].quality == 7


def test_extract_values_at_points_quality_is_none_when_not_supplied(
    synthetic_odim_file,
):
    point = cp.SamplingPoint(
        label="local", latitude=0.0, longitude=0.0,
        easting=2_600_000.0, northing=1_200_000.0,
    )
    values = cp.extract_values_at_points(
        hdf5_path=synthetic_odim_file, points=[point]
    )
    assert values[0].quality is None
