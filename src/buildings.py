#!/usr/bin/env python3
"""Building detection for tilestream: height layers, roof-or-tree layers, and a table of
objects (buildings and tree crowns) with the features a building rule will use.

Rasters on a 0.5 m grid:
  dtm           ground height: mean of the ground points (class 2) in each pixel; pixels
                without ground points get the height of the nearest pixel that has them
  dsm           top height: highest point in each pixel (noise dropped). The scan pattern
                leaves ~2% of pixels empty by chance; those get the height of the nearest
                pixel with a point if it is within DSM_FILL. Bigger holes (water, dropouts) stay NaN.
  ndsm          height above ground, dsm - dtm
  ground_gap    distance (m) to the nearest pixel with a ground point: 0 where the ground was
                measured, growing toward the middle of a hole. A roof stops every pulse, so a
                building leaves a hole with straight edges; trees leave scattered ground points.
                Where ground_gap > ~1 m, the dtm is filled in, not measured.
  multi_return  share of laser pulses that gave more than one return, over each pixel's 3 x 3
                neighbourhood: near 0 on roofs (a hard surface stops the pulse), high in trees
  roughness     how sharply the dsm bends, |Laplacian| (m): ~0 on flat and evenly sloping
                roofs, high in tree crowns
  segments      the objects: 1 = grown from a roof seed, 2 = grown from a tree top, 0 = no
                object, or the edge between two objects (so each crown and roof stands out)

Objects: everything higher than MIN_HEIGHT, split into one object per building or tree crown
by a watershed on the ndsm. The flood starts from two kinds of seeds:
  roof seeds  covered holes (high pixels further than HOLE_GAP from any ground point) that look
              like roofs: few split pulses, smooth. One seed per roof, so ridges and dormers
              don't cut a building into pieces.
  tree tops   local maxima of the lightly smoothed ndsm, away from the roof seeds
Each high pixel joins the seed it is "downhill" from, so boundaries fall in the valleys
between crowns and roofs.

Table "objects": one row per object of at least MIN_AREA. Its point (x, y) is inside the
object (a roof's pixel furthest from any ground point, a tree's top); the window holding that
point keeps the row. Columns:
  roof_seed (1 or 0), area (m^2), height (median ndsm), hole_depth (largest ground_gap),
  hole_share (share of pixels further than HOLE_GAP from any ground point), rectangularity
  (1 for a rectangle, lower for ragged shapes), multi_return (share of split pulses),
  roughness (median)

Needs the fields X, Y, Z, Classification, ReturnNumber and NumberOfReturns.

    python src/tilestream.py run out/ --ept WA_NorthEast_2_B22 --bbox XMIN YMIN XMAX YMAX \\
        --analysis src/buildings.py
    python src/tilestream.py merge out/
"""
import numpy as np
from scipy import ndimage
from scipy.spatial import ConvexHull
from skimage.segmentation import watershed

from tilestream import Raster

RES = 0.5               # m, fine enough for 2 x 3 m sheds
HALO = 50.0             # m. Under a building the dtm is filled from the ground around it, and an
                        # object must be seen whole: 50 m covers buildings up to ~100 m wide.
DSM_FILL = 1.0          # m, fills dsm gaps up to ~2 m across
MIN_HEIGHT = 2.0        # m above the ground
HOLE_GAP = 0.5          # m: no ground point in the pixel or its 4 neighbours
MIN_AREA = 1.0          # m^2; a 2 x 3 m shed's covered hole is ~2 m^2
ROOF_MULTI = 0.15       # a roof seed has a smaller share of split pulses than this,
ROOF_ROUGH = 0.5        # and a median roughness below this (m)
TREE_SMOOTH = 0.5       # m, Gaussian smoothing of the ndsm before finding tree tops
TREE_TOP_RADIUS = 2.5   # m, a tree top is the highest point within this distance; 1.5 m split
                        # big crowns into 2-3 pieces
GROUND = 2
NOISE = (7, 18)         # low and high noise
FEATURES = ("area", "height", "hole_depth", "hole_share", "rectangularity", "multi_return", "roughness")


def analyze(pts, w):
    x0, ytop, nx, ny = w.grid(RES)
    # flat pixel index of each point; a point exactly on the grid's bottom edge would get row ny
    row = np.minimum(((ytop - pts["Y"]) / RES).astype(np.int64), ny - 1)
    col = ((pts["X"] - x0) / RES).astype(np.int64)
    pixel = row * nx + col

    ground = pts["Classification"] == GROUND
    dtm, ground_gap = fill_from_nearest(mean_per_pixel(pixel[ground], pts["Z"][ground], ny, nx))
    keep = ~np.isin(pts["Classification"], NOISE)
    dsm, dsm_gap = fill_from_nearest(highest_per_pixel(pixel[keep], pts["Z"][keep], ny, nx))
    dsm[dsm_gap > DSM_FILL] = np.nan
    ndsm = dsm - dtm

    # roof or tree: every pulse has exactly one first return; count the pulses that split
    first = keep & (pts["ReturnNumber"] == 1)
    pulses = np.bincount(pixel[first], minlength=ny * nx).reshape(ny, nx)
    split = np.bincount(pixel[first & (pts["NumberOfReturns"] > 1)], minlength=ny * nx).reshape(ny, nx)
    box = np.ones((3, 3), np.int64)  # integer sums over 3 x 3, exact
    with np.errstate(invalid="ignore"):  # 0 / 0 -> NaN where no pulses
        multi_return = (ndimage.correlate(split, box, mode="constant") /
                        ndimage.correlate(pulses, box, mode="constant")).astype(np.float32)
    roughness = np.abs(ndimage.laplace(dsm))

    layers = {"dtm": dtm, "dsm": dsm, "ndsm": ndsm, "ground_gap": ground_gap,
              "multi_return": multi_return, "roughness": roughness}
    grids = {**layers, "pulses": pulses, "split": split}  # what describe() reads
    segments, n_roofs = segment(grids)

    out = {name: Raster(r, x0, ytop, RES, nodata=np.nan) for name, r in layers.items()}
    out["segments"] = Raster(segment_map(segments, n_roofs), x0, ytop, RES, nodata=0)
    out["objects"] = objects_table(segments, n_roofs, grids, x0, ytop)
    return out


def segment(g):
    """Split everything higher than MIN_HEIGHT into objects (see the module docstring).
    Returns the label image (0 = no object) and n_roofs: labels 1..n_roofs grew from roof seeds."""
    high = g["ndsm"] > MIN_HEIGHT
    roofs = np.zeros(high.shape, np.int32)
    n_roofs = 0
    holes, _ = ndimage.label(high & (g["ground_gap"] > HOLE_GAP))
    for _, r, c in pixels(holes):
        f = describe(r, c, g)
        if f["area"] >= MIN_AREA and f["multi_return"] < ROOF_MULTI and f["roughness"] < ROOF_ROUGH:
            n_roofs += 1
            roofs[r, c] = n_roofs

    smooth = ndimage.gaussian_filter(np.where(high, g["ndsm"], 0), TREE_SMOOTH / RES)
    peak = smooth == ndimage.maximum_filter(smooth, 2 * round(TREE_TOP_RADIUS / RES) + 1)
    near_roof = ndimage.binary_dilation(roofs > 0, iterations=2)  # 1 m: eaves and parapets aren't tree tops
    tops, _ = ndimage.label(peak & high & ~near_roof)  # touching peak pixels (a tie) make one top

    markers = np.where(tops > 0, tops + n_roofs, roofs)
    # a small patch next to a taller tree has no top of its own (the tree is higher within
    # TREE_TOP_RADIUS), and the flood never reaches a patch without a seed: seed it at its highest pixel
    patches, n_patches = ndimage.label(high)
    seeded = np.zeros(n_patches + 1, bool)
    seeded[patches[markers > 0]] = True
    unseeded = np.flatnonzero(~seeded[1:]) + 1
    for k, rc in enumerate(ndimage.maximum_position(g["ndsm"], patches, unseeded), markers.max() + 1):
        markers[rc] = k
    return watershed(np.where(high, -g["ndsm"], 0), markers, mask=high), n_roofs


def objects_table(segments, n_roofs, g, x0, ytop):
    """The objects table (see the module docstring), as {column: array}."""
    rows = []
    for i, r, c in pixels(segments):
        if len(r) * RES ** 2 < MIN_AREA:
            continue
        roof = i <= n_roofs
        # a point inside the object: a roof's pixel furthest from any ground point, a tree's top
        k = np.argmax(g["ground_gap"][r, c] if roof else g["ndsm"][r, c])
        rows.append({"x": x0 + (c[k] + 0.5) * RES, "y": ytop - (r[k] + 0.5) * RES, "roof_seed": float(roof),
                     **describe(r, c, g)})
    columns = ("x", "y", "roof_seed") + FEATURES
    return {k: np.array([row[k] for row in rows], dtype=float) for k in columns}


def describe(r, c, g):
    """The FEATURES of one object, from its pixel rows and columns and the grids ``g``."""
    gap = g["ground_gap"][r, c]
    n_pulses = g["pulses"][r, c].sum()
    return {"area": len(r) * RES ** 2,
            "height": np.median(g["ndsm"][r, c]),
            "hole_depth": gap.max(),
            "hole_share": np.mean(gap > HOLE_GAP),
            "rectangularity": rectangularity(r, c),
            "multi_return": g["split"][r, c].sum() / n_pulses if n_pulses else np.nan,
            "roughness": np.nanmedian(g["roughness"][r, c])}


def segment_map(segments, n_roofs):
    """1 for roof-seeded objects, 2 for tree crowns, 0 elsewhere and on the edges between objects."""
    kind = np.where(segments == 0, 0, np.where(segments <= n_roofs, 1, 2)).astype(np.uint8)
    edge = np.zeros(segments.shape, bool)
    edge[:, :-1] |= segments[:, :-1] != segments[:, 1:]
    edge[:-1] |= segments[:-1] != segments[1:]
    kind[edge] = 0
    return kind


def pixels(labels):
    """(label, rows, columns) of each object in a label image."""
    for i, box in enumerate(ndimage.find_objects(labels), 1):
        if box is not None:
            r, c = np.nonzero(labels[box] == i)
            yield i, r + box[0].start, c + box[1].start


def rectangularity(r, c):
    """Number of pixels / area of the smallest rotated rectangle around them: 1 for a rectangle,
    lower for ragged shapes. That rectangle always has a side along an edge of the shape's
    convex hull, so each hull edge is tried as the rectangle's direction."""
    corners = np.concatenate([np.c_[c + dc, r + dr] for dc in (0, 1) for dr in (0, 1)]).astype(float)
    hull = corners[ConvexHull(corners).vertices]
    edges = np.roll(hull, -1, axis=0) - hull
    along = edges / np.hypot(edges[:, :1], edges[:, 1:])  # unit vector along each hull edge
    across = np.c_[-along[:, 1], along[:, 0]]
    sizes = np.ptp(hull @ along.T, axis=0) * np.ptp(hull @ across.T, axis=0)
    return len(r) / sizes.min()


def mean_per_pixel(pixel, z, ny, nx):
    """Mean z of the points in each pixel; NaN where there are none."""
    count = np.bincount(pixel, minlength=ny * nx)
    total = np.bincount(pixel, weights=z, minlength=ny * nx)
    with np.errstate(invalid="ignore"):  # 0 / 0 -> NaN for empty pixels
        return (total / count).reshape(ny, nx).astype(np.float32)


def highest_per_pixel(pixel, z, ny, nx):
    """Highest z of the points in each pixel; NaN where there are none."""
    top = np.full(ny * nx, -np.inf)
    np.maximum.at(top, pixel, z)
    top[np.isneginf(top)] = np.nan
    return top.reshape(ny, nx).astype(np.float32)


def fill_from_nearest(grid):
    """Fill the NaN pixels of ``grid`` with the value of the nearest pixel that has one.
    Also returns each pixel's distance (m) to that pixel, 0 where the grid had a value."""
    empty = np.isnan(grid)
    if empty.all():  # no points in the whole window: nothing to fill from
        return grid, np.full(grid.shape, np.nan, np.float32)
    # for every empty pixel: distance to, and row/column of, the nearest non-empty pixel
    dist, (r, c) = ndimage.distance_transform_edt(empty, sampling=RES, return_indices=True)
    return grid[r, c], dist.astype(np.float32)
