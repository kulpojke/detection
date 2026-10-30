"""Offline tests: window planning, cropping, and run/merge/resume/exec on synthetic points.
Coordinates are arbitrary metric numbers; no EPT or network is used."""
import csv
import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest

import example_peaks as ex
import tilestream as ts

SRS = "EPSG:6340"  # only a label on the synthetic outputs
SRC = Path(__file__).parents[1] / "src"
DSM_SH = str(SRC / "example_dsm.sh")


# --------------------------------------------------------------------------- area of interest

def test_load_tiles(tmp_path):
    p = tmp_path / "list.txt"
    p.write_text("https://x/y/USGS_LPC_A_B_11TMN470250.laz\nhttps://x/y/USGS_LPC_A_B_11TMP471300.laz\n")
    t = ts.load_tiles(p)
    assert [(x.zone, x.xmin, x.ymin) for x in t] == [(11, 470000, 5250000), (11, 471000, 5300000)]
    assert t[0].bounds == (470000, 5250000, 471000, 5251000)


@pytest.mark.parametrize("names,err", [("not_a_tile.laz", "unrecognised"),
                                       ("a_11TMN470250.laz b_10TMN470250.laz", "several UTM zones")])
def test_load_tiles_errors(tmp_path, names, err):
    p = tmp_path / "list.txt"
    p.write_text(names)
    with pytest.raises(ValueError, match=err):
        ts.load_tiles(p)


@pytest.mark.parametrize("lon,lat,srs", [
    (-117.4, 47.4, "EPSG:6340"),   # NAD83(2011) / UTM 11N
    (-122.3, 47.6, "EPSG:6339"),   # 10N
    (-80.2, 25.8, "EPSG:6346"),    # 17N
    (-149.9, 61.2, "EPSG:6335"),   # Alaska, 6N
    (-155.5, 19.6, "EPSG:32605"),  # Hawaii: no NAD83(2011) UTM -> WGS 84
    (10.0, 50.0, "EPSG:32632"),
    (-70.0, -33.0, "EPSG:32719"),
])
def test_utm_srs(lon, lat, srs):
    assert ts.utm_srs(lon, lat) == srs


def test_load_bboxes(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("name,xmin,ymin,xmax,ymax\nfoo,1,2,3,4\n")
    assert ts.load_bboxes(p) == ([(1.0, 2.0, 3.0, 4.0)], ["foo"])
    p.write_text("xmin,ymin,xmax,ymax\n1,2,3,4\n")
    assert ts.load_bboxes(p) == ([(1.0, 2.0, 3.0, 4.0)], None)


# --------------------------------------------------------------------------- windows

AREA = (500000.0, 4000000.0, 501000.0, 4000500.0)
BOXES = [(500000.0, 4000000.0, 502000.0, 4001000.0), (502000.0, 4000000.0, 503000.0, 4003000.0)]  # an L


@pytest.mark.parametrize("core,expected", [(250, 32 + 48), (1000, 2 + 3), (3000, 3)])
def test_grid_window_counts(core, expected):
    windows = ts.grid_windows(BOXES, core, 40)
    assert len(windows) == expected == len({w.name for w in windows})
    for w in windows:
        assert w.core[2] - w.core[0] == core and w.core[3] - w.core[1] == core
        assert w.halo == (w.core[0] - 40, w.core[1] - 40, w.core[2] + 40, w.core[3] + 40)


@pytest.mark.parametrize("core", [250, 700, 3000])
def test_grid_windows_cover_boxes_exactly_once(core):
    """Every point of every box is owned by exactly one window core, whatever the window size."""
    windows = ts.grid_windows(BOXES, core, 0)
    rng = np.random.default_rng(0)
    x = np.concatenate([rng.uniform(b[0], b[2], 5000) for b in BOXES] + [[b[0] for b in BOXES]])
    y = np.concatenate([rng.uniform(b[1], b[3], 5000) for b in BOXES] + [[b[1] for b in BOXES]])
    owners = sum(w.in_core(x, y).astype(int) for w in windows)
    assert (owners == 1).all()


def test_grid_windows_serpentine():
    windows = ts.grid_windows([(0, 0, 5000, 4000)], 1000, 0, order="rows")
    for a, b in zip(windows, windows[1:]):
        if a.core[1] == b.core[1]:
            step = 1000 if (a.core[1] // 1000) % 2 == 0 else -1000
            assert b.core[0] - a.core[0] == step
        else:
            assert b.core[1] - a.core[1] == 1000 and b.core[0] == a.core[0]


def test_grid_windows_clip():
    windows = ts.grid_windows(BOXES, 500, 20, bbox=AREA)
    assert [w.core for w in windows] == [(500000, 4000000, 500500, 4000500), (500500, 4000000, 501000, 4000500)]


def test_bbox_windows_names_and_halo():
    ws = ts.bbox_windows([(1, 2, 3, 4), (5, 6, 7, 8)], 10, ["my site", "a/b"])
    assert [w.name for w in ws] == ["my_site", "a_b"]
    assert ws[0].core == (1, 2, 3, 4) and ws[0].halo == (-9, -8, 13, 14)
    assert [w.name for w in ts.bbox_windows([(0, 0, 1, 1)] * 2, 0)] == ["site_00000", "site_00001"]
    with pytest.raises(ValueError, match="unique"):
        ts.bbox_windows([(0, 0, 1, 1)] * 2, 0, ["x", "x"])


def test_bbox_windows_lonlat_encloses_box():
    from pyproj import Transformer
    box = (-100.0016, 40.0000, -100.0000, 40.0010)
    srs = ts.utm_srs(*ts._centre(box))
    w, = ts.bbox_windows([box], 0, crs=ts.LONLAT, srs=srs)
    tr = Transformer.from_crs(ts.LONLAT, srs, always_xy=True)
    lon, lat = np.linspace(box[0], box[2], 20), np.linspace(box[1], box[3], 20)
    for xs, ys in [(lon, np.full(20, box[1])), (lon, np.full(20, box[3])),
                   (np.full(20, box[0]), lat), (np.full(20, box[2]), lat)]:
        x, y = tr.transform(xs, ys)
        assert w.core[0] <= x.min() and x.max() <= w.core[2]
        assert w.core[1] <= y.min() and y.max() <= w.core[3]
    assert 130 < w.core[2] - w.core[0] < 150 and 105 < w.core[3] - w.core[1] < 120


def test_plan_without_ept(tmp_path):
    """bbox, bboxes and download-list plans need no EPT; srs comes from the area."""
    windows, srs = ts.plan(None, 20, bbox=(-117.0, 47.0, -116.99, 47.005), core=250)
    assert srs == "EPSG:6340" and len(windows) == 12
    windows, srs = ts.plan(None, 20, bboxes=[(-122.3, 47.6, -122.299, 47.601)], names=["a"])
    assert srs == "EPSG:6339" and [w.name for w in windows] == ["a"]
    p = tmp_path / "list.txt"
    p.write_text("https://x/y/USGS_LPC_A_B_11TMN470250.laz\n")
    windows, srs = ts.plan(None, 20, tiles=ts.load_tiles(p), core=500)
    assert srs == "EPSG:6340" and len(windows) == 4
    windows, _ = ts.plan(None, 20, tiles=ts.load_tiles(p), core=500, bbox=(470000, 5250000, 470400, 5250400),
                         bbox_crs="EPSG:6340")
    assert [w.core for w in windows] == [(470000, 5250000, 470500, 5250500)]
    for bad in ({}, {"bbox": (0, 0, 1, 1), "bboxes": [(0, 0, 1, 1)]}, {"everything": True, "bboxes": [(0, 0, 1, 1)]}):
        with pytest.raises(ValueError, match="area"):
            ts.plan(None, 20, **bad)


# --------------------------------------------------------------------------- rasters

@pytest.mark.parametrize("res", [0.5, 1.0, 2.0, 3.0])
def test_window_grid_snaps_and_covers_halo(res):
    w = ts.Window("w", (500123.4, 4000456.7, 500223.4, 4000556.7), (500103.4, 4000436.7, 500243.4, 4000576.7))
    x0, ytop, nx, ny = w.grid(res)
    assert x0 / res == round(x0 / res) and ytop / res == round(ytop / res)
    assert x0 <= w.halo[0] and x0 + nx * res >= w.halo[2]
    assert ytop >= w.halo[3] and ytop - ny * res <= w.halo[1]


def test_crop_raster_exact_for_grid_window():
    w = ts.grid_windows([AREA], 500, 20)[0]
    x0, ytop, nx, ny = w.grid(1.0)
    c = ts.crop_raster(ts.Raster(np.zeros((ny, nx)), x0, ytop, 1.0), w.core)
    assert c.data.shape == (500, 500) and (c.x0, c.y0) == (500000, 4000500)


def test_crop_raster_snaps_bbox_outward():
    w, = ts.bbox_windows([(500123.4, 4000456.7, 500223.4, 4000556.7)], 20)
    x0, ytop, nx, ny = w.grid(1.0)
    c = ts.crop_raster(ts.Raster(np.zeros((2, ny, nx)), x0, ytop, 1.0), w.core)
    assert (c.x0, c.y0) == (500123, 4000557) and c.data.shape == (2, 101, 101)


def test_crop_raster_rejects_raster_not_covering_core():
    with pytest.raises(ValueError, match="does not cover"):
        ts.crop_raster(ts.Raster(np.zeros((10, 10)), 0, 10, 1.0), (5, 5, 15, 15))


def test_crop_geotiff_matches_crop_raster(tmp_path):
    from osgeo import gdal
    w, = ts.bbox_windows([(500123.4, 4000456.7, 500223.4, 4000556.7)], 20)
    x0, ytop, nx, ny = w.grid(1.0)
    r = ts.Raster(np.arange(ny * nx, dtype=np.float32).reshape(ny, nx), x0, ytop, 1.0, nodata=-1)
    ts.write_geotiff(tmp_path / "in.tif", r, SRS)
    ts.crop_geotiff(tmp_path / "in.tif", tmp_path / "out.tif", w.core)
    ds = gdal.Open(str(tmp_path / "out.tif"))
    want = ts.crop_raster(r, w.core)
    np.testing.assert_array_equal(ds.ReadAsArray(), want.data)
    assert ds.GetGeoTransform() == (want.x0, 1.0, 0.0, want.y0, 0.0, -1.0)
    assert ds.GetRasterBand(1).GetNoDataValue() == -1


def test_crop_csv(tmp_path):
    w, = ts.bbox_windows([(0, 0, 10, 10)], 5)
    (tmp_path / "a.csv").write_text("x,y,v\n-1,5,a\n0,0,b\n9.9,5,c\n10,5,d\n")
    ts.crop_csv(tmp_path / "a.csv", tmp_path / "b.csv", w)
    assert (tmp_path / "b.csv").read_text().split() == ["x,y,v", "0,0,b", "9.9,5,c"]
    (tmp_path / "a.csv").write_text("v\n1\n2\n")
    ts.crop_csv(tmp_path / "a.csv", tmp_path / "b.csv", w)
    assert (tmp_path / "b.csv").read_text().split() == ["v", "1", "2"]


# --------------------------------------------------------------------------- run / merge on synthetic points

SPACING = 0.5  # m


def synthetic_points(bounds):
    """Deterministic 'lidar': a 0.5 m lattice over a surface with a peak every ~40 m,
    so every window sees exactly the points a whole-area read would."""
    x0, y0, x1, y1 = bounds
    xs = (np.arange(np.floor(x0 / SPACING) - 1, np.ceil(x1 / SPACING) + 1) + 0.5) * SPACING
    ys = (np.arange(np.floor(y0 / SPACING) - 1, np.ceil(y1 / SPACING) + 1) + 0.5) * SPACING
    xs, ys = xs[(xs >= x0) & (xs < x1)], ys[(ys >= y0) & (ys < y1)]
    X, Y = (v.ravel() for v in np.meshgrid(xs, ys))
    a = np.zeros(len(X), dtype=[("X", "f8"), ("Y", "f8"), ("Z", "f8"),
                                ("Classification", "u1"), ("ReturnNumber", "u1")])
    a["X"], a["Y"] = X, Y
    a["Z"] = 600 + 0.05 * (X - 500000) + 10 * np.cos(2 * np.pi * X / 37) * np.cos(2 * np.pi * Y / 41)  # 5% slope
    a["ReturnNumber"] = 1
    noise = (np.round(X / SPACING) * 7919 + np.round(Y / SPACING) * 104729) % 997 == 0  # by location, not index
    a["Classification"] = np.where(noise, 7, 1)  # noise points that analyses must drop
    a["Z"][noise] += 50
    hole = (X > 500600) & (X < 500700) & (Y > 4000300) & (Y < 4000400)  # no data here
    return a[~hole]


@pytest.fixture
def fake_ept(monkeypatch):
    calls = []

    def fetch(ept, bounds, srs, dims=None, resolution=None, threads=8, retries=6):
        calls.append(bounds)
        return synthetic_points(bounds)

    monkeypatch.setattr(ts, "fetch_points", fetch)
    return calls


def reference_peaks_run():
    big, = ts.bbox_windows([AREA], ex.HALO)
    res = ex.analyze(synthetic_points(big.halo), big)
    keep = big.in_core(res["peaks"]["x"], res["peaks"]["y"])
    peaks = set(zip(res["peaks"]["x"][keep], res["peaks"]["y"][keep]))
    return ts.crop_raster(res["dsm"], big.core).data, peaks


@pytest.mark.parametrize("core", [250, 500])
def test_run_merge_is_seamless(tmp_path, fake_ept, core):
    from osgeo import gdal
    windows = ts.grid_windows([AREA], core, ex.HALO)
    ts.run(windows, ex.analyze, tmp_path, ept="fake", srs=SRS)
    ts.merge(tmp_path)

    ref_dsm, ref_peaks = reference_peaks_run()
    mosaic = gdal.Open(str(tmp_path / "dsm.vrt")).ReadAsArray()
    assert mosaic.shape == ref_dsm.shape == (500, 1000)
    np.testing.assert_array_equal(mosaic, ref_dsm)
    assert np.nanmax(mosaic) < 670  # noise points dropped

    with np.load(tmp_path / "peaks.npz") as pk:
        peaks = set(zip(pk["x"], pk["y"]))
    assert len(peaks) > 100 and peaks == ref_peaks

    with open(tmp_path / "stats.csv") as f:
        rows = list(csv.DictReader(f))
    assert sorted(r["window"] for r in rows) == sorted(w.name for w in windows)
    assert list(rows[0]) == ["window", "xmin", "ymin", "xmax", "ymax", "n_peaks_in_core", "max_z"]
    assert sum(int(r["n_peaks_in_core"]) for r in rows) == len(peaks)


def test_run_resumes_and_limits(tmp_path, fake_ept):
    windows = ts.grid_windows([AREA], 250, ex.HALO)
    ts.run(windows, ex.analyze, tmp_path, ept="fake", srs=SRS, limit=3)
    assert len(fake_ept) == 3 and len(list((tmp_path / "_done").glob("*.json"))) == 3
    ts.run(windows, ex.analyze, tmp_path, ept="fake", srs=SRS)
    assert len(fake_ept) == len(windows)  # the first 3 were not fetched again
    ts.run(windows, ex.analyze, tmp_path, ept="fake", srs=SRS)
    assert len(fake_ept) == len(windows)


def test_run_refuses_to_mix_settings(tmp_path, fake_ept):
    windows = ts.grid_windows([AREA], 500, ex.HALO)
    ts.run(windows, ex.analyze, tmp_path, ept="fake", srs=SRS, limit=1)
    with pytest.raises(ValueError, match="other settings"):
        ts.run(windows, ex.analyze, tmp_path, ept="fake", srs="EPSG:32611")


def test_run_skips_empty_windows(tmp_path, fake_ept):
    calls = []
    w, = ts.bbox_windows([(500610, 4000310, 500690, 4000390)], 5, ["hole"])  # inside the synthetic hole
    ts.run([w], lambda p, w: calls.append(w) or {}, tmp_path, ept="fake", srs=SRS)
    assert calls == [] and json.loads((tmp_path / "_done" / "hole.json").read_text())["points"] == 0


def test_run_bbox_sites(tmp_path, fake_ept):
    from osgeo import gdal
    windows = ts.bbox_windows([(500123.4, 4000456.7, 500223.4, 4000556.7), (500900, 4000100, 500950, 4000150)],
                              ex.HALO, ["odd box", "tiny"])
    ts.run(windows, ex.analyze, tmp_path, ept="fake", srs=SRS)
    ts.merge(tmp_path)
    ds = gdal.Open(str(tmp_path / "dsm" / "odd_box.tif"))
    assert (ds.RasterXSize, ds.RasterYSize) == (101, 101)
    assert ds.GetGeoTransform()[:4] == (500123.0, 1.0, 0.0, 4000557.0)
    with open(tmp_path / "stats.csv") as f:
        assert sorted(r["window"] for r in csv.DictReader(f)) == ["odd_box", "tiny"]
    with np.load(tmp_path / "peaks.npz") as pk:
        inside = [w.in_core(pk["x"], pk["y"]) for w in windows]
    assert (inside[0] | inside[1]).all()


# --------------------------------------------------------------------------- --exec analyses

def write_script(path: Path, body: str) -> str:
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body)
    path.chmod(0o755)
    return str(path)


def test_exec_contract(tmp_path, fake_ept, monkeypatch):
    """Args, env vars and PATH the command sees, and what happens to each kind of output file."""
    env_bin = str(Path(sys.prefix) / "bin")
    monkeypatch.setenv("PATH", os.pathsep.join(p for p in os.environ["PATH"].split(os.pathsep) if p != env_bin))
    script = write_script(tmp_path / "tool.sh", """
extra=$1 laz=$2 out=$3
cat > "$out/env.json" <<EOF
{"name": "$TS_NAME", "srs": "$TS_SRS", "core": "$TS_CORE", "halo": "$TS_HALO",
 "pdal": "$(command -v pdal)", "laz_exists": $([ -s "$laz" ] && echo true || echo false), "extra": "$extra"}
EOF
printf 'x,y\\n499999,4000100\\n500100,4000100\\n' > "$out/pts.csv"
echo hello > "$out/note.txt"
""")
    out = tmp_path / "out"
    w, = ts.grid_windows([AREA], 500, 7.5, bbox=(500000, 4000000, 500100, 4000100))
    ts.run([w], ts.exec_analysis([script, "--extra-arg"], out, SRS), out, ept="fake", srs=SRS)
    ts.merge(out)
    with open(out / "env.csv") as f:
        row, = csv.DictReader(f)
    assert row["name"] == w.name == "500000_4000000" and row["srs"] == SRS
    assert tuple(map(float, row["core"].split())) == w.core and tuple(map(float, row["halo"].split())) == w.halo
    assert row["pdal"] == str(Path(env_bin) / "pdal") and row["laz_exists"] == "True"
    assert row["extra"] == "--extra-arg"  # the command's own args come first, then LAZ and work dir
    assert (out / "pts" / f"{w.name}.csv").read_text().split() == ["x,y", "500100,4000100"]
    assert (out / "note" / f"{w.name}.txt").read_text() == "hello\n"
    assert not (out / "_work" / w.name).exists() and not list((out / "_work").glob("*.laz"))


def test_exec_failure_stops_run_and_keeps_window_todo(tmp_path, fake_ept):
    script = write_script(tmp_path / "bad.sh", 'echo partial > "$2/x.txt"; exit 3\n')
    out = tmp_path / "out"
    windows = ts.grid_windows([AREA], 500, 5)
    with pytest.raises(RuntimeError, match="exited with 3"):
        ts.run(windows, ts.exec_analysis(script, out, SRS), out, ept="fake", srs=SRS)
    assert not list((out / "_done").glob("*.json"))
    assert (out / "_work" / windows[0].name / "x.txt").exists()  # left for debugging
    assert not list((out / "_work").glob("*.laz"))


def test_exec_example_dsm_is_seamless(tmp_path, fake_ept):
    """example_dsm.sh over four 250 m windows mosaics identically to one 1000 x 500 m window."""
    from osgeo import gdal
    halo = 5
    grid_out, single_out = tmp_path / "grid", tmp_path / "single"
    ts.run(ts.grid_windows([AREA], 250, halo), ts.exec_analysis(DSM_SH, grid_out, SRS), grid_out,
           ept="fake", srs=SRS)
    ts.merge(grid_out)
    ts.run(ts.bbox_windows([AREA], halo, ["all"]), ts.exec_analysis(DSM_SH, single_out, SRS), single_out,
           ept="fake", srs=SRS)
    mosaic = gdal.Open(str(grid_out / "dsm.vrt")).ReadAsArray()
    single = gdal.Open(str(single_out / "dsm" / "all.tif")).ReadAsArray()
    assert mosaic.shape == single.shape == (500, 1000)
    np.testing.assert_array_equal(mosaic, single)
    valid = mosaic != -9999
    assert valid.mean() > 0.95 and mosaic[valid].max() < 670  # noise dropped; the hole is nodata
    with open(grid_out / "info.csv") as f:
        assert len(list(csv.DictReader(f))) == 8  # one summary row per window
