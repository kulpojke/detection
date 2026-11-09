"""Offline tests: window planning, cropping, and run/merge/resume/exec on synthetic points.
Coordinates are arbitrary metric numbers; no EPT or network is used."""
import csv
import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest

import buildings
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
    a = np.zeros(len(X), dtype=[("X", "f8"), ("Y", "f8"), ("Z", "f8"), ("Intensity", "u2"),
                                ("Classification", "u1"), ("ReturnNumber", "u1"), ("NumberOfReturns", "u1"),
                                ("PointSourceId", "u2")])
    a["X"], a["Y"], a["Intensity"], a["PointSourceId"] = X, Y, 100, 1
    a["Z"] = 600 + 0.05 * (X - 500000) + 10 * np.cos(2 * np.pi * X / 37) * np.cos(2 * np.pi * Y / 41)  # 5% slope
    a["ReturnNumber"] = a["NumberOfReturns"] = 1
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


@pytest.mark.parametrize("analysis", [ex, buildings])
def test_point_on_grid_bottom_edge(analysis):
    """A point exactly on the bottom edge of the grid lands in the last row instead of crashing."""
    w, = ts.bbox_windows([(500000.0, 4000000.0, 500010.0, 4000010.0)], 0)
    pts = synthetic_points(w.halo)
    pts["Y"][0] = w.halo[1]  # the grid starts exactly here: halo 0, edge a multiple of RES
    analysis.analyze(pts, w)


def test_rectangularity():
    """1 for a rectangle, nearly 1 for a rotated one (its edges are pixel steps), lower for other shapes."""
    y, x = np.mgrid[-30:31, -30:31]

    def score(mask):
        return buildings.rectangularity(*np.nonzero(mask))

    assert score((abs(x) <= 10) & (abs(y) <= 4)) == pytest.approx(1)
    assert score((abs(x + y) <= 30) & (abs(x - y) <= 12)) > 0.9  # rotated 45 degrees
    assert score((abs(x) <= 10) & (abs(y) <= 10) & ((x <= 0) | (y <= 0))) == pytest.approx(341 / 441)  # L shape
    assert 0.7 < score(x ** 2 + y ** 2 <= 400) < 0.82  # disc: pi / 4 for a perfect one


def test_segment_keeps_small_patch_next_to_tree():
    """A 2 x 2 m patch half a metre from a taller tree has no tree top of its own (the tree is
    higher within TREE_TOP_RADIUS), but it still becomes an object of its own."""
    y, x = np.mgrid[0:30, 0:30]
    ndsm = 15 - 2 * np.hypot(x - 12, y - 15)  # a cone-shaped tree, 3 m high at its edge, 6 pixels out
    ndsm[ndsm < 3] = 0
    ndsm[14:18, 20:24] = 2.5  # the patch, one empty pixel right of the tree
    zeros = np.zeros((30, 30), np.float32)
    g = {"ndsm": ndsm.astype(np.float32), "ground_gap": zeros,  # no holes: no roof seeds
         "multi_return": zeros, "roughness": zeros}
    segments, n_roofs = buildings.segment(g)
    tree, patch = segments[15, 12], segments[15, 21]
    assert n_roofs == 0 and tree > 0 and patch > 0 and tree != patch
    assert set(np.unique(segments)) == {0, tree, patch}  # just these two objects


def test_segment_separates_tree_over_roof():
    """A tree hanging over a roof edge also hides the ground, so the roof's covered hole runs
    on under its crown. The crown still becomes a tree of its own, not part of the roof."""
    y, x = np.mgrid[0:50, 0:50]
    roof = (y >= 5) & (y < 35) & (x >= 5) & (x < 35)  # a flat 15 x 15 m roof, 4 m high
    cone = 8 - 1.2 * np.hypot(x - 36, y - 20)  # a tree on the roof's right edge, 8 m at the top
    crown = cone > np.where(roof, 4, 2.5)  # where the crown is seen from above
    ndsm = np.where(crown, cone, np.where(roof, 4, 0)).astype(np.float32)
    split = crown.astype(np.float32)  # pulses split in the crown, not on the roof
    g = {"ndsm": ndsm, "ground_gap": np.where(ndsm > 2, 2, 0).astype(np.float32),  # no ground under either
         "multi_return": split, "roughness": split, "pulses": np.ones((50, 50)), "split": split}
    segments, n_roofs = buildings.segment(g)
    assert n_roofs == 1 and segments[20, 36] > 1  # the tree top is not in the roof
    assert (segments[roof & ~crown] == 1).all()  # the roof keeps all of its own pixels
    # the crown is the tree, but for a pixel or two at its pointed edge: there it is thinner than
    # the disk of reclaim_roof's opening, so they go to the roof
    assert np.sum(segments[crown] != segments[20, 36]) <= 2


def test_segment_seeds_small_shed():
    """On a 2 x 3 m shed every pixel is near the edge, so the 3 x 3 multi_return of each pixel
    is high, though the shed as a whole has no split pulses. It is still a roof."""
    shed = np.zeros((20, 20), bool)
    shed[8:12, 7:13] = True
    ones = np.ones((20, 20))
    g = {"ndsm": np.where(shed, 2.5, 0).astype(np.float32), "ground_gap": np.where(shed, 1, 0).astype(np.float32),
         "multi_return": np.full((20, 20), 0.2, np.float32),  # edge splits seen from each 3 x 3 neighbourhood
         "roughness": np.zeros((20, 20), np.float32), "pulses": ones, "split": 0 * ones}
    segments, n_roofs = buildings.segment(g)
    assert n_roofs == 1 and (segments[shed] == 1).all()


def test_is_building():
    """The rule on real objects from Nine Mile Falls and Chewelah (bush, big house), and edge cases."""
    t = {k: np.array(v, dtype=float) for k, v in {
        #                  house  shed  crown  rock  bush  big house  tree-seeded  3.5 m^2  no pulses
        "roof_seed":        [1,    1,    1,     1,    1,    1,         0,           1,       1],
        "area":             [183,  7.5,  12.8,  8.8,  16,   180,       50,          3.5,     10],
        "multi_return":     [0.08, 0.18, 0.55,  0.27, 0.23, 0.18,      0.1,         0.08,    np.nan],
        "roughness_inside": [0.12, 0.05, 2.43,  0.61, 0.36, 0.40,      0.1,         0.05,    0.1]}.items()}
    # the bush is too rough inside for a small object; the big house, as rough, has a complex roof
    assert buildings.is_building(t).tolist() == [True, True, False, False, False, True, False, False, False]
    assert buildings.is_building({k: v[1] for k, v in t.items()})  # one row works too
    # the building column: the shed is a small structure, the houses are buildings
    assert buildings.building_code(t).tolist() == [1, 2, 0, 0, 0, 1, 0, 0, 0]


def test_point_metrics():
    """Metrics from each object's points, the same whatever order the points come in (it can
    differ between windows)."""
    #                   object 1: four first returns, one under MIN_HEIGHT, a second return   2      none
    h = np.array([3, 4, 5, 6, 1, 2.5, 1, 9])
    first = np.array([1, 1, 1, 1, 1, 0, 1, 1], bool)
    intensity = np.array([10, 20, 30, 40, 99, 5, 7, 8], np.uint16)
    label = np.array([1, 1, 1, 1, 1, 1, 2, 0])
    m = buildings.point_metrics(h, first, intensity, label, np.array([1, 2]))
    assert m["n_points"].tolist() == [6, 1]
    assert m["top"][0] == 6 and m["h_mean"][0] == pytest.approx(4.1) and m["h_p50"][0] == 4  # 2.5, 3, 4, 5, 6
    assert m["h_p95"][0] == pytest.approx(5.8) and m["h_std"][0] == pytest.approx(np.std([2.5, 3, 4, 5, 6]))
    assert m["cover"][0] == 4 / 5 and m["i_mean"][0] == 25 and m["i_std"][0] == pytest.approx(np.std([10, 20, 30, 40]))
    assert m["cover"][1] == 0 and np.isnan(m["top"][1]) and np.isnan(m["i_mean"][1])  # nothing above MIN_HEIGHT
    o = np.random.default_rng(0).permutation(len(h))
    shuffled = buildings.point_metrics(h[o], first[o], intensity[o], label[o], np.array([1, 2]))
    assert all(np.array_equal(m[k], shuffled[k], equal_nan=True) for k in m)


def test_cover_min():
    """The vehicle clue. Object 1, a vehicle, is there in line 7 and gone in line 9. Object 2, a
    building, is there in both; line 12 only grazes it (1 of its 4 pixels) and sees the ground
    beyond its edge, so it doesn't count; its second return isn't a first return. Object 3 has
    no points."""
    #               vehicle: line 7    line 9 | building: line 7   line 9      2nd    line 12 | none
    pixel = np.array([10, 11,          10, 11,  20, 21, 22, 23,   20, 21, 22, 23, 21,    23,     30])
    source = np.array([7, 7,           9, 9,    7, 7, 7, 7,       9, 9, 9, 9,     9,     12,     9])
    h = np.array([2.5, 2.5,           0.1, 0.1, 3, 3, 3, 3,       3, 3, 3, 3,     0,     0.2,    0])
    label = np.array([1, 1,            1, 1,    2, 2, 2, 2,       2, 2, 2, 2,     2,     2,      0])
    first = label >= 0
    first[12] = False
    rows = np.array([1, 2, 3])
    c = buildings.cover_min(h, first, label, pixel, source, rows)
    assert c[0] == 0 and c[1] == 1 and np.isnan(c[2])
    o = np.random.default_rng(0).permutation(len(h))
    assert np.array_equal(buildings.cover_min(h[o], first[o], label[o], pixel[o], source[o], rows), c, equal_nan=True)


def test_describe_roughness_inside():
    """On a small object half the pixels are edge pixels, where the Laplacian sees the drop to the
    ground. Inside, a shed's flat roof is smooth, but a rock (a dome) is not."""
    from scipy import ndimage
    y, x = np.mgrid[0:20, 0:30]
    shed = (y >= 6) & (y < 12) & (x >= 4) & (x < 12)  # 3 x 4 m, flat, 2.5 m high
    dome = 2.5 * (1 - np.hypot(x - 21, y - 9) ** 2 / 16)  # a rock 4 m across, 2.5 m high
    rock = dome > 0
    dsm = np.where(shed, 2.5, 0) + np.where(rock, dome, 0)
    zeros = np.zeros(dsm.shape)
    g = {"ndsm": dsm, "roughness": np.abs(ndimage.laplace(dsm)), "ground_gap": zeros,
         "pulses": zeros + 1, "split": zeros}
    f = buildings.describe(*np.nonzero(shed), g)
    assert f["roughness"] > 1 and f["roughness_inside"] == 0  # all pixels: half of them are edge
    # a paraboloid bends the same everywhere: Laplacian 4 x 2.5 m / 4^2 pixels
    assert buildings.describe(*np.nonzero(rock), g)["roughness_inside"] == pytest.approx(0.625)


def test_reclaim_roof():
    """Tree pixels that are really roof go to the roof: a thin strip along its edge (an eave) and
    a patch with roof all around it (a rooftop unit). A crown over the roof's edge stays a tree,
    and so does a thin hedge that doesn't touch the roof."""
    seg = np.zeros((40, 40), np.int32)
    y, x = np.mgrid[0:40, 0:40]
    seg[5:30, 5:25] = 1  # the roof (n_roofs = 1)
    seg[5:30, 3:5] = 2  # a 1 m strip along its left edge
    seg[14:20, 12:18] = 3  # a 3 x 3 m unit on it
    crown = np.hypot(x - 27, y - 15) <= 5  # a 5 m crown over its right edge
    seg[crown] = 4
    seg[34:36, 5:30] = 5  # a 1 m hedge, 2 m from the roof
    out = buildings.reclaim_roof(seg.copy(), 1, tree_like(seg))
    assert (out[5:30, 3:5] == 1).all() and (out[14:20, 12:18] == 1).all()  # strip and unit are roof now
    assert np.sum(out[crown] != 4) <= 4  # pixels at the crown's pointed edge, next to the roof, may go
    assert (out[34:36, 5:30] == 5).all()


def tree_like(seg, split=None):
    """Grids for reclaim_roof: every pulse splits (unless ``split`` says otherwise), and the
    height is the same everywhere."""
    ones = np.ones(seg.shape)
    return {"split": ones if split is None else split, "pulses": ones, "ndsm": ones}


def test_reclaim_roof_roof_like_object():
    """A whole "tree" object next to a roof with few split pulses is a roof section (a tree top
    landed on it): it goes to the roof. A real crown next to it, with many split pulses, stays."""
    seg = np.zeros((30, 40), np.int32)
    seg[5:25, 10:30] = 1  # the roof
    seg[5:12, 3:10] = 2  # 3.5 x 3.5 m against its left edge: few split pulses
    seg[15:22, 30:37] = 3  # the same against its right edge: many split pulses
    split = np.where(seg == 2, 0.1, 0.5)
    out = buildings.reclaim_roof(seg.copy(), 1, tree_like(seg, split))
    # the crown's middle stays (its corner pixels next to the roof go with the opening, as above)
    assert (out[seg == 2] == 1).all() and (out[17:20, 32:36] == 3).all()


def test_reclaim_roof_keeps_objects_whole():
    """Two "trees" that flooded along the roof's edges, each from a top beside the roof. The thin
    strips go to the roof, which cuts each object in parts. The part with the top keeps the
    label. A cut-off part next to the roof goes to the roof; one that only touches the strip
    (now roof) becomes an object of its own. Every object ends up in one piece."""
    from scipy import ndimage
    seg = np.zeros((40, 45), np.int32)
    seg[5:35, 12:28] = 1  # the roof
    seg[2:9, 4:11] = seg[9:31, 10:12] = seg[28:35, 5:12] = 2  # top part, 1 m strip on the left edge, end part
    seg[2:9, 30:37] = seg[2:23, 28:30] = seg[21:23, 30:34] = seg[18:25, 34:41] = 3  # top part, strip, neck, end part
    ndsm = np.ones(seg.shape)
    ndsm[5, 7] = ndsm[5, 33] = 8  # the tops
    g = {**tree_like(seg), "ndsm": ndsm}
    out = buildings.reclaim_roof(seg.copy(), 1, g)
    assert out[5, 7] == 2 and out[5, 33] == 3  # the parts with the tops
    assert (out[12:27, 10:12] == 1).all() and (out[29:34, 6:11] == 1).all()  # strip and end part on the left
    end = out[19:24, 35:40]
    assert len(np.unique(end)) == 1 and end[0, 0] not in (0, 1, 2, 3)  # the right end part: an object of its own
    assert all(ndimage.label(out == k)[1] == 1 for k in np.unique(out[out > 1]))


def test_outlines():
    """Outlines follow the pixel edges in map coordinates. On objects of at least
    SIMPLIFY_MIN_AREA a pixel staircase becomes a straight edge; smaller ones keep it."""
    from osgeo import ogr
    labels = np.zeros((60, 60), np.int32)
    labels[2:6, 3:9] = 1  # 4 x 6 pixels: a 2 x 3 m shed
    y, x = np.mgrid[0:60, 0:60]
    labels[(abs(x + y - 38) <= 8) & (abs(x - y + 2) <= 3)] = 2  # a rectangle turned 45 degrees, 14.75 m^2
    labels[(abs(x + y - 82) <= 16) & (abs(x - y) <= 6)] = 3  # a bigger one, 53.75 m^2
    shapes = {i: buildings.simplify(g) for i, g in buildings.trace(labels, 1000.0, 2000.0).items()}
    assert shapes[1].GetEnvelope() == (1001.5, 1004.5, 1997.0, 1999.0) and shapes[1].GetArea() == 6
    assert shapes[1].GetGeometryRef(0).GetPointCount() == 5  # 4 corners, closed
    # small: the exact staircase (simplified, it grew 15%)
    assert shapes[2].GetGeometryRef(0).GetPointCount() == 41 and shapes[2].GetArea() == 14.75
    # big: straight edges, and about the same area
    assert shapes[3].GetGeometryRef(0).GetPointCount() <= 9
    assert shapes[3].GetArea() == pytest.approx(np.sum(labels == 3) * 0.25, rel=0.1)


def turned(inside, angle=30):
    """A 40 x 40 m label image (0.5 m pixels, top left corner at (1000, 2040)) of a shape given in
    its own coordinates (u along it, v across, centre at (1020, 2020)), turned by angle: a pixel is
    in the shape if its centre is."""
    y, x = np.mgrid[0:80, 0:80]
    x, y = (x + 0.5) * 0.5 - 20, 20 - (y + 0.5) * 0.5
    t = np.radians(angle)
    return inside(x * np.cos(t) + y * np.sin(t), y * np.cos(t) - x * np.sin(t)).astype(np.int32)


def turn(corners, angle=30):
    """Shape coordinates (as in turned) in map coordinates."""
    t = np.radians(angle)
    return np.array([(1020 + u * np.cos(t) - v * np.sin(t), 2020 + u * np.sin(t) + v * np.cos(t)) for u, v in corners])


def pixel_outlines(labels):
    """{label: ogr geometry}: the exact pixel outlines of a label image made by turned."""
    return buildings.trace(labels, 1000.0, 2040.0)


BOX = lambda u, v: (abs(u) < 6) & (abs(v) < 4)  # 12 x 8 m
STRAIGHT_SHAPES = {  # inside test (shape coordinates) and the true corners
    "rectangle": (BOX, [(-6, -4), (6, -4), (6, 4), (-6, 4)]),
    "L": (lambda u, v: BOX(u, v) & ~((u > 1) & (v > 0)), [(-6, -4), (6, -4), (6, 0), (1, 0), (1, 4), (-6, 4)]),
    "diagonal wall": (lambda u, v: BOX(u, v) & (u + v < 7), [(-6, -4), (6, -4), (6, 1), (3, 4), (-6, 4)]),
    "6 x 6 m garage": (lambda u, v: (abs(u) < 3) & (abs(v) < 3), [(-3, -3), (3, -3), (3, 3), (-3, 3)]),
    # a cut under MIN_DIAGONAL (3.2 m; 3.6 m between the corners Douglas-Peucker finds) is a ragged
    # bit: the walls meet at the corner, though it is further than MAX_SHIFT from the middle of the
    # cut (square walls always meet)
    "3.2 m cut corner": (lambda u, v: BOX(u, v) & (u + v < 7.75), [(-6, -4), (6, -4), (6, 4), (-6, 4)]),
}


@pytest.mark.parametrize("shape", STRAIGHT_SHAPES)
def test_straighten(shape):
    """A shape turned 30 degrees comes back with its true corners: right angles, the diagonal wall
    kept, no corner cut off by the pixels, and about the true area (not the 8% more of simplifying)."""
    inside, true = STRAIGHT_SHAPES[shape]
    out, agree, code = buildings.straighten(pixel_outlines(turned(inside))[1])
    assert code == 0
    corners, true = np.array(out.GetGeometryRef(0).GetPoints())[:-1, :2], turn(true)
    assert len(corners) == len(true)
    assert max(np.hypot(*(corners - c).T).min() for c in true) < 0.15  # m, each true corner
    assert out.GetArea() == pytest.approx(buildings.ring_area(np.vstack([true, true[:1]])), rel=0.02)


def test_straighten_keeps_neighbours_apart():
    """Two 8 x 5 m buildings 1 m apart stay apart: a wall moves to the middle of its pixel staircase,
    never a whole pixel outward."""
    labels = turned(lambda u, v: (abs(u + 4.5) < 4) & (abs(v) < 2.5)) + 2 * turned(lambda u, v: (abs(u - 4.5) < 4) & (abs(v) < 2.5))
    shapes = pixel_outlines(labels)
    (a, _, code_a), (b, _, code_b) = buildings.straighten(shapes[1]), buildings.straighten(shapes[2])
    assert code_a == code_b == 0
    assert a.Distance(b) > 0.75


def test_straighten_safety_net():
    """A 2 x 3 m shed is too small to straighten (code 1). A disc's walls point every way, so they
    don't agree on a direction (code 2). Both keep their outline (None)."""
    shed = pixel_outlines(turned(lambda u, v: (abs(u) < 1.5) & (abs(v) < 1)))[1]
    disc = pixel_outlines(turned(lambda u, v: np.hypot(u, v) < 6))[1]
    assert buildings.straighten(shed)[::2] == (None, 1)
    out, agree, code = buildings.straighten(disc)
    assert (out, code) == (None, 2) and agree < buildings.STRAIGHT_AGREE


def test_trace_fills_small_holes():
    """A one-pixel hole in a roof (a low pixel: a skylight, a wet spot) is filled; a 2 x 2 m light
    well stays."""
    labels = np.zeros((80, 80), np.int32)
    labels[10:40, 10:40] = 1
    labels[15, 15] = 0
    labels[25:29, 25:29] = 0
    geom = pixel_outlines(labels)[1]
    assert geom.GetGeometryCount() == 2  # the outer ring and the light well
    assert geom.GetArea() == np.sum(labels == 1) * 0.25 + 0.25


def test_tree_notch():
    """A tree over a roof's corner: the roof outline gets a diagonal wall with tree in front of it,
    a tree notch (~5.7 m). The same diagonal wall with ground in front of it is a real wall, and a
    tree beside a straight wall isn't a notch either."""
    cut = lambda u, v: BOX(u, v) & (u + v < 6)  # a corner cut off: the diagonal from (6, 0) to (2, 4)
    crown = turned(lambda u, v: np.hypot(u - 6, v - 4) < 4.5)
    beside = turned(lambda u, v: np.hypot(u - 9, v) < 3.5)
    for roof, tree, notch in ((cut, crown, True), (cut, 0, False), (BOX, beside, False)):
        seg = turned(roof)
        seg[(tree == 1) & (seg == 0)] = 2  # the tree is object 2 (n_roofs = 1)
        length = buildings.tree_notch(pixel_outlines(seg)[1], 1, seg, 1, 1000.0, 2040.0)
        assert length == (pytest.approx(5.7, abs=0.5) if notch else 0)


def scene_points():
    """One pulse every 0.25 m over a 40 x 40 m square of flat ground (z = 0), with a 10 x 12 m
    house (a flat roof, 4 m), a tree (a cone, 12 m at the top, 8 m across; its pulses split and
    also reach the ground), a smooth dome (a "rock", 4 m high) and a 1 x 1.5 m bush (3 m)."""
    s = 0.25
    x, y = (v.ravel() for v in np.meshgrid(np.arange(1000 + s / 2, 1040, s), np.arange(2000 + s / 2, 2040, s)))
    house = (abs(x - 1010) < 5) & (abs(y - 2028) < 6)
    d_tree, d_rock = np.hypot(x - 1030, y - 2030), np.hypot(x - 1012, y - 2008)
    tree, rock = d_tree < 4, d_rock < 3
    bush = (abs(x - 1030) < 0.5) & (abs(y - 2010) < 0.75)
    pts = np.zeros(len(x), [("X", "f8"), ("Y", "f8"), ("Z", "f8"), ("Intensity", "u2"), ("Classification", "u1"),
                            ("ReturnNumber", "u1"), ("NumberOfReturns", "u1"), ("PointSourceId", "u2")])
    pts["PointSourceId"] = 1  # one flight line
    pts["X"], pts["Y"] = x, y
    pts["Z"] = np.select([house, tree, rock, bush], [4, 12 - 2.5 * d_tree, 4 * (1 - (d_rock / 3) ** 2), 3], 0)
    pts["Intensity"] = np.select([house, tree], [200, 300], 100)
    pts["Classification"] = np.where(house | tree | rock | bush, 1, 2)
    pts["ReturnNumber"], pts["NumberOfReturns"] = 1, np.where(tree | bush, 2, 1)
    ground = pts[tree | bush].copy()  # the second return of a split pulse
    ground["Z"], ground["Classification"], ground["ReturnNumber"], ground["Intensity"] = 0, 2, 2, 50
    return np.concatenate([pts, ground])


def test_analyze_layers():
    """The house is a footprint (a building), the tree a crown. The rock gets a roof seed but
    the rule rejects it (rough inside), so it is in both layers. The bush is too small for a crown."""
    from osgeo import ogr
    w = ts.Window("scene", (1000, 2000, 1040, 2040), (1000, 2000, 1040, 2040))
    out = buildings.analyze(scene_points(), w)
    near = lambda t, x, y: np.flatnonzero(np.hypot(t["x"] - x, t["y"] - y) < 2)
    fp, cr = out["footprints"], out["crowns"]
    house, rock = near(fp, 1010, 2028), near(fp, 1012, 2008)
    assert len(fp["x"]) == 2 and fp["building"][house] == 1 and fp["building"][rock] == 0
    tree, rock_crown = near(cr, 1030, 2030), near(cr, 1012, 2008)
    assert len(cr["x"]) == 2 and cr["roof_seed"][tree] == 0 and cr["roof_seed"][rock_crown] == 1
    assert "building" not in cr and "roof_seed" not in fp
    # the house gets straight walls: 4 corners, its 10 x 12 m; the rock isn't a building. No tree
    # notches. Only footprints have these columns.
    assert fp["outline"][house] == 0 and fp["outline"][rock] == 5 and fp["review"].tolist() == [0, 0]
    assert "outline" not in cr and "review" not in cr and "outline" not in out["objects"]
    walls = ogr.CreateGeometryFromWkt(fp["geometry"][house[0]])
    assert walls.GetGeometryRef(0).GetPointCount() == 5 and walls.GetArea() == pytest.approx(120, rel=0.02)
    assert len(near(out["objects"], 1030, 2010)) == 1  # the bush is an object, but no crown
    crown = ogr.CreateGeometryFromWkt(cr["geometry"][tree[0]])
    assert crown.Contains(ogr.CreateGeometryFromWkt("POINT (1030 2030)"))  # around the tree's top
    assert crown.GetArea() == pytest.approx(np.pi * 4 ** 2, rel=0.15)
    # metrics from the points: the house is a flat 4 m roof; the tree's highest point is 12 m minus
    # its distance to the middle (0.18 m on the 0.25 m lattice) x 2.5, and its first returns have intensity 300
    assert fp["top"][house] == 4 and fp["h_std"][house] == 0 and fp["cover"][house] == 1 and fp["i_mean"][house] == 200
    assert fp["cover_min"][house] == 1  # one flight line: the same as cover
    assert cr["top"][tree] == pytest.approx(12 - 2.5 * np.hypot(0.125, 0.125)) and cr["i_mean"][tree] == 300
    assert cr["cover"][tree] > 0.9  # a few edge pixels also hold ground hits


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


def squares(pts, w):
    """Toy analysis with a polygon output: a 20 m square around each point of a 40 m lattice
    over AREA (squares centred near a core edge reach into the next window), with an integer,
    a float (some NaN), a text and a coded column (see SQUARE_CODES)."""
    cx, cy = (v.ravel() for v in np.meshgrid(np.arange(500005, 501000, 40.0), np.arange(4000005, 4000500, 40.0)))
    wkt = [f"POLYGON(({x - 10} {y - 10},{x + 10} {y - 10},{x + 10} {y + 10},{x - 10} {y + 10},{x - 10} {y - 10}))"
           for x, y in zip(cx, cy)]
    i = np.arange(len(cx))
    return {"squares": {"x": cx, "y": cy, "geometry": np.array(wkt), "id": i,
                        "value": np.where(i % 7 == 0, np.nan, i / 2), "label": np.array([f"sq{k}" for k in i]),
                        "kind": i % 3}}


SQUARE_CODES = {"squares": {"kind": {0: "red", 1: "green", 2: "blue"}}}


def read_features(path):
    from osgeo import ogr
    ds = ogr.Open(str(path))  # keep ds: a layer is gone once its dataset is
    return {(f.GetGeometryRef().ExportToWkt(), f["id"], f["value"], f["label"]) for f in ds.GetLayer(0)}


def test_run_merge_features(tmp_path, fake_ept):
    """A table with a geometry column becomes a GeoPackage. Each polygon is kept whole, by the
    one window that owns its point, so a grid of windows gives the same features as one window.
    The meaning of coded columns is stored in the file, and survives the merge."""
    from osgeo import gdal, ogr
    grid, one = tmp_path / "grid", tmp_path / "one"
    ts.run(ts.grid_windows([AREA], 250, 0), squares, grid, ept="fake", srs=SRS, codes=SQUARE_CODES)
    ts.merge(grid)
    ts.run(ts.bbox_windows([AREA], 0, ["all"]), squares, one, ept="fake", srs=SRS, codes=SQUARE_CODES)
    features = read_features(grid / "squares.gpkg")
    assert len(features) == 25 * 13 and features == read_features(one / "squares" / "all.gpkg")
    ds = ogr.Open(str(grid / "squares.gpkg"))
    layer = ds.GetLayer(0)
    assert layer.GetFeatureCount() == 25 * 13  # each square once (the set above would hide doubles)
    assert layer.GetName() == "squares" and layer.GetSpatialRef().GetAuthorityCode(None) == "6340"
    assert {f.GetGeometryRef().GetArea() for f in layer} == {400}  # squares on a core edge are whole
    assert sum(value is None for _, _, value, _ in features) == len(range(0, 25 * 13, 7))  # NaN -> empty
    ds = gdal.OpenEx(str(grid / "squares.gpkg"))
    field = ds.GetLayer(0).GetLayerDefn().GetFieldDefn(ds.GetLayer(0).FindFieldIndex("kind", True))
    assert field.GetDomainName() == "kind"
    assert ds.GetFieldDomain("kind").GetEnumeration() == {"0": "red", "1": "green", "2": "blue"}


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
    """example_dsm.sh over eight 250 m windows mosaics identically to one 1000 x 500 m window."""
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
