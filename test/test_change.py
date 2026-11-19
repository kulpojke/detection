"""Offline tests of change.py (before and after): pairs, the shift between two sets, the CLI.
Footprints are hand-made rectangles in metres; EPSG:6339 is only a label."""
import re

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import box

import change

CRS = "EPSG:6339"


def frame(geoms, **columns):
    return gpd.GeoDataFrame(columns, geometry=list(geoms), crs=CRS)


def town(n=8, spacing=30.0, seed=0):
    """n x n houses, 10-16 m by 10-14 m, one per ``spacing`` m: far enough apart that a shift of a
    few metres never makes a house overlap its neighbour."""
    rng = np.random.default_rng(seed)
    houses = []
    for i in range(n):
        for j in range(n):
            w, h = rng.uniform(10, 16), rng.uniform(10, 14)
            x, y = 600000 + i * spacing, 4400000 + j * spacing
            houses.append(box(x, y, x + w, y + h))
    return houses


def after(houses, dx, dy, jitter=0.1, gone=(), new=(), seed=1):
    """The same houses moved by (dx, dy) as a whole, each by up to ``jitter`` m more (two flights
    never outline a house the same), without the houses at ``gone``, plus the boxes ``new``."""
    rng = np.random.default_rng(seed)
    moved = [gpd.GeoSeries([h]).translate(dx + rng.uniform(-jitter, jitter),
                                          dy + rng.uniform(-jitter, jitter)).iloc[0]
             for k, h in enumerate(houses) if k not in gone]
    return moved + list(new)


def test_pairs_shares():
    # A 10 x 10 m house seen 2 m further east: they share 80 m², 0.8 of each.
    pre = frame([box(0, 0, 10, 10)])
    post = frame([box(2, 0, 12, 10), box(-10, 0, 0, 10)])  # the second only touches it
    p = change.pairs(pre, post)
    assert len(p) == 1
    row = p.iloc[0]
    assert (row["pre"], row["post"]) == (0, 0)
    assert row["shared"] == pytest.approx(80)
    assert (row["of_pre"], row["of_post"]) == (pytest.approx(0.8), pytest.approx(0.8))


def test_one_to_one_leaves_out_a_building_seen_in_two_pieces():
    # Before: one 20 x 10 m building; after: two halves (a burned middle, or a tree between them).
    pre = frame([box(0, 0, 20, 10), box(40, 0, 50, 10)])
    post = frame([box(0, 0, 10, 10), box(10, 0, 20, 10), box(40, 0, 50, 10)])
    clear = change.one_to_one(change.pairs(pre, post), 0.7)
    assert clear[["pre", "post"]].values.tolist() == [[1, 2]]


@pytest.mark.parametrize("dx,dy", [(-1.35, 0.23),  # the Camp Fire's 2019 lidar vs 2018
                                   (4.0, -3.0),    # bigger: only the rough first round finds pairs
                                   (0.0, 0.0)])
def test_estimate_shift_finds_the_offset(dx, dy):
    houses = town()
    pre = frame(houses)
    # 6 houses gone, 3 new ones (in the gaps between the houses before), each house outlined up
    # to 0.1 m differently.
    post = frame(after(houses, dx, dy, gone={3, 10, 17, 30, 41, 55},
                       new=[box(600018, 4400016, 600026, 4400024), box(600108, 4400106, 600116, 4400114),
                            box(600198, 4400046, 600208, 4400054)]))
    sx_, sy_, spread_x, spread_y, n = change.estimate_shift(pre, post)
    assert (sx_, sy_) == (pytest.approx(dx, abs=0.03), pytest.approx(dy, abs=0.03))
    assert n == 64 - 6
    assert spread_x < 0.1 and spread_y < 0.1


def test_the_rough_round_is_needed_for_a_big_shift():
    # At 4 m east and 3 m south no pair is clear (each shares about 0.5 of the other), so clear
    # pairs alone would find nothing; the rough first round is what finds the shift.
    houses = town()
    pre, post = frame(houses), frame(after(houses, 4.0, -3.0))
    assert len(change.one_to_one(change.pairs(pre, post), change.CLEAR_SHARE)) == 0
    assert change.estimate_shift(pre, post)[:2] == (pytest.approx(4.0, abs=0.03), pytest.approx(-3.0, abs=0.03))


def test_no_shift_from_too_few_pairs():
    houses = town(n=3)  # 9 pairs, fewer than MIN_PAIRS
    dx, dy, spread_x, spread_y, n = change.estimate_shift(frame(houses), frame(after(houses, -1.35, 0.23)))
    assert (dx, dy, n) == (0.0, 0.0, 9)
    assert np.isnan(spread_x) and np.isnan(spread_y)


def test_read_takes_the_id_column_or_the_fid(tmp_path):
    with_id, without = tmp_path / "with_id.gpkg", tmp_path / "without.gpkg"
    frame([box(0, 0, 10, 10), box(20, 0, 30, 10)], id=["a", "b"]).to_file(with_id)
    frame([box(0, 0, 10, 10), box(20, 0, 30, 10)], name=["a", "b"]).to_file(without)
    assert change.read(with_id)["id"].tolist() == ["a", "b"]
    assert change.read(without)["id"].tolist() == ["1", "2"]  # a GeoPackage's fids start at 1
    assert change.read(without, id_column="name")["id"].tolist() == ["a", "b"]


def test_cli_box_and_too_few_pairs(tmp_path):
    houses = town()
    pre_path, post_path, out = tmp_path / "pre.gpkg", tmp_path / "post.gpkg", tmp_path / "out.gpkg"
    frame(houses).to_file(pre_path)
    frame(after(houses, -1.35, 0.23, jitter=0)).to_file(post_path)
    # The box holds the first 4 x 4 houses (their middles are inside it).
    change.main([str(out), "--pre", str(pre_path), "--post", str(post_path),
                 "--box", "599990", "4399990", "600110", "4400110"])
    pre, post = gpd.read_file(out, layer="pre"), gpd.read_file(out, layer="post")
    assert len(pre) == len(post) == 16
    # 16 clear pairs are fewer than MIN_PAIRS, so the shift isn't trusted and nothing is moved.
    assert (post["moved_x"] == 0).all() and (post["moved_y"] == 0).all()


def test_cli_moves_post_onto_pre_with_enough_pairs(tmp_path):
    houses = town()
    pre_path, post_path, out = tmp_path / "pre.gpkg", tmp_path / "post.gpkg", tmp_path / "out.gpkg"
    frame(houses).to_file(pre_path)
    frame(after(houses, -1.35, 0.23, jitter=0)).to_file(post_path)
    change.main([str(out), "--pre", str(pre_path), "--post", str(post_path)])
    pre, post = gpd.read_file(out, layer="pre"), gpd.read_file(out, layer="post")
    assert post["moved_x"].iloc[0] == pytest.approx(1.35, abs=0.001)
    assert post["moved_y"].iloc[0] == pytest.approx(-0.23, abs=0.001)
    # Without jitter, every house after is back exactly on its house before.
    assert np.allclose(post.geometry.centroid.x, pre.geometry.centroid.x, atol=0.001)
    assert np.allclose(post.geometry.centroid.y, pre.geometry.centroid.y, atol=0.001)


def flown_tif(path, x0, y1, first, last=None, cell=10.0):
    """A flight-date map like detect.py's: 2 bands, the first and the last point flown in each cell
    (days since 1970-01-01; NaN without points), top-left corner (x0, y1)."""
    from osgeo import gdal, osr
    gdal.UseExceptions()
    first = np.asarray(first, np.float32)
    last = first if last is None else np.asarray(last, np.float32)
    ds = gdal.GetDriverByName("GTiff").Create(str(path), first.shape[1], first.shape[0], 2, gdal.GDT_Float32)
    ds.SetGeoTransform((x0, cell, 0, y1, 0, -cell))
    sr = osr.SpatialReference()
    sr.SetFromUserInput(CRS)
    ds.SetProjection(sr.ExportToWkt())
    for k, a in enumerate((first, last), 1):
        ds.GetRasterBand(k).WriteArray(a)
        ds.GetRasterBand(k).SetNoDataValue(float("nan"))
    ds = None
    return path


DAY = change.day_number("2018-11-08")


def halves(left, right):
    """10 x 10 cells of 10 m: the left half with ``left``, the right half with ``right``."""
    a = np.full((10, 10), np.nan, np.float32)
    a[:, :5], a[:, 5:] = left, right
    return a


def test_seen_needs_points_under_the_middle(tmp_path):
    # A map of 100 x 100 m at (0, 0)-(100, 100): points in the left half only.
    tif = flown_tif(tmp_path / "flown.tif", 0, 100, halves(DAY, np.nan))
    g = frame([box(10, 10, 20, 20),     # all in the left half
               box(35, 10, 55, 20),     # its middle in the left half, a corner over the empty right
               box(70, 10, 80, 20),     # in the empty right half
               box(200, 10, 210, 20)])  # off the map
    assert change.seen(g, [tif]).tolist() == [True, True, False, False]
    assert change.seen(g, []) is None  # no maps: nobody can say


def test_seen_checks_the_dates(tmp_path):
    # The left half flown 100 days before the event, the right half 100 days after it.
    tif = flown_tif(tmp_path / "flown.tif", 0, 100, halves(DAY - 100, DAY + 100))
    g = frame([box(10, 10, 20, 20), box(70, 10, 80, 20),
               box(35, 10, 55, 20)])  # on the edge: its middle flown before, a corner after
    event = "2018-11-08"
    assert change.seen(g, [tif], before=event).tolist() == [True, False, False]
    assert change.seen(g, [tif], after=event).tolist() == [False, True, False]
    # Two maps (two lidar sets): a spot counts as seen before if either flew it before.
    tif2 = flown_tif(tmp_path / "flown2.tif", 0, 100, halves(np.nan, DAY - 50))
    assert change.seen(g, [tif, tif2], before=event).tolist() == [True, True, True]


def test_predictions_statuses():
    pre = frame([box(0, 0, 10, 10),    # 0 survived: the same house after
                 box(30, 0, 40, 10),   # 1 gone: nothing after
                 box(60, 0, 70, 10),   # 2 unsure: something after on 0.3 of it
                 box(90, 0, 100, 10),  # 3 gone: only a sliver after (0.05 of it)
                 box(120, 0, 130, 10),  # 4 not seen after, though a house is there
                 box(150, 0, 160, 10), box(162, 0, 172, 10)],  # 5, 6 two houses, one outline after
                id=[f"pre{k}" for k in range(7)], height=[5.0, 4.0, 4.0, 4.0, 4.0, 6.0, 6.0])
    post = frame([box(0, 0, 10, 10), box(60, 0, 63, 10), box(90, 0, 90.5, 10), box(120, 0, 130, 10),
                  box(150, 0, 172, 10),
                  box(200, 0, 210, 10),   # new
                  box(230, 0, 240, 10)],  # not seen: the lidar before didn't see this spot
                 id=[f"post{k}" for k in range(7)], height=[5.2, 3.0, 2.0, 4.0, 6.1, 3.0, 3.0])
    pre["seen_pre"] = pd.array([True] * 7, dtype="boolean")
    pre["seen_post"] = pd.array([True, True, True, True, False, True, True], dtype="boolean")
    post["seen_pre"] = pd.array([True] * 6 + [False], dtype="boolean")
    post["seen_post"] = pd.array([True] * 7, dtype="boolean")
    out = change.predictions(pre, post, change.pairs(pre, post), height="height")
    assert out["status"].tolist() == ["survived", "gone", "unsure", "gone", "not_seen", "unsure", "unsure",
                                      "new", "not_seen"]
    assert out["pre_id"].tolist()[:7] == [f"pre{k}" for k in range(7)]
    assert out["pre_id"].isna().tolist()[7:] == [True, True]
    assert out["post_id"].fillna("-").tolist() == ["post0", "-", "post1", "post2", "post3", "post4", "post4",
                                                   "post5", "post6"]
    assert out["covered"].round(2).tolist()[:7] == [1.0, 0.0, 0.3, 0.05, 1.0, 1.0, 1.0]
    # 5 and 6 merged into one outline after: its n_pre is 2 (gone 1 has no pair: 0 here).
    assert out["n_pre"].iloc[:7].fillna(0).astype(int).tolist() == [1, 0, 1, 1, 1, 2, 2]
    assert out["height_change"].round(1).tolist()[0] == 0.2
    assert np.isnan(out["height_change"].iloc[1])  # gone: no height after


def test_cli_writes_three_layers(tmp_path):
    houses = town()
    gone, new = {3, 10, 17, 30, 41, 55}, [box(600018, 4400016, 600026, 4400024)]
    pre_path, post_path, out = tmp_path / "pre.gpkg", tmp_path / "post.gpkg", tmp_path / "out.gpkg"
    frame(houses, height=np.full(len(houses), 5.0)).to_file(pre_path)
    moved = after(houses, -1.35, 0.23, gone=gone, new=new)
    frame(moved, height=np.full(len(moved), 5.0)).to_file(post_path)
    # Lidar before: flown before the event everywhere. Lidar after: everywhere but the last
    # column of houses (x from 600210), flown after the event.
    everywhere = np.full((26, 26), DAY - 100, np.float32)
    after_map = np.full((26, 26), DAY + 100, np.float32)
    after_map[:, 21:] = np.nan
    pre_tif = flown_tif(tmp_path / "pre.tif", 600000 - 10, 4400000 + 250, everywhere)
    post_tif = flown_tif(tmp_path / "post.tif", 600000 - 10, 4400000 + 250, after_map)
    change.main([str(out), "--pre", str(pre_path), "--post", str(post_path), "--pre-flown", str(pre_tif),
                 "--post-flown", str(post_tif), "--event", "2018-11-08"])
    pred = gpd.read_file(out, layer="predictions")
    status = pred["status"].value_counts().to_dict()
    # The 8 houses of the last column (56-63) aren't seen after; the 6 gone ones are gone, the
    # new box is new.
    assert status == {"survived": 64 - 8 - 6, "not_seen": 8, "gone": 6, "new": 1}
    assert pred["seen_post"].tolist().count(False) == 8
    assert set(gpd.list_layers(out)["name"]) == {"pre", "post", "predictions"}
    # The README beside it says what this run found.
    readme = (tmp_path / "out_README.md").read_text()
    assert re.search(r"sat 1\.3\d m west and 0\.2\d m north of those before", readme)  # 1.35, 0.23 ± jitter
    assert "| `gone` |" in readme and "| 6 |" in readme
    assert "`id` is the fid" in readme


def test_cli_refuses_to_overwrite_an_input(tmp_path):
    pre_path = tmp_path / "pre.gpkg"
    frame(town(n=2)).to_file(pre_path)
    with pytest.raises(SystemExit, match="must not be one of the inputs"):
        change.main([str(pre_path), "--pre", str(pre_path), "--post", str(pre_path)])
