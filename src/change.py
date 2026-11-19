#!/usr/bin/env python3
"""Before and after: which buildings are still standing (pre- vs post-event footprints).

    python src/change.py OUT.gpkg --pre PRE.gpkg --post POST.gpkg [--pre-id COLUMN] [--post-id COLUMN]
                         [--box XMIN YMIN XMAX YMAX] [--pre-flown TIF ...] [--post-flown TIF ...]
                         [--event YYYY-MM-DD] [--height COLUMN]

Both inputs are building footprints, e.g. detect.py's buildings.gpkg from lidar flown before and
after a fire. Nothing here knows about a fire, a place or a damage survey: that belongs to whoever
uses the output, so the same steps can be used again on other events.

Steps:
  1. read   both sets in one metric CRS: the pre set's, or its UTM zone if it is in degrees; with
            --box (in that CRS), only the buildings whose representative point is inside it
  2. shift  two lidar sets can sit apart as a whole. Over the Camp Fire the 2019 lidar sits 1.35 m
            west and 0.23 m north of the 2018 lidar, about what NAD83(2011) and WGS84 differ by
            there: a datum mix-up somewhere upstream. The shift is measured from clear pairs
            (estimate_shift) and taken off the post set, so the pairs line up. It is measured
            each time, never assumed: another pair of lidar sets can have none, or another
  3. seen   did each lidar see each building's spot (seen): from detect.py's flight-date maps
            (--pre-flown, --post-flown), and with --event, at the right time. "Gone" says the lidar
            after looked and found nothing; "not seen" says it never looked (no data there, or
            flown before the event), so it has no opinion
  4. pairs  every pre/post pair that shares area, with each one's share of it
  5. predictions  a status per building (survived, unsure, gone, not_seen; new for a building
            after with no pair) and the evidence for it (predictions(): shares, cover, counts,
            heights)
OUT.gpkg gets the layers pre (as read), post (moved onto the pre set's frame; moved_x and moved_y
say by how much) and predictions; both input layers also get seen_pre and seen_post.
"""
import argparse
import sys
from pathlib import Path

import tilestream  # noqa: F401  (sets PROJ_DATA and GDAL_DATA: without them geopandas can't find PROJ's database)

import geopandas as gpd  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import shapely  # noqa: E402
from shapely.geometry import box  # noqa: E402

CLEAR_SHARE = 0.7  # a clear pair: one to one, and each covers at least this share of the other
ROUGH_SHARE = 0.3  # the first, rough guess of the shift uses one-to-one pairs from this share up
MIN_PAIRS = 30     # with fewer clear pairs than this, no shift is taken off: too few to trust
MIN_SHARED = 0.01  # m²: less than this is two footprints touching, not sharing area
# The status of a building before. First guesses, to be chosen with the DINS points of a test box:
SURVIVED_SHARE = 0.5  # survived: its best pair shares at least this of each of the two
GONE_COVER = 0.1      # gone: buildings after cover less than this share of it
STATUSES = ("survived", "unsure", "gone", "not_seen", "new")


def read(path, crs=None, id_column=None):
    """Footprints from a file, in ``crs`` (default: the file's own, or its UTM zone if it is in
    degrees), with an ``id`` column: ``id_column``, else the file's ``id`` column if it has one,
    else its fid."""
    g = gpd.read_file(path, fid_as_index=True)
    if crs is None:
        crs = g.crs if g.crs.is_projected else g.estimate_utm_crs()
    g = g.to_crs(crs)
    id_column = id_column or ("id" if "id" in g.columns else None)
    g["id"] = (g[id_column] if id_column else g.index.to_series()).astype(str).to_numpy()
    g = g.reset_index(drop=True)
    g.attrs["id_from"] = f"`{id_column}`" if id_column else "the fid"
    return g


def offset(dx, dy):
    """A shift in words: '1.36 m west and 0.23 m north'."""
    return f"{abs(dx):.2f} m {'east' if dx >= 0 else 'west'} and {abs(dy):.2f} m {'north' if dy >= 0 else 'south'}"


def inside(g, area):
    """The footprints whose representative point lies inside ``area`` (a polygon)."""
    return g[g.representative_point().within(area)].reset_index(drop=True)


def pairs(pre, post):
    """Every (pre, post) pair that shares area: their row numbers, the area shared (m²) and each
    one's share of it (of_pre: the shared area over the pre building's area; of_post likewise)."""
    j = gpd.sjoin(pre[["geometry"]], post[["geometry"]], predicate="intersects")
    p = pd.DataFrame({"pre": j.index.to_numpy(), "post": j["index_right"].to_numpy()})
    p = p.sort_values(["pre", "post"]).reset_index(drop=True)
    p["shared"] = pre.geometry.values[p["pre"].to_numpy()].intersection(
        post.geometry.values[p["post"].to_numpy()]).area
    p = p[p["shared"] > MIN_SHARED].reset_index(drop=True)
    p["of_pre"] = p["shared"] / pre.area.to_numpy()[p["pre"]]
    p["of_post"] = p["shared"] / post.area.to_numpy()[p["post"]]
    return p


def one_to_one(p, share):
    """The pairs where each building overlaps only the other, and each covers at least ``share``
    of the other."""
    alone = ~p["pre"].duplicated(keep=False) & ~p["post"].duplicated(keep=False)
    return p[alone & (p["of_pre"] >= share) & (p["of_post"] >= share)]


def mad(v):
    """Median absolute deviation: how far the values typically lie from their median."""
    return float(np.median(np.abs(v - np.median(v))))


def estimate_shift(pre, post, rounds=5):
    """How far the post set sits from the pre set as a whole: (dx, dy, spread_x, spread_y, n).

    dx, dy: the median of (post centre − pre centre) over the clear pairs; spread: the median
    absolute deviation around it; n: the number of clear pairs. A median, so the pairs whose
    shapes differ (a tree's bite in one of them) don't pull it.
    A shift of a few metres leaves few clear pairs, so the first, rough guess comes from looser
    pairs (ROUGH_SHARE); each next round moves the post set by the guess so far and measures what
    is left from the clear pairs (CLEAR_SHARE), until that is under 1 cm. With fewer than
    MIN_PAIRS pairs, no shift: (0, 0, nan, nan, n)."""
    dx = dy = 0.0
    for r in range(rounds):
        moved = gpd.GeoDataFrame(geometry=post.geometry.translate(-dx, -dy), crs=post.crs)
        p = one_to_one(pairs(pre, moved), ROUGH_SHARE if r == 0 else CLEAR_SHARE)
        if len(p) < MIN_PAIRS:
            return 0.0, 0.0, float("nan"), float("nan"), len(p)
        a = pre.geometry.iloc[p["pre"].to_numpy()].centroid
        b = moved.geometry.iloc[p["post"].to_numpy()].centroid
        ex, ey = b.x.to_numpy() - a.x.to_numpy(), b.y.to_numpy() - a.y.to_numpy()
        left_x, left_y = float(np.median(ex)), float(np.median(ey))
        dx, dy = dx + left_x, dy + left_y
        if r > 0 and np.hypot(left_x, left_y) < 0.01:
            break
    return dx, dy, mad(ex), mad(ey), len(p)


def best_share(p, n_pre):
    """For each pre building, the best of its pairs: the smaller of the two shares, 0 without any."""
    both = np.minimum(p["of_pre"], p["of_post"])
    return both.groupby(p["pre"]).max().reindex(range(n_pre), fill_value=0.0)


def day_number(date):
    """A date (YYYY-MM-DD) as days since 1970-01-01, the unit of detect.py's flight-date maps."""
    return int((np.datetime64(str(date), "D") - np.datetime64("1970-01-01", "D")).astype(int))


def seen(g, maps, before=None, after=None):
    """Whether lidar saw each footprint of ``g``, from ``maps`` (detect.py's flown/<set>.tif:
    10 m cells, band 1 the first point flown there, band 2 the last, in days since 1970-01-01;
    NaN without points). With ``before`` (a date) a cell counts only if it was flown all before
    it (its last point); with ``after``, only if all on or after it (its first point).

    Seen: some map has points, at the right time, in the cell under the footprint's representative
    point, and wherever a map has points under its outline's corners, they are at the right time
    too (a building on the edge between two flights, one before and one after, is not seen). A
    corner over a cell without points doesn't count against it: the maps cover only the squares
    that were run, and a building at the edge of the run sticks out of them, though detect.py
    saw it whole (its windows read 50 m around each square).
    None without maps: then nobody can say, and every building counts as seen."""
    if not maps:
        return None
    from osgeo import gdal
    from pyproj import CRS, Transformer
    gdal.UseExceptions()
    xy, owner = shapely.get_coordinates(g.geometry.values, return_index=True)
    rep = g.representative_point()
    owner = np.concatenate([owner, np.arange(len(g))])
    is_middle = np.r_[np.zeros(len(xy), bool), np.ones(len(g), bool)]
    x, y = np.concatenate([xy[:, 0], rep.x.to_numpy()]), np.concatenate([xy[:, 1], rep.y.to_numpy()])
    has = np.zeros(len(x), bool)   # some map has points in the cell of this point
    good = np.zeros(len(x), bool)  # ... flown at the right time
    for path in maps:
        ds = gdal.Open(str(path))
        gx, rx, _, gy, _, ry = ds.GetGeoTransform()
        first, last = ds.GetRasterBand(1).ReadAsArray(), ds.GetRasterBand(2).ReadAsArray()
        crs = CRS.from_wkt(ds.GetProjection())
        px, py = (x, y) if crs == g.crs else Transformer.from_crs(g.crs, crs, always_xy=True).transform(x, y)
        i, j = np.floor((py - gy) / ry).astype(int), np.floor((px - gx) / rx).astype(int)
        inside = (i >= 0) & (i < first.shape[0]) & (j >= 0) & (j < first.shape[1])
        f, l = np.full(len(x), np.nan), np.full(len(x), np.nan)
        f[inside], l[inside] = first[i[inside], j[inside]], last[i[inside], j[inside]]
        on_time = np.isfinite(f)
        if before is not None:
            on_time &= l < day_number(before)
        if after is not None:
            on_time &= f >= day_number(after)
        has |= np.isfinite(f)
        good |= on_time
    middle_ok = pd.Series(good[is_middle], index=owner[is_middle])
    wrong_time = pd.Series(has & ~good).groupby(owner).any()
    return (middle_ok & ~wrong_time).reindex(range(len(g)), fill_value=False).to_numpy(bool)


def unseen(rows):
    """True where a lidar didn't see the spot: seen_pre or seen_post False (NA counts as seen)."""
    return ((rows["seen_pre"] == False) | (rows["seen_post"] == False)).fillna(False).to_numpy(bool)  # noqa: E712


def seen_columns(g, pre_maps, post_maps, event=None, shift=(0.0, 0.0)):
    """Add seen_pre and seen_post to ``g`` (pre or post footprints, in the pre set's frame): did
    the lidar before see each one's spot (before ``event``), did the lidar after (on or after it).
    The post lidar's maps are in its own frame, so they are read at ``g`` moved by ``shift`` (how
    far the post set sat from the pre set). NA without maps."""
    on_post = gpd.GeoDataFrame(geometry=g.geometry.translate(*shift), crs=g.crs)
    for column, where, maps, when in (("seen_pre", g, pre_maps, {"before": event}),
                                      ("seen_post", on_post, post_maps, {"after": event})):
        s = seen(where, maps, **when)
        g[column] = pd.array([pd.NA] * len(g) if s is None else s, dtype="boolean")
    return g


def predictions(pre, post, p, height=None):
    """What became of each building: a row per building before (its footprint), and a row per
    building after that shares no area with any before (its footprint).

    Columns: status (see STATUSES), pre_id, post_id (for a building before: its best pair's),
    seen_pre and seen_post (from seen_columns, if the frames have them), and the evidence:
    best_share (the smaller of the best pair's two shares), of_pre and of_post (the best pair's
    shares), covered (the share of the building before that buildings after cover; they don't
    overlap one another, so their shared areas add up), n_post (how many buildings after it
    shares area with), n_pre (how many buildings before its best pair shares area with: 2 or more
    when two houses merged into one outline), and with ``height`` (a column of both sets)
    height_pre, height_post (its best pair's) and height_change (after − before).

    A row is not_seen when either lidar didn't see its spot (seen_pre or seen_post False; NA,
    no maps, counts as seen)."""
    n = len(pre)
    both = np.minimum(p["of_pre"], p["of_post"])
    best = p.assign(both=both).sort_values(["pre", "both"], ascending=[True, False]).drop_duplicates("pre")
    best = best.set_index("pre").reindex(range(n))
    has = best["post"].notna().to_numpy()
    best_post = best["post"].fillna(-1).astype(int).to_numpy()

    rows = pd.DataFrame({"pre_id": pre["id"].to_numpy()})
    rows["post_id"] = pd.array([post["id"].iat[k] if k >= 0 else None for k in best_post], dtype="string")
    for column in ("seen_pre", "seen_post"):
        rows[column] = pre[column].array if column in pre else pd.array([pd.NA] * n, dtype="boolean")
    rows["best_share"] = best["both"].fillna(0.0).to_numpy()
    rows["of_pre"] = best["of_pre"].to_numpy()
    rows["of_post"] = best["of_post"].to_numpy()
    rows["covered"] = (p.groupby("pre")["shared"].sum().reindex(range(n), fill_value=0.0).to_numpy()
                       / pre.area.to_numpy()).clip(max=1.0)
    rows["n_post"] = pd.array(p.groupby("pre").size().reindex(range(n), fill_value=0).to_numpy(), dtype="Int64")
    n_pre = p.groupby("post").size()
    rows["n_pre"] = pd.array([int(n_pre[k]) if k >= 0 else pd.NA for k in best_post], dtype="Int64")
    if height:
        rows["height_pre"] = pre[height].to_numpy(float)
        rows["height_post"] = np.where(has, post[height].to_numpy(float)[np.maximum(best_post, 0)], np.nan)
        rows["height_change"] = rows["height_post"] - rows["height_pre"]
    rows["status"] = np.select([unseen(rows), rows["best_share"] >= SURVIVED_SHARE,
                                rows["covered"] < GONE_COVER], ["not_seen", "survived", "gone"], "unsure")
    rows = gpd.GeoDataFrame(rows, geometry=pre.geometry.to_numpy(), crs=pre.crs)

    # The buildings after with no pair: new, unless a lidar didn't see the spot (the lidar before
    # was flown there only after the event, say: then a survivor would look new).
    alone = np.setdiff1d(np.arange(len(post)), p["post"].to_numpy())
    extra = pd.DataFrame({"post_id": pd.array(post["id"].to_numpy()[alone], dtype="string")})
    for column in ("seen_pre", "seen_post"):
        extra[column] = (post[column].array[alone] if column in post
                         else pd.array([pd.NA] * len(alone), dtype="boolean"))
    extra["status"] = np.where(unseen(extra), "not_seen", "new")
    if height:
        extra["height_post"] = post[height].to_numpy(float)[alone]
    extra = gpd.GeoDataFrame(extra, geometry=post.geometry.to_numpy()[alone], crs=pre.crs)
    out = pd.concat([rows, extra], ignore_index=True)
    columns = ["status", "pre_id", "post_id", "seen_pre", "seen_post", "best_share", "of_pre", "of_post",
               "covered", "n_post", "n_pre"] + (["height_pre", "height_post", "height_change"] if height else [])
    return out[columns + ["geometry"]]


def write_readme(path, argv, a, pre, post, shift, counts, height, id_from):
    """``path``: what OUT.gpkg holds and where it came from, for this run (its numbers, its inputs).
    ``id_from``: where the IDs of the two sets came from (read() says, in ``attrs``)."""
    import datetime
    import shlex
    from detect import git_version
    dx, dy, sx, sy, n = shift
    moved = n >= MIN_PAIRS

    def seen_line(which, maps, column):
        option = "--pre-flown" if column == "seen_pre" else "--post-flown"
        if not maps:
            return f"- **Lidar {which}:** no flight-date maps (`{option}`), so every building counts as seen by it."
        return (f"- **Lidar {which}:** {', '.join(f'`{m}`' for m in maps)}. It saw {int(pre[column].sum()):,} of "
                f"the {len(pre):,} buildings before and {int(post[column].sum()):,} of the {len(post):,} after.")

    status_rows = {
        "survived": f"its best pair shares at least {SURVIVED_SHARE:g} of each of the two",
        "unsure": "something after is there, but not enough for survived: a part, a piece, or two houses "
                  "seen as one outline (see n_post, n_pre)",
        "gone": f"both lidars saw the spot, and buildings after cover less than {GONE_COVER:g} of it",
        "not_seen": "a lidar didn't see the spot (seen_pre or seen_post false): no opinion. For a building "
                    "after with no pair: the lidar before didn't see that spot",
        "new": "a building after that shares no area with any building before, where both lidars saw the spot",
    }
    text = f"""# Before and after: {Path(a.out).name}

Made {datetime.datetime.now():%Y-%m-%d %H:%M} with `src/change.py` from
<https://github.com/kulpojke/detection>, code version {git_version()}. A first, automatic result: the
thresholds are first guesses, not yet chosen against damage data (see Limits).

    {shlex.join(['python', 'src/change.py', *argv])}

## Inputs

- **Before:** `{a.pre}`: {len(pre):,} buildings{' in the box' if a.box else ''}; `id` is {id_from[0]}.
- **After:** `{a.post}`: {len(post):,} buildings{' in the box' if a.box else ''}; `id` is {id_from[1]}.
- **Area:** {f'the box {a.box} ({pre.crs.name}): the buildings whose representative point is inside it' if a.box else 'all buildings of both'}.
{seen_line('before', a.pre_flown, 'seen_pre')}
{seen_line('after', a.post_flown, 'seen_post')}
- **Event:** {f'{a.event}: the lidar before counts only where it was flown before it, the lidar after only where flown on or after it' if a.event is not None else 'none given: the flight dates are not checked'}.
- **CRS:** {pre.crs.name} (EPSG:{pre.crs.to_epsg()}); areas in m², heights in m.

## The shift

Two lidar sets can sit apart as a whole, by a metre or more (a datum mix-up somewhere upstream, for
example: NAD83(2011) and WGS84 differ by about 1-1.5 m in the western US). The shift is measured
from clear pairs: one building before and one after, overlapping only each other, each covering at
least {CLEAR_SHARE:g} of the other. It is the median of (centre after − centre before); a first,
rough round uses looser pairs (from {ROUGH_SHARE:g}), so a shift of a few metres is found too.

{f'**Measured: the buildings after sat {offset(dx, dy)} of those before** (spread, the median absolute deviation: {sx:.2f} / {sy:.2f} m; {n:,} clear pairs). That is taken off: layer `post` is moved {offset(-dx, -dy)} (`moved_x`, `moved_y`).' if moved else f'**Not measured: only {n} clear pairs, fewer than {MIN_PAIRS}.** Nothing is moved; if the two sets do sit apart, the shares below are too low.'}

## Layers

- `pre`: the buildings before as read, with `id`, `seen_pre` and `seen_post` added.
- `post`: the buildings after, moved by the shift onto the frame of those before, with `id`,
  `moved_x`, `moved_y`, `seen_pre`, `seen_post`.
- `predictions`: one row per building before (its footprint before), and one per building after
  that shares no area with any building before (its footprint after).

## Status

| status | when | count |
| --- | --- | --- |
""" + "".join(f"| `{s}` | {status_rows[s]} | {counts.get(s, 0):,} |\n" for s in STATUSES) + f"""
"Gone" says the lidar after looked and found nothing; "not seen" says it never looked, so it has no
opinion (no lidar there, or flown on the wrong side of the event). Seen means: one of the flight-date
maps (10 m cells) has points, flown at the right time, under the footprint's representative point,
and wherever a map has points under its corners, they are at the right time too. A corner over a
cell without points doesn't count against it (the maps cover only the squares that were run).

## Columns of `predictions`

- `status`: see above.
- `pre_id`, `post_id`: the building before (empty for a building after with no pair), and its best
  pair after (the one sharing the most of each, by the smaller of the two shares).
- `seen_pre`, `seen_post`: did the lidar before / after see the spot (empty without maps).
- `best_share`: the smaller of the best pair's two shares (0 without a pair).
- `of_pre`, `of_post`: the best pair's shares: the area they share over the area of the building
  before, and over that of the building after.
- `covered`: the share of the building before that buildings after cover, all of them together.
- `n_post`: how many buildings after share area with the building before; `n_pre`: how many
  buildings before its best pair shares area with (2 or more: houses merged into one outline after).
""" + (f"""- `height_pre`, `height_post`, `height_change`: `{height}` of the building before, of its best pair
  after, and after − before (m). Evidence only: the status doesn't use it yet.
""" if height else "") + f"""
## Limits

- **The thresholds are first guesses:** `SURVIVED_SHARE` {SURVIVED_SHARE:g} and `GONE_COVER` {GONE_COVER:g}
  in `src/change.py`. They are to be chosen against damage data in a test area, and scored outside it.
- **Survived means a building of about the same outline stands there after,** not that it is
  undamaged: a damaged building that still stands can be survived.
- **`new` is not always a building:** a detector run on post-event lidar finds trailers, debris
  piles and vehicles too.
- Seen is decided on 10 m cells: a data gap smaller than a cell, under a building, goes unnoticed.
"""
    Path(path).write_text(text)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("out", help="output GeoPackage (rewritten each run)")
    ap.add_argument("--pre", required=True, help="footprints before (GeoPackage, GeoJSON, ...)")
    ap.add_argument("--post", required=True, help="footprints after")
    ap.add_argument("--pre-id", help="the pre set's ID column (default: id if it has one, else its fid)")
    ap.add_argument("--post-id", help="the post set's ID column (likewise)")
    ap.add_argument("--box", type=float, nargs=4, metavar=("XMIN", "YMIN", "XMAX", "YMAX"),
                    help="only the buildings whose representative point is inside this box (in the pre set's CRS)")
    ap.add_argument("--pre-flown", nargs="+", default=[], metavar="TIF",
                    help="flight-date maps of the lidar before (detect.py's flown/<set>.tif): where it saw")
    ap.add_argument("--post-flown", nargs="+", default=[], metavar="TIF", help="likewise, of the lidar after")
    ap.add_argument("--event", type=lambda s: np.datetime64(s, "D"), metavar="YYYY-MM-DD",
                    help="the event's date (a fire's start): the lidar before must be flown before it, after on or after it")
    ap.add_argument("--height", help="the height column of both sets (default: height, if both have it)")
    a = ap.parse_args(argv)
    out = Path(a.out)
    if out.resolve() in (Path(a.pre).resolve(), Path(a.post).resolve()):
        sys.exit("change.py: OUT must not be one of the inputs")

    # 1. read
    pre = read(a.pre, id_column=a.pre_id)
    post = read(a.post, crs=pre.crs, id_column=a.post_id)
    id_from = (pre.attrs["id_from"], post.attrs["id_from"])
    area = box(*a.box) if a.box else None
    if area is not None:
        pre, post_in = inside(pre, area), inside(post, area)
    else:
        post_in = post
    print(f"1. read: {len(pre):,} buildings before, {len(post_in):,} after ({pre.crs.name})")

    # 2. shift, measured on the buildings in the box, taken off all of post
    dx, dy, sx, sy, n = estimate_shift(pre, post_in)
    if n < MIN_PAIRS:
        print(f"2. shift: only {n} clear pairs, fewer than {MIN_PAIRS}: no shift taken off")
    else:
        print(f"2. shift: the buildings after sat {offset(dx, dy)} of those before (spread {sx:.2f} / "
              f"{sy:.2f} m, {n} clear pairs); taken off")
    post["geometry"] = post.geometry.translate(-dx, -dy)
    post["moved_x"], post["moved_y"] = round(-dx, 3), round(-dy, 3)
    if area is not None:
        post = inside(post, area)

    # 3. seen: did each lidar see each building's spot, at the right time
    for g in (pre, post):
        seen_columns(g, a.pre_flown, a.post_flown, a.event, shift=(dx, dy))
    for column, maps, which in (("seen_pre", a.pre_flown, "before"), ("seen_post", a.post_flown, "after")):
        if maps:
            print(f"3. seen by the lidar {which}: {int(pre[column].sum()):,} of the {len(pre):,} buildings "
                  f"before, {int(post[column].sum()):,} of the {len(post):,} after")
        else:
            print(f"3. seen by the lidar {which}: no flight-date maps, so every building counts as seen")

    # 4. pairs
    p = pairs(pre, post)
    best = best_share(p, len(pre))
    print(f"4. pairs: {len(p):,}; {p['pre'].nunique():,} of {len(pre):,} buildings before share area "
          f"with one after. The best share of each building before (the smaller of the two shares):")
    labels = ["none", "up to 0.1", "0.1-0.3", "0.3-0.5", "0.5-0.7", "0.7-0.9", "0.9-1"]
    counts = pd.cut(best, [-1, 0, 0.1, 0.3, 0.5, 0.7, 0.9, 1.0], labels=labels).value_counts().sort_index()
    for label, count in counts.items():
        print(f"   {label:>9}: {count:,}")

    # 5. predictions
    height = a.height or ("height" if "height" in pre and "height" in post else None)
    pred = predictions(pre, post, p, height=height)
    counts = pred["status"].value_counts()
    print("5. predictions: " + ", ".join(f"{s} {counts.get(s, 0):,}" for s in STATUSES))

    out.unlink(missing_ok=True)
    pre.to_file(out, layer="pre", driver="GPKG")
    post.to_file(out, layer="post", driver="GPKG")
    pred.to_file(out, layer="predictions", driver="GPKG")
    readme = out.with_name(f"{out.stem}_README.md")
    write_readme(readme, sys.argv[1:] if argv is None else argv, a, pre, post, (dx, dy, sx, sy, n),
                 counts.to_dict(), height, id_from)
    print(f"wrote {out} (layers pre, post, predictions) and {readme.name}")


if __name__ == "__main__":
    main()
