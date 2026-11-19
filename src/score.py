#!/usr/bin/env python3
"""Score change.py's predictions against damage points (CAL FIRE DINS), to choose its thresholds.

    python src/score.py CHANGE.gpkg --points DINS.parquet [--box XMIN YMIN XMAX YMAX] [--damage COLUMN]
                        [--out POINTS.gpkg]

This is the check, not the model: the damage points never go into change.py. Choose the
thresholds in a test box, then score with them outside it, where nothing was chosen.

  1. link   each damage point to the building before it lies in, or the nearest within
            LINK_DISTANCE m. In central Paradise DINS points sit typically 4 m from their
            building's centre; further out than 2 m they start to reach the neighbours. Points
            with no building before that close are counted apart (the lidar before missed the
            structure, or the point sits at the street): they can't say anything about the model
  2. truth  the damage classes as destroyed or standing, three ways (WAYS): Destroyed alone is
            destroyed (so Major counts as standing); Destroyed and Major are destroyed; Major is
            left out. A building with two points is scored twice, once per point
  3. score  per way, the table of truth by status, and per class: how many of the destroyed
            came out gone, how many of the standing survived. Survivors are rare, so one overall
            share would hide them. not_seen is the model saying nothing: counted, not scored
  4. sweep  the statuses again for other thresholds (change.classify on the evidence columns,
            so exactly the model's rule), and the same numbers for each pair
"""
import argparse
import itertools
import sys

import change  # sets PROJ_DATA, like everything that imports tilestream

import geopandas as gpd  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from shapely.geometry import box  # noqa: E402

LINK_DISTANCE = 2.0  # m: a point belongs to the building it lies in, or the nearest this close
# The first word of a DINS damage class ("Destroyed (>50%)", "No Damage", ...), and what each
# way of scoring makes of it: destroyed, standing, or None (left out).
WAYS = {
    "Destroyed": {"destroyed": "destroyed", "major": "standing", "minor": "standing",
                  "affected": "standing", "no": "standing"},
    "Destroyed + Major": {"destroyed": "destroyed", "major": "destroyed", "minor": "standing",
                          "affected": "standing", "no": "standing"},
    "Major left out": {"destroyed": "destroyed", "major": None, "minor": "standing",
                       "affected": "standing", "no": "standing"},
}
SWEEP_WAY = "Major left out"  # the thresholds are chosen on the clear classes only
SWEEP = {"survived_share": (0.3, 0.4, 0.5, 0.6, 0.7), "gone_cover": (0.05, 0.1, 0.2, 0.3)}
JUDGED = ("survived", "unsure", "gone")  # the statuses that say something (not_seen doesn't)


def damage_word(values):
    """The first word of each damage class, lower case: 'destroyed', 'major', ..., 'no'."""
    return pd.Series(values, dtype="string").str.split().str[0].str.lower()


def truth(values, way):
    """Each damage class as 'destroyed', 'standing' or None (left out, or a class the way doesn't
    know, such as 'Inaccessible')."""
    return damage_word(values).map(WAYS[way]).astype(object).where(lambda s: s.notna(), None).to_numpy()


def link(points, buildings, distance=LINK_DISTANCE):
    """For each point, the row of the building it lies in or the nearest one within ``distance``
    m (-1 for none), and how far it is (0 inside)."""
    j = gpd.sjoin_nearest(points[["geometry"]], buildings[["geometry"]], how="left",
                          max_distance=distance, distance_col="dist")
    j = j[~j.index.duplicated()]  # a point as near to two buildings: the first
    return j["index_right"].fillna(-1).astype(int).to_numpy(), j["dist"].to_numpy()


def score(truths, statuses):
    """The table of truth (destroyed, standing) by status, and the numbers per class:
    destroyed → gone and standing → survived (of the judged points), what share of gone is
    destroyed and of survived is standing, the share unsure, and the balanced score (the mean
    of the first two). Points with no truth are left out."""
    keep = pd.notna(truths)
    table = pd.crosstab(pd.Categorical(np.asarray(truths)[keep], ["destroyed", "standing"]),
                        pd.Categorical(np.asarray(statuses)[keep], list(change.STATUSES[:4])), dropna=False,
                        rownames=["truth"], colnames=["status"])
    d, s = table.loc["destroyed"], table.loc["standing"]
    nd, ns = sum(d[k] for k in JUDGED), sum(s[k] for k in JUDGED)

    def share(a, b):
        return a / b if b else float("nan")

    numbers = {"destroyed → gone": share(d["gone"], nd), "standing → survived": share(s["survived"], ns),
               "gone that is destroyed": share(d["gone"], d["gone"] + s["gone"]),
               "survived that is standing": share(s["survived"], s["survived"] + d["survived"]),
               "unsure": share(d["unsure"] + s["unsure"], nd + ns)}
    numbers["balanced"] = (numbers["destroyed → gone"] + numbers["standing → survived"]) / 2
    return table, numbers


def statuses_for(rows, survived_share, gone_cover):
    """The statuses of buildings before for other thresholds: change.py's own rule."""
    return change.classify(rows["best_share"].to_numpy(float), rows["covered"].to_numpy(float),
                           (rows["status"] == "not_seen").to_numpy(), survived_share, gone_cover)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("change", help="change.py's output (its layer predictions)")
    ap.add_argument("--points", required=True, help="damage points (GeoParquet, GeoPackage, ...), e.g. CAL FIRE DINS")
    ap.add_argument("--damage", default="DAMAGE", help="their damage class column (default DAMAGE)")
    ap.add_argument("--box", type=float, nargs=4, metavar=("XMIN", "YMIN", "XMAX", "YMAX"),
                    help="only the points inside this box (in the predictions' CRS)")
    ap.add_argument("--by", help="also score each value of this column of the points apart (e.g. STRUCTURECATEGORY)")
    ap.add_argument("--out", help="write the linked points, with their truth and status, to this GeoPackage")
    a = ap.parse_args(argv)

    pred = gpd.read_file(a.change, layer="predictions")
    rows = pred[pred["pre_id"].notna()].reset_index(drop=True)  # the buildings before
    points = (gpd.read_parquet(a.points) if a.points.endswith(".parquet") else gpd.read_file(a.points)).to_crs(rows.crs)
    if a.box:
        points = points[points.within(box(*a.box))].reset_index(drop=True)
    print(f"{len(points):,} damage points{' in the box' if a.box else ''}: "
          + ", ".join(f"{k} {v:,}" for k, v in points[a.damage].value_counts().items()))

    # 1. link
    row, dist = link(points, rows)
    linked = row >= 0
    print(f"1. link: {linked.sum():,} on a building before or within {LINK_DISTANCE:g} m of one "
          f"({(dist == 0).sum():,} inside); {(~linked).sum():,} with none that close, not scored "
          f"({', '.join(f'{k} {v:,}' for k, v in points.loc[~linked, a.damage].value_counts().items())})")
    points["building"] = pd.array(np.where(linked, rows["pre_id"].to_numpy()[np.maximum(row, 0)], None), dtype="string")
    points["dist"] = dist
    status = np.where(linked, rows["status"].to_numpy()[np.maximum(row, 0)], None)
    points["status"] = status

    # 2, 3. truth and score, three ways
    for way in WAYS:
        t = truth(points[a.damage], way)
        points[f"truth_{way.lower().replace(' + ', '_').replace(' ', '_')}"] = t
        table, numbers = score(t[linked], status[linked])
        print(f"\n2-3. way '{way}':")
        print("   " + table.to_string().replace("\n", "\n   "))
        print("   " + "; ".join(f"{k} {v:.2f}" for k, v in numbers.items()))
        if a.by:
            for value, group in points[linked].groupby(a.by):
                k = group.index.to_numpy()
                table, numbers = score(t[k], status[k])
                n = table.to_numpy().sum()
                print(f"   {a.by} {value} ({n:,} points; destroyed/standing: {table.loc['destroyed'].sum():,}/"
                      f"{table.loc['standing'].sum():,}): " + "; ".join(f"{k} {v:.2f}" for k, v in numbers.items()))

    # 4. sweep
    print(f"\n4. sweep, way '{SWEEP_WAY}' (statuses again from the evidence, change.classify):")
    t = truth(points.loc[linked, a.damage], SWEEP_WAY)
    results = []
    for s, g in itertools.product(SWEEP["survived_share"], SWEEP["gone_cover"]):
        st = statuses_for(rows, s, g)[row[linked]]
        _, numbers = score(t, st)
        results.append({"survived_share": s, "gone_cover": g, **numbers})
    sweep = pd.DataFrame(results)
    current = (sweep["survived_share"] == change.SURVIVED_SHARE) & (sweep["gone_cover"] == change.GONE_COVER)
    sweep["now"] = np.where(current, "<- change.py now", "")
    print("   " + sweep.round(3).to_string(index=False).replace("\n", "\n   "))

    if a.out:
        points.to_file(a.out, driver="GPKG")
        print(f"\nwrote {a.out}")


if __name__ == "__main__":
    sys.exit(main())
