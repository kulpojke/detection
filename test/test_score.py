"""Offline tests of score.py (the check of change.py against damage points): truth, link, score."""
import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import Point, box

import change
import score

CRS = "EPSG:6339"
DINS = ["Destroyed (>50%)", "Major (25-50%)", "Minor (10-25%)", "Affected (>0-10%)", "No Damage", "Inaccessible"]


@pytest.mark.parametrize("way,expected", [
    ("Destroyed", ["destroyed", "standing", "standing", "standing", "standing", None]),
    ("Destroyed + Major", ["destroyed", "destroyed", "standing", "standing", "standing", None]),
    ("Major left out", ["destroyed", None, "standing", "standing", "standing", None]),
])
def test_truth_three_ways(way, expected):
    assert score.truth(DINS, way).tolist() == expected  # a class no way knows is left out


def test_link_inside_near_and_far():
    buildings = gpd.GeoDataFrame(geometry=[box(0, 0, 10, 10), box(30, 0, 40, 10)], crs=CRS)
    points = gpd.GeoDataFrame(geometry=[Point(5, 5),       # inside the first
                                        Point(41.5, 5),    # 1.5 m from the second
                                        Point(45, 5)],     # 5 m from it: too far
                              crs=CRS)
    row, dist = score.link(points, buildings)
    assert row.tolist() == [0, 1, -1]
    assert dist[:2].tolist() == [0.0, pytest.approx(1.5)] and np.isnan(dist[2])


def test_score_counts_each_class_apart():
    truths = ["destroyed"] * 10 + ["standing"] * 4 + [None]
    statuses = (["gone"] * 8 + ["unsure", "survived"] + ["survived"] * 3 + ["gone"] + ["gone"])
    table, n = score.score(np.array(truths, dtype=object), np.array(statuses, dtype=object))
    assert table.loc["destroyed", "gone"] == 8 and table.loc["standing", "survived"] == 3
    assert table.to_numpy().sum() == 14  # the point without truth is left out
    assert n["destroyed → gone"] == pytest.approx(0.8)
    assert n["standing → survived"] == pytest.approx(0.75)
    assert n["gone that is destroyed"] == pytest.approx(8 / 9)
    assert n["survived that is standing"] == pytest.approx(3 / 4)
    assert n["unsure"] == pytest.approx(1 / 14)
    assert n["balanced"] == pytest.approx((0.8 + 0.75) / 2)


def test_score_leaves_not_seen_out_of_the_shares():
    table, n = score.score(np.array(["destroyed", "destroyed", "standing"], dtype=object),
                           np.array(["gone", "not_seen", "survived"], dtype=object))
    assert table.loc["destroyed", "not_seen"] == 1
    assert n["destroyed → gone"] == 1.0  # of the judged points only


def test_the_sweep_uses_change_pys_own_rule():
    # At change.py's thresholds, statuses_for gives back exactly change.py's statuses.
    pre = gpd.GeoDataFrame({"id": list("abcd")}, geometry=[box(0, 0, 10, 10), box(30, 0, 40, 10),
                                                           box(60, 0, 70, 10), box(90, 0, 100, 10)], crs=CRS)
    post = gpd.GeoDataFrame({"id": list("wxy")}, geometry=[box(0, 0, 10, 10), box(60, 0, 64, 10),
                                                           box(90, 0, 90.5, 10)], crs=CRS)
    pre["seen_pre"] = pd.array([True] * 4, dtype="boolean")
    pre["seen_post"] = pd.array([True, True, True, False], dtype="boolean")
    pred = change.predictions(pre, post, change.pairs(pre, post))
    rows = pred[pred["pre_id"].notna()].reset_index(drop=True)
    again = score.statuses_for(rows, change.SURVIVED_SHARE, change.GONE_COVER)
    assert again.tolist() == rows["status"].tolist() == ["survived", "gone", "unsure", "not_seen"]
    # And other thresholds change it: with survived from 0.4, the box covered 0.4 survives.
    assert score.statuses_for(rows, 0.4, change.GONE_COVER).tolist()[2] == "survived"
