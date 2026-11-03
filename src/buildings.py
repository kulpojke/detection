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
              don't cut a building into pieces. The seed is only the hole's pixels that look
              like roof themselves (in pieces of at least MIN_AREA): a dense crown next to a
              building also hides the ground, and must not end up in the roof.
  tree tops   local maxima of the lightly smoothed ndsm, away from the roof seeds
Each high pixel joins the seed it is "downhill" from, so boundaries fall in the valleys
between crowns and roofs. Then tree pixels that are really roof go to the nearest roof: thin
pieces next to a roof (eaves, ridges) and tree patches with roof all around them (rooftop units).

Table "objects": one row per object of at least MIN_AREA. Its point (x, y) is inside the
object (a roof's pixel furthest from any ground point, a tree's top); the window holding that
point keeps the row. Columns:
  roof_seed (1 or 0), area (m^2), height (median ndsm), hole_depth (largest ground_gap),
  hole_share (share of pixels further than HOLE_GAP from any ground point), rectangularity
  (1 for a rectangle, lower for ragged shapes), multi_return (share of split pulses),
  roughness (median), roughness_inside (median without the object's edge pixels: at the edge
  the Laplacian sees the drop to the ground, and on a small object half the pixels are edge),
  building (the rule's verdict, CODES: 0 = not a building, 1 = building, 2 = shed, container
  or RV, a building under SMALL_STRUCTURE)

Layer "footprints" (GeoPackage): the roof-seeded objects as polygons, with the same columns
(without roof_seed). The outline follows the object's pixel edges, with pixel staircases
simplified away (SIMPLIFY; not for objects under SIMPLIFY_MIN_AREA). A footprint is the roof
seen from above: eaves included, parts under overhanging trees missing. The meaning of the
building codes is stored in the file (a coded field domain), so QGIS shows the labels.

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
THIN = 1.0              # m, disk radius: tree pieces narrower than ~2.5 m that touch a roof are roof
                        # (eaves); with 0.5 m, half the eave strips stayed trees
SIMPLIFY = 0.5          # m: outline corners within this of a straight line are dropped (one pixel)
SIMPLIFY_MIN_AREA = 25  # m^2: smaller objects keep their exact pixel outline
BUILDING_MULTI = 0.35   # the rule (is_building): over the whole object, a share of split pulses below this,
BUILDING_ROUGH = 0.5    # a median roughness_inside below this (m; as ROOF_ROUGH: a rock is a dome),
BUILDING_SMALL = 25     # m^2: smaller objects need a roughness_inside below BUILDING_ROUGH_SMALL:
BUILDING_ROUGH_SMALL = 0.3  # a shed's simple roof is smoother (0.05-0.15) than a bush (0.36-0.38),
BUILDING_AREA = 4.0     # and an area of at least this (m^2; a 2 x 3 m shed is 6 m^2)
SMALL_STRUCTURE = 40    # m^2: a smaller building is a shed, container or RV (40 ft container 30 m^2, big RV 35)
CODES = {"footprints": {"building": {0: "not a building", 1: "building", 2: "shed, container or RV"}}}
GROUND = 2
NOISE = (7, 18)         # low and high noise
FEATURES = ("area", "height", "hole_depth", "hole_share", "rectangularity", "multi_return", "roughness",
            "roughness_inside")


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
    out["objects"], labels = objects_table(segments, n_roofs, grids, x0, ytop)
    out["objects"]["building"] = building_code(out["objects"])
    # footprints: the roof-seeded rows again, with an outline
    roof = out["objects"]["roof_seed"] == 1
    shapes = outlines(np.where(segments <= n_roofs, segments, 0), x0, ytop)
    out["footprints"] = {**{k: v[roof] for k, v in out["objects"].items() if k != "roof_seed"},
                         "geometry": np.array([shapes[i] for i in labels[roof]])}
    return out


def segment(g):
    """Split everything higher than MIN_HEIGHT into objects (see the module docstring).
    Returns the label image (0 = no object) and n_roofs: labels 1..n_roofs grew from roof seeds."""
    high = g["ndsm"] > MIN_HEIGHT
    roofs = np.zeros(high.shape, np.int32)
    n_roofs = 0
    holes, _ = ndimage.label(high & (g["ground_gap"] > HOLE_GAP))
    # the pixels of the holes that look like roof themselves, in connected pieces
    pieces, _ = ndimage.label((holes > 0) & (g["multi_return"] < ROOF_MULTI) & (g["roughness"] < ROOF_ROUGH))
    for _, r, c in pixels(holes):
        f = describe(r, c, g)
        if f["area"] >= MIN_AREA and f["multi_return"] < ROOF_MULTI and f["roughness"] < ROOF_ROUGH:
            # seed only the hole's roof-like pieces (of at least MIN_AREA, not stray pixels in a
            # crown): a dense crown next to a building also hides the ground, so the hole often
            # runs on under the tree. All pieces, not just the largest: steps between roof
            # sections split pulses too, and cut the roof-like part apart.
            sizes = np.bincount(pieces[r, c]) * RES ** 2
            sizes[0] = 0  # pixels that aren't roof-like
            keep = sizes[pieces[r, c]] >= MIN_AREA
            if not keep.any():  # a shed is nearly all edge, which splits pulses and is rough:
                keep[:] = True  # its roof-like pixels are stray ones, so seed the whole hole
            n_roofs += 1
            roofs[r[keep], c[keep]] = n_roofs

    smooth = ndimage.gaussian_filter(np.where(high, g["ndsm"], 0), TREE_SMOOTH / RES)
    peak = smooth == ndimage.maximum_filter(smooth, 2 * round(TREE_TOP_RADIUS / RES) + 1)
    # 1 m around the (roof-like) seeds: eaves and parapets aren't tree tops, a crown over the roof is
    near_roof = ndimage.binary_dilation(roofs > 0, iterations=2)
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
    segments = watershed(np.where(high, -g["ndsm"], 0), markers, mask=high)
    return reclaim_roof(segments, n_roofs), n_roofs


def reclaim_roof(segments, n_roofs):
    """Give the tree pixels that are really roof to the nearest roof:
      - thin pieces next to a roof. Eaves, ridges and steps between roof sections look like trees
        (split pulses, sharp bends), and a crown can flood along them deep into a roof. An erosion
        followed by a dilation (an opening) with a disk of radius THIN keeps only the tree pixels
        the disk fits in, so it finds every piece narrower than ~2.5 m.
      - tree patches with roof all around them: rooftop units and raised parts of a building are
        too wide for the opening, but a real tree reaches past the roof edge somewhere, down to
        lower ground."""
    if n_roofs == 0:
        return segments
    trees = segments > n_roofs
    roof = (segments > 0) & ~trees
    k = round(THIN / RES)
    disk = np.hypot(*np.mgrid[-k:k + 1, -k:k + 1]) <= k
    thin = trees & ~ndimage.binary_opening(trees, disk)
    pieces, _ = ndimage.label(thin)
    take = np.isin(pieces, pieces[thin & ndimage.binary_dilation(roof)])  # the pieces touching a roof
    rest = trees & ~take
    patches, _ = ndimage.label(rest)
    reach_ground = np.isin(patches, patches[rest & ndimage.binary_dilation(segments == 0)])
    take |= rest & ~reach_ground  # patches that touch no lower ground: roof all around
    # the row and column of each pixel's nearest roof pixel
    _, (r, c) = ndimage.distance_transform_edt(~roof, return_indices=True)
    segments[take] = segments[r[take], c[take]]
    return segments


def objects_table(segments, n_roofs, g, x0, ytop):
    """The objects table (see the module docstring), as {column: array}, and each row's
    label in ``segments``."""
    rows, labels = [], []
    for i, r, c in pixels(segments):
        if len(r) * RES ** 2 < MIN_AREA:
            continue
        roof = i <= n_roofs
        # a point inside the object: a roof's pixel furthest from any ground point, a tree's top
        k = np.argmax(g["ground_gap"][r, c] if roof else g["ndsm"][r, c])
        rows.append({"x": x0 + (c[k] + 0.5) * RES, "y": ytop - (r[k] + 0.5) * RES, "roof_seed": float(roof),
                     **describe(r, c, g)})
        labels.append(i)
    columns = ("x", "y", "roof_seed") + FEATURES
    return {k: np.array([row[k] for row in rows], dtype=float) for k in columns}, np.array(labels, int)


def is_building(t):
    """The rule, version 1: which objects are buildings. ``t`` is the objects table (arrays, or
    one row). Only roof-seeded objects can be, and they are judged as a whole: a dense crown can
    hide the ground and look smooth in its middle, so it gets a roof seed, but over the whole
    crown its pulses split and it is rough. Roughness is measured inside the object: a shed's
    flat roof is smooth there, a rock (a dome) or a bush isn't. Small objects must be smoother:
    a shed has a simple roof, while big buildings have ridges, vents and units.
    NaN (no pulses, no inside) fails the test."""
    rough_max = np.where(t["area"] < BUILDING_SMALL, BUILDING_ROUGH_SMALL, BUILDING_ROUGH)
    return ((t["roof_seed"] == 1) & (t["multi_return"] < BUILDING_MULTI)
            & (t["roughness_inside"] < rough_max) & (t["area"] >= BUILDING_AREA))


def building_code(t):
    """The "building" column (CODES): 0 = not a building, 1 = building, 2 = a building smaller
    than SMALL_STRUCTURE: a shed, container or RV (or a parked truck: from above it is a box too)."""
    return np.where(is_building(t), np.where(t["area"] < SMALL_STRUCTURE, 2, 1), 0)


def describe(r, c, g):
    """The FEATURES of one object, from its pixel rows and columns and the grids ``g``."""
    gap = g["ground_gap"][r, c]
    n_pulses = g["pulses"][r, c].sum()
    # the pixels whose 4 neighbours are in the object too: the Laplacian there doesn't see the drop at the edge
    box = np.zeros((np.ptp(r) + 1, np.ptp(c) + 1), bool)
    box[r - r.min(), c - c.min()] = True
    inside = ndimage.binary_erosion(box)[r - r.min(), c - c.min()]
    return {"area": len(r) * RES ** 2,
            "height": np.median(g["ndsm"][r, c]),
            "hole_depth": gap.max(),
            "hole_share": np.mean(gap > HOLE_GAP),
            "rectangularity": rectangularity(r, c),
            "multi_return": g["split"][r, c].sum() / n_pulses if n_pulses else np.nan,
            "roughness": np.nanmedian(g["roughness"][r, c]),
            "roughness_inside": np.nanmedian(g["roughness"][r[inside], c[inside]]) if inside.any() else np.nan}


def outlines(labels, x0, ytop):
    """{label: WKT outline} of each object in a label image (0 = no object): gdal.Polygonize
    traces the pixel edges, then simplifying by SIMPLIFY turns pixel staircases into straight edges
    (only on objects of at least SIMPLIFY_MIN_AREA)."""
    from osgeo import gdal, ogr
    gdal.UseExceptions()
    ny, nx = labels.shape
    img = gdal.GetDriverByName("MEM").Create("", nx, ny, 1, gdal.GDT_Int32)
    img.SetGeoTransform((x0, RES, 0, ytop, 0, -RES))
    band = img.GetRasterBand(1)
    band.WriteArray(labels)
    vec = ogr.GetDriverByName("MEM").CreateDataSource("outlines")
    layer = vec.CreateLayer("outlines")
    layer.CreateField(ogr.FieldDefn("label", ogr.OFTInteger))
    gdal.Polygonize(band, band, layer, 0)  # the band is its own mask: label 0 isn't traced
    shapes = {}
    for f in layer:
        i, geom = f["label"], f.GetGeometryRef().Clone()
        # an object in pieces: a roof seed can be, e.g. two roof sections with a tree between them
        shapes[i] = shapes[i].Union(geom) if i in shapes else geom
    for i, geom in shapes.items():
        geom.Normalize()  # a fixed start corner, so the simplified outline is the same in every window
        # simplifying keeps the outer corners of a staircase, so the area grows a little: on a small
        # object a lot (+15% for a 15 m^2 diagonal shape), and corners got cut off past its point
        if geom.GetArea() >= SIMPLIFY_MIN_AREA:
            geom = geom.SimplifyPreserveTopology(SIMPLIFY)
        shapes[i] = geom.ExportToWkt()
    return shapes


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
    # from the object's own corner: the same numbers, so the same rounding, in every window
    r, c = r - r.min(), c - c.min()
    corners =np.concatenate([np.c_[c + dc, r + dr] for dc in (0, 1) for dr in (0, 1)]).astype(float)
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
