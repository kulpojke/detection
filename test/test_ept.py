"""Network tests against a real EPT, given with ``pytest --ept NAME``. The test site comes
from the ``site`` fixture (conftest.py), so nothing here is tied to one location. Reads are
kept small (well under 1 km^2 at full density); run under a memory cap all the same."""
import csv
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

import example_peaks as ex
import tilestream as ts

pytestmark = pytest.mark.network
SRC = Path(__file__).parents[1] / "src"


def small_box(site, size=100.0):
    cx, cy = ts._centre(site.area)
    return cx - size / 2, cy - size / 2, cx + size / 2, cy + size / 2


def test_info(ept, site):
    info = ts.ept_info(ept)
    assert info["points"] > 0 and {"X", "Y", "Z"} <= {d["name"] for d in info["schema"]}
    lon0, lat0, lon1, lat1 = ts.footprint(ept)
    assert -180 <= lon0 < lon1 <= 180 and -90 <= lat0 < lat1 <= 90
    assert any(b[0] <= site.area[0] and site.area[2] <= b[2] + 1000 for b in ts.occupied_boxes(ept, site.srs))


def test_fetch_small_bbox(ept, site):
    box = small_box(site)
    t = time.time()
    a = ts.fetch_points(ept, box, site.srs)
    print(f"{len(a):,} pts ({len(a) / 1e4:.1f}/m^2) in {time.time() - t:.1f}s")
    assert len(a) > 1e4  # > 1 pt/m^2
    assert {"X", "Y", "Z", "Classification", "ReturnNumber"} <= set(a.dtype.names)
    assert a["X"].min() >= box[0] and a["X"].max() < box[2]
    assert a["Y"].min() >= box[1] and a["Y"].max() < box[3]


def test_fetch_dims_keeps_only_requested_fields(ept, site):
    a = ts.fetch_points(ept, small_box(site), site.srs, dims=("X", "Y", "Z"))
    assert a.dtype.names == ("X", "Y", "Z") and a.dtype.itemsize == 24


def test_fetch_resolution_thins(ept, site):
    n = {res: len(ts.fetch_points(ept, site.area, site.srs, dims=("X", "Y", "Z"), resolution=res))
         for res in (None, 2.0, 8.0)}
    print({k: f"{v / 250_000:.2f} pts/m^2" for k, v in n.items()})
    assert n[None] > n[2.0] > n[8.0] > 0
    assert n[8.0] < n[None] / 10


def test_fetch_big_window_thinned(ept, site):
    """The coarse whole-area use case: 3 km x 3 km at 8 m, XYZ only, fits in little memory."""
    cx, cy = ts._centre(site.area)
    t = time.time()
    a = ts.fetch_points(ept, (cx - 1500, cy - 1500, cx + 1500, cy + 1500), site.srs, dims=("X", "Y", "Z"),
                        resolution=8.0)
    print(f"{len(a) / 1e6:.2f} Mpts ({len(a) / 9e6:.3f} pts/m^2) in {time.time() - t:.1f}s")
    assert 0 < len(a) < 5e6


def test_seam_python_analysis(ept, site, tmp_path):
    """Four 250 m windows vs one 500 m window: same DSM pixels, same peaks."""
    from osgeo import gdal
    windows = ts.grid_windows([site.area], 250, ex.HALO)
    assert len(windows) == 4
    ts.run(windows, ex.analyze, tmp_path, ept=ept, srs=site.srs)
    ts.merge(tmp_path)

    big, = ts.bbox_windows([site.area], ex.HALO)
    res = ex.analyze(ts.fetch_points(ept, big.halo, site.srs), big)
    ref = ts.crop_raster(res["dsm"], big.core).data
    mosaic = gdal.Open(str(tmp_path / "dsm.vrt")).ReadAsArray()
    assert mosaic.shape == ref.shape == (500, 500)
    np.testing.assert_array_equal(mosaic, ref)
    assert np.isfinite(mosaic).mean() > 0.9  # the site really is full of data

    keep = big.in_core(res["peaks"]["x"], res["peaks"]["y"])
    with np.load(tmp_path / "peaks.npz") as pk:
        peaks = set(zip(pk["x"], pk["y"]))
    print(f"{len(peaks)} peaks")
    assert peaks == set(zip(res["peaks"]["x"][keep], res["peaks"]["y"][keep]))


def cli(*args, cwd):
    subprocess.run([sys.executable, str(SRC / "tilestream.py"), *map(str, args)], check=True, cwd=cwd)


def test_cli_exec_seam(ept, site, tmp_path):
    """example_dsm.sh via --exec: 250 m grid over the site vs the site as one bbox window."""
    from osgeo import gdal
    area = [f"{v:.3f}" for v in site.area]
    cli("run", tmp_path / "grid", "--ept", ept, "--bbox", *area, "--bbox-crs", site.srs, "--core", 250,
        "--halo", 5, "--exec", SRC / "example_dsm.sh", cwd=tmp_path)
    cli("merge", tmp_path / "grid", cwd=tmp_path)
    (tmp_path / "site.csv").write_text("name,xmin,ymin,xmax,ymax\nsite," + ",".join(area) + "\n")
    cli("run", tmp_path / "one", "--ept", ept, "--bboxes", tmp_path / "site.csv", "--bbox-crs", site.srs,
        "--halo", 5, "--exec", SRC / "example_dsm.sh", cwd=tmp_path)

    mosaic = gdal.Open(str(tmp_path / "grid" / "dsm.vrt")).ReadAsArray()
    single = gdal.Open(str(tmp_path / "one" / "dsm" / "site.tif")).ReadAsArray()
    assert mosaic.shape == single.shape == (500, 500)
    np.testing.assert_array_equal(mosaic, single)
    assert (mosaic != -9999).mean() > 0.9  # the site really is full of data
    with open(tmp_path / "grid" / "info.csv") as f:
        assert len(list(csv.DictReader(f))) == 4


def test_cli_python_analysis_lonlat_sites(ept, site, tmp_path):
    """--analysis with lon/lat bboxes (the default --bbox-crs); srs is picked automatically."""
    from osgeo import gdal
    lon0, lat0, lon1, lat1 = site.lonlat
    dx, dy = (lon1 - lon0) / 4, (lat1 - lat0) / 4
    (tmp_path / "sites.csv").write_text(
        "name,xmin,ymin,xmax,ymax\n"
        f"south west,{lon0},{lat0},{lon0 + dx},{lat0 + dy}\n"
        f"north east,{lon1 - dx},{lat1 - dy},{lon1},{lat1}\n")
    out = tmp_path / "out"
    cli("run", out, "--ept", ept, "--bboxes", tmp_path / "sites.csv", "--analysis", SRC / "example_peaks.py",
        cwd=tmp_path)
    cli("merge", out, cwd=tmp_path)
    assert (out / "_run.json").read_text().count(site.srs) == 1
    with open(out / "stats.csv") as f:
        stats = list(csv.DictReader(f))
    assert sorted(r["window"] for r in stats) == ["north_east", "south_west"]
    for r in stats:
        assert np.isfinite(float(r["max_z"]))
        ds = gdal.Open(str(out / "dsm" / f"{r['window']}.tif"))
        x0, res, _, ytop, _, _ = ds.GetGeoTransform()
        assert x0 <= float(r["xmin"]) and x0 + ds.RasterXSize * res >= float(r["xmax"])
        assert ytop >= float(r["ymax"]) and ytop - ds.RasterYSize * res <= float(r["ymin"])


def test_plan_all_covers_site(ept, site):
    windows, srs = ts.plan(ept, 20, everything=True, core=1000, srs=site.srs)
    cx, cy = ts._centre(site.area)
    assert sum(bool(w.in_core(cx, cy)) for w in windows) == 1
    print(f"--all: {len(windows)} windows of 1 km")
