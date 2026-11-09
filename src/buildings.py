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
              building also hides the ground, and must not end up in the roof. Such a crown
              can make the hole fail as a whole; then only its roof-like pieces of at least
              ROOF_PIECE seed it.
  tree tops   local maxima of the lightly smoothed ndsm, away from the roof seeds
Each high pixel joins the seed it is "downhill" from, so boundaries fall in the valleys
between crowns and roofs. Then tree pixels that are really roof go to the nearest roof: whole
"tree" objects next to a roof with few split pulses (roof sections where a tree top landed),
thin pieces next to a roof (eaves, ridges) and tree patches with roof all around them (rooftop
units). Every tree object stays in one piece.

Table "objects": one row per object of at least MIN_AREA. Its point (x, y) is inside the
object (a roof's pixel furthest from any ground point, a tree's top); the window holding that
point keeps the row. Columns:
  roof_seed (1 or 0), area (m^2), height (median ndsm), hole_depth (largest ground_gap),
  hole_share (share of pixels further than HOLE_GAP from any ground point), rectangularity
  (1 for a rectangle, lower for ragged shapes), multi_return (share of split pulses),
  roughness (median), roughness_inside (median without the object's edge pixels: at the edge
  the Laplacian sees the drop to the ground, and on a small object half the pixels are edge),
  building (the rule's verdict, CODES: 0 = not a building, 1 = building, 2 = shed, container
  or RV, a building under SMALL_STRUCTURE), and with POINT_METRICS the metrics from the
  object's points: n_points, top, h_mean, h_std, h_p50, h_p95, cover, i_mean, i_std (see
  point_metrics), and cover_min (the lowest cover of one flight line: about 0 for a vehicle
  that came or went between two passes; see cover_min)

Layer "footprints" (GeoPackage): the roof-seeded objects as polygons, with the same columns
(without roof_seed) and two more:
  outline  how the polygon was made (CODES). A building gets straight walls (straighten): one
           main direction per building, walls within SNAP_ANGLE of it (or square to it) turned
           onto it and placed through the middle of their pixel staircase; diagonal walls of at
           least MIN_DIAGONAL stay. The safety net keeps the exact pixel outline for buildings
           under STRAIGHT_MIN_AREA, and the simplified one (pixel staircases simplified away by
           SIMPLIFY) where the walls don't agree on a direction (ragged, tree-bitten), the result
           is invalid or its area changed too much. Objects that aren't buildings are simplified
           (not under SIMPLIFY_MIN_AREA).
  review   1 for a tree notch (tree_notch): a diagonal wall with tree in front of it, where a
           tree hangs over the roof and hides part of it.
Holes under HOLE_FILL are filled. A footprint is the roof seen from above: eaves included, parts
under overhanging trees missing. The meanings of the codes are stored in the file (coded field
domains), so QGIS shows the labels.

Layer "crowns" (GeoPackage): the objects that aren't buildings (building = 0) of at least
CROWN_MIN_AREA, as polygons in the same way, with the same columns (without building). Mostly
tree crowns. roof_seed = 1 marks a roof-seeded object the rule rejected: it is in footprints
too. Neighbouring crowns are simplified one by one, so their shared edges can differ a little.

Needs the fields X, Y, Z, Classification, ReturnNumber and NumberOfReturns, and Intensity and
PointSourceId for POINT_METRICS (tilestream reads all fields unless --dims says otherwise).

    python src/tilestream.py run out/ --ept WA_NorthEast_2_B22 --bbox XMIN YMIN XMAX YMAX \\
        --analysis src/buildings.py
    python src/tilestream.py merge out/
"""
import numpy as np
from scipy import ndimage
from scipy.spatial import ConvexHull
from skimage import measure
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
ROOF_PIECE = 20         # m^2: a hole that fails that as a whole (it runs on under trees) is seeded from its
                        # roof-like pieces of at least this (Chewelah: 8 of 2,297 such holes, 5 of them roofs)
TREE_SMOOTH = 0.5       # m, Gaussian smoothing of the ndsm before finding tree tops
TREE_TOP_RADIUS = 2.5   # m, a tree top is the highest point within this distance; 1.5 m split
                        # big crowns into 2-3 pieces
THIN = 1.0              # m, disk radius: tree pieces narrower than ~2.5 m that touch a roof are roof
                        # (eaves); with 0.5 m, half the eave strips stayed trees
SIMPLIFY = 0.5          # m: outline corners within this of a straight line are dropped (one pixel)
SIMPLIFY_MIN_AREA = 25  # m^2: smaller objects keep their exact pixel outline
HOLE_FILL = 1.0         # m^2: smaller holes in an outline are filled: single low pixels in a roof (in Chewelah
                        # 109 of 120 holes; a skylight, a wet spot)
WALL_TOL = 0.75         # m (straighten): an outline point further than this from a straight line starts a new wall
SNAP_ANGLE = 15         # degrees: a wall within this of the main direction (or square to it) is turned onto it
MIN_DIAGONAL = 4.0      # m: a shorter wall that doesn't snap is a ragged bit or a corner the pixels cut off, and
                        # is dropped (Chewelah: 251 unsnapped walls of 2.5-4 m, 10 of 6-10 m, none longer)
MAX_SHIFT = 1.5         # m: a diagonal wall that would meet its neighbour further than this from their old
                        # corner gets a jog instead
STRAIGHT_MIN_AREA = 25  # m^2: a smaller ring (building, part or hole) keeps its pixel outline: walls too short
STRAIGHT_AGREE = 0.7    # the walls must agree at least this well on one direction (tree-bitten outlines don't)
STRAIGHT_MAX_CHANGE = 0.1  # the straightened area may differ at most this share from the pixel area
BUILDING_MULTI = 0.35   # the rule (is_building): over the whole object, a share of split pulses below this,
BUILDING_ROUGH = 0.5    # a median roughness_inside below this (m; as ROOF_ROUGH: a rock is a dome),
BUILDING_SMALL = 25     # m^2: smaller objects need a roughness_inside below BUILDING_ROUGH_SMALL:
BUILDING_ROUGH_SMALL = 0.3  # a shed's simple roof is smoother (0.05-0.15) than a bush (0.36-0.38),
BUILDING_AREA = 4.0     # and an area of at least this (m^2; a 2 x 3 m shed is 6 m^2)
SMALL_STRUCTURE = 40    # m^2: a smaller building is a shed, container or RV (40 ft container 30 m^2, big RV 35)
CODES = {"footprints": {"building": {0: "not a building", 1: "building", 2: "shed, container or RV"},
                        "outline": {0: "straightened", 1: "not straightened: too small",
                                    2: "not straightened: walls don't agree on a direction",
                                    3: "not straightened: the result was not a valid shape",
                                    4: "not straightened: the area changed too much",
                                    5: "not straightened: not a building"},
                        "review": {0: "no", 1: "tree notch: a tree may hide part of the roof"}}}
POINT_METRICS = True    # per-object metrics from the points (heights, cover, intensity; see
                        # point_metrics, and cover_min). Needs the Intensity and PointSourceId fields.
COVER_LINE_SHARE = 0.5  # cover_min: a flight line counts if it reaches at least this share of the object's
                        # pixels (one that grazes the object sees only its edge)
CROWN_MIN_AREA = 5.0    # m^2: smaller tree objects are mostly loose bits just over MIN_HEIGHT (shrubs,
                        # snags): in Nine Mile Falls 44% of the tree objects, but 1% of their area
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
    t = out["objects"]
    if POINT_METRICS:
        h = pts["Z"][keep] - dtm.ravel()[pixel[keep]]  # height above the ground
        label = segments.ravel()[pixel[keep]]
        t.update(point_metrics(h, first[keep], pts["Intensity"][keep], label, labels))
        t["cover_min"] = cover_min(h, first[keep], label, pixel[keep], pts["PointSourceId"][keep], labels)
    t["building"] = building_code(t)
    # two polygon layers: footprints = the roof-seeded rows, crowns = the rows that aren't buildings.
    # Most roof-seeded rejects are crowns too, so they are in both until we trust the rule.
    roof = t["roof_seed"] == 1
    crown = (t["building"] == 0) & (t["area"] >= CROWN_MIN_AREA)
    traced = trace(np.where(np.isin(segments, labels[roof | crown]), segments, 0), x0, ytop)
    shapes = {i: simplify(g) for i, g in traced.items()}
    # buildings get straight walls where the safety net allows, and a review flag for tree notches
    outline, review = np.full(len(t["x"]), 5), np.zeros(len(t["x"]), int)
    for j in np.flatnonzero(t["building"] > 0):
        i = labels[j]
        geom, _, outline[j] = straighten(traced[i])
        if geom is not None:
            geom.Normalize()
            shapes[i] = geom
        review[j] = tree_notch(traced[i], i, segments, n_roofs, x0, ytop) > 0

    def layer(rows, drop, **extra):  # the table's rows, without a column that is the same in every row
        return {**{k: v[rows] for k, v in t.items() if k != drop}, **{k: v[rows] for k, v in extra.items()},
                "geometry": np.array([shapes[i].ExportToWkt() for i in labels[rows]])}
    out["footprints"] = layer(roof, "roof_seed", outline=outline, review=review)
    out["crowns"] = layer(crown, "building")
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
        sizes = np.bincount(pieces[r, c]) * RES ** 2
        sizes[0] = 0  # pixels that aren't roof-like
        size = sizes[pieces[r, c]]  # each pixel's piece size
        if f["area"] >= MIN_AREA and f["multi_return"] < ROOF_MULTI and f["roughness"] < ROOF_ROUGH:
            # seed only the hole's roof-like pieces (of at least MIN_AREA, not stray pixels in a
            # crown): a dense crown next to a building also hides the ground, so the hole often
            # runs on under the tree. All pieces, not just the largest: steps between roof
            # sections split pulses too, and cut the roof-like part apart.
            keep = size >= MIN_AREA
            if not keep.any():  # a shed is nearly all edge, which splits pulses and is rough:
                keep[:] = True  # its roof-like pixels are stray ones, so seed the whole hole
        else:
            # where the hole runs on under trees, the trees can make it fail as a whole: then
            # only big roof-like pieces seed it (a dense crown's smooth bits are smaller)
            keep = size >= ROOF_PIECE
            if not keep.any():
                continue
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
    return reclaim_roof(segments, n_roofs, g), n_roofs


def reclaim_roof(segments, n_roofs, g):
    """Give the tree pixels that are really roof to the nearest roof:
      - whole tree objects next to a roof with, as a whole, few split pulses (below ROOF_MULTI):
        roof sections and roof edges where a tree top landed, so the flood made them a "tree".
      - thin pieces next to a roof. Eaves, ridges and steps between roof sections look like trees
        (split pulses, sharp bends), and a crown can flood along them deep into a roof. An erosion
        followed by a dilation (an opening) with a disk of radius THIN keeps only the tree pixels
        the disk fits in, so it finds every piece narrower than ~2.5 m.
      - tree patches with roof all around them: rooftop units and raised parts of a building are
        too wide for the opening, but a real tree reaches past the roof edge somewhere, down to
        lower ground.
    Taking the thin pieces can cut a tree object in parts. The part with its top keeps the
    label; another part goes to the roof if it touches one, or else becomes an object of its own."""
    if n_roofs == 0:
        return segments
    trees = segments > n_roofs
    roof = (segments > 0) & ~trees
    # the row and column of each pixel's nearest roof pixel
    _, (r, c) = ndimage.distance_transform_edt(~roof, return_indices=True)
    size = segments.max() + 1
    with np.errstate(invalid="ignore", divide="ignore"):  # no pulses: NaN, not roof-like
        roof_like = (np.bincount(segments.ravel(), g["split"].ravel(), size)
                     / np.bincount(segments.ravel(), g["pulses"].ravel(), size)) < ROOF_MULTI
    touching = np.zeros(size, bool)
    touching[segments[trees & ndimage.binary_dilation(roof)]] = True
    take = trees & (roof_like & touching)[segments]  # whole roof-like objects next to a roof
    roof |= take
    rest = trees & ~take
    k = round(THIN / RES)
    disk = np.hypot(*np.mgrid[-k:k + 1, -k:k + 1]) <= k
    thin = rest & ~ndimage.binary_opening(rest, disk)
    pieces, _ = ndimage.label(thin)
    take |= np.isin(pieces, pieces[thin & ndimage.binary_dilation(roof)])  # the pieces touching a roof
    rest = trees & ~take
    patches, _ = ndimage.label(rest)
    reach_ground = np.isin(patches, patches[rest & ndimage.binary_dilation(segments == 0)])
    take |= rest & ~reach_ground  # patches that touch no lower ground: roof all around
    segments[take] = segments[r[take], c[take]]

    # each tree object in one piece again: the part with its top (highest pixel) keeps the label
    trees = segments > n_roofs
    parts = measure.label(np.where(trees, segments, 0), connectivity=1)  # connected parts of one label
    tops = ndimage.maximum_position(g["ndsm"], segments, np.unique(segments[trees]))
    main = np.zeros(parts.max() + 1, bool)
    main[[parts[p] for p in tops]] = True
    cut = trees & ~main[parts]
    # a cut-off part next to a roof (the roof before the thin pieces: not just the piece that was cut)
    to_roof = cut & np.isin(parts, parts[cut & ndimage.binary_dilation(roof)])
    segments[to_roof] = segments[r[to_roof], c[to_roof]]
    alone = cut & ~to_roof
    segments[alone] = segments.max() + parts[alone]  # new labels, past all the others
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
    (Judging objects without a roof seed too was tried on 2026-11-03: in Chewelah it turned
    many trees into buildings.)
    NaN (no pulses, no inside) fails the test."""
    rough_max = np.where(t["area"] < BUILDING_SMALL, BUILDING_ROUGH_SMALL, BUILDING_ROUGH)
    return ((t["roof_seed"] == 1) & (t["multi_return"] < BUILDING_MULTI)
            & (t["roughness_inside"] < rough_max) & (t["area"] >= BUILDING_AREA))


def point_metrics(h, first, intensity, label, rows):
    """POINT_METRICS of the objects ``rows`` (their labels), from the points in their pixels:
    ``h`` is each point's height above the ground, ``first`` whether it is a first return,
    ``label`` the object its pixel belongs to (0 = none).
      n_points                          all returns
      top, h_mean, h_std, h_p50, h_p95  heights of the returns above MIN_HEIGHT: the crown or the
                                        roof, not what lies under it
      cover                             share of the first returns that are above MIN_HEIGHT
      i_mean, i_std                     intensity of the first returns above MIN_HEIGHT
    The points can arrive in another order in each window. So the heights are sorted per object
    before they are summed, and intensities are whole numbers, whose sums are exact in any order."""
    size = max(label.max(initial=0), rows.max(initial=0)) + 1
    up = h > MIN_HEIGHT

    def per_object(m, weights=None):  # count (or sum of weights) per label, of the points in m
        return np.bincount(label[m], None if weights is None else weights[m], minlength=size)[rows]

    i = intensity.astype(np.float64)
    with np.errstate(invalid="ignore", divide="ignore"):  # objects without such points get NaN
        n_i = per_object(first & up)
        i_mean = per_object(first & up, i) / n_i
        out = {"n_points": per_object(np.ones(len(h), bool)).astype(np.int64),
               "cover": n_i / per_object(first),
               "i_mean": i_mean,
               "i_std": np.sqrt(np.maximum(per_object(first & up, i * i) / n_i - i_mean ** 2, 0))}
    order = np.lexsort((h[up], label[up]))  # by object, then by height
    hs, ls = h[up][order], label[up][order]
    heights = {k: np.full(len(rows), np.nan) for k in ("top", "h_mean", "h_std", "h_p50", "h_p95")}
    for j, (a, b) in enumerate(zip(np.searchsorted(ls, rows), np.searchsorted(ls, rows, side="right"))):
        if b > a:
            v = hs[a:b]
            heights["top"][j], heights["h_mean"][j], heights["h_std"][j] = v[-1], v.mean(), v.std()
            heights["h_p50"][j], heights["h_p95"][j] = np.percentile(v, [50, 95])
    return {"n_points": out.pop("n_points"), **heights, **out}


def cover_min(h, first, label, pixel, source, rows):
    """The vehicle clue: the lowest cover (see point_metrics) of any one flight line over each
    object ``rows`` (their labels). A vehicle that came or went between two passes is there in
    one line, and in another its first returns hit the ground (cover ~0). A building is there in
    every line (Chewelah 2022: at least 0.44 in each line of every footprint). Only lines that
    reach at least COVER_LINE_SHARE of the object's pixels (those with a first return of any line)
    count. ``source`` is each point's flight line (PointSourceId), ``pixel`` its pixel; NaN where
    no line counts. Counts and a minimum: the same in any order of the points."""
    m = first & (label > 0)
    lab, pix, up = label[m].astype(np.int64), pixel[m], h[m] > MIN_HEIGHT
    lines, line = np.unique(source[m], return_inverse=True)
    key = lab * len(lines) + line  # one key per object and line
    k = np.arange(key.max(initial=-1) + 1)
    obj = k // max(len(lines), 1)  # each key's object
    with np.errstate(invalid="ignore"):  # keys without returns: 0 / 0, and they don't count
        cover = np.bincount(key, up, len(k)) / np.bincount(key, minlength=len(k))
    # the pixels each line reaches, and each object's pixels that any line reaches
    cells = pixel.max(initial=0) + 1
    size = max(label.max(initial=0), rows.max(initial=0)) + 1
    reached = np.bincount(np.unique(key * cells + pix) // cells, minlength=len(k))
    seen = np.bincount(np.unique(lab * cells + pix) // cells, minlength=size)
    ok = (reached > 0) & (reached >= COVER_LINE_SHARE * seen[obj])
    low = np.full(size, np.inf)
    np.minimum.at(low, obj[ok], cover[ok])
    return np.where(np.isinf(low), np.nan, low)[rows]


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


def trace(labels, x0, ytop):
    """{label: ogr geometry}: the outline of each object in a label image (0 = no object) along its
    pixel edges (gdal.Polygonize), in map coordinates, with the holes under HOLE_FILL filled."""
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
        shapes[i] = fill_small_holes(geom)
        shapes[i].Normalize()  # a fixed start corner, so the simplified outline is the same in every window
    return shapes


def simplify(geom):
    """The outline with pixel staircases turned into straight edges (simplifying by SIMPLIFY), if
    the object is at least SIMPLIFY_MIN_AREA. Simplifying keeps the outer corners of a staircase,
    so the area grows a little: on a small object a lot (+15% for a 15 m^2 diagonal shape)."""
    return geom.SimplifyPreserveTopology(SIMPLIFY) if geom.GetArea() >= SIMPLIFY_MIN_AREA else geom


def straighten(geom):
    """A building's pixel outline (ogr Polygon or MultiPolygon) with straight walls: (geometry or
    None, agree, code). Each ring is cut into walls (wall_pieces); one main direction for the whole
    building (main_direction_of); a wall within SNAP_ANGLE of it (or square to it) is turned onto
    it, the others keep their own direction (straighten_ring). Rings under STRAIGHT_MIN_AREA (small
    holes and parts) keep their pixel outline. The safety net's codes: 0 straightened, 1 too small
    (under STRAIGHT_MIN_AREA), 2 the walls don't agree on a direction (agree under STRAIGHT_AGREE:
    ragged, often bitten by trees), 3 the result isn't a valid shape, 4 its area differs more than
    STRAIGHT_MAX_CHANGE from the pixel area."""
    polys = polygon_rings(geom)
    pieces = [[wall_pieces(r) for r in rings] for rings in polys]
    main, agree = main_direction_of([w for rings in pieces for ring in rings for w in ring])
    if geom.GetArea() < STRAIGHT_MIN_AREA:
        return None, agree, 1
    if agree < STRAIGHT_AGREE:
        return None, agree, 2
    new = [[straighten_ring(w, main) if ring_area(r) >= STRAIGHT_MIN_AREA else r for r, w in zip(rings, ws)]
           for rings, ws in zip(polys, pieces)]
    out = from_rings(new) if all(len(r) >= 5 for rings in new for r in rings) else None  # 4 corners, closed
    if out is None or not out.IsValid():
        return None, agree, 3
    if abs(out.GetArea() / geom.GetArea() - 1) > STRAIGHT_MAX_CHANGE:
        return None, agree, 4
    return out, agree, 0


def straighten_ring(pieces, main):
    """One ring's walls (wall_pieces), straightened along the main direction: the closed ring of
    its new corners, (n, 2). Short walls that don't snap are dropped, and neighbouring parallel
    walls closer than WALL_TOL become one. Then neighbouring walls meet where their lines cross:
    square walls always, a diagonal only within MAX_SHIFT of where they met in the pixel outline.
    Parallel walls (and a diagonal too far off) get a jog between them."""
    # a wall: its pieces of the outline (more than one after a merge), and its line
    walls = [([w], fit_wall([w], main)) for w in pieces]
    walls = [(w, f) for w, f in walls if f[2] or np.hypot(*(w[-1][-1] - w[0][0])) >= MIN_DIAGONAL]
    k = 0
    while k < len(walls) and len(walls) > 3:  # k = 0: the last wall and the first
        (w1, (t1, c1, _)), (w2, (t2, c2, _)) = walls[k - 1], walls[k]
        if parallel(t1, t2) and abs(c1 - normal(t1) @ normal(t2) * c2) < WALL_TOL:
            walls[k - 1] = (w1 + w2, fit_wall(w1 + w2, main))
            del walls[k]
        else:
            k += 1
    if len(walls) < 3:
        return np.empty((0, 2))
    corners = []
    for (w1, (t1, c1, snap1)), (w2, (t2, c2, snap2)) in zip(walls, walls[1:] + walls[:1]):
        n1, n2 = normal(t1), normal(t2)
        p = (w1[-1][-1] + w2[0][0]) / 2  # where they met in the pixel outline (the middle of a dropped bit)
        if not parallel(t1, t2):
            x = np.linalg.solve(np.array([n1, n2]), [c1, c2])
            if (snap1 and snap2) or np.hypot(*(x - p)) <= MAX_SHIFT:
                corners.append(x)
                continue
        corners += [p - (n1 @ p - c1) * n1, p - (n2 @ p - c2) * n2]  # a jog: p's foot on each wall
    return np.array(corners + corners[:1])


def wall_pieces(ring):
    """A pixel outline ring (closed (n, 2) array) cut into walls: the pieces of outline between the
    corners Douglas-Peucker finds (wall_ends), starting at a real corner."""
    ring = start_at_corner(ring)
    ends = wall_ends(ring)
    return [ring[a:b + 1] for a, b in zip(ends[:-1], ends[1:])]


def start_at_corner(ring):
    """The closed ring, starting at its point furthest from its centre: almost always a real
    corner. Douglas-Peucker always keeps its start, which would cut a wall in two."""
    pts = ring[:-1]
    pts = np.roll(pts, -np.argmax(np.hypot(*(pts - pts.mean(axis=0)).T)), axis=0)
    return np.vstack([pts, pts[:1]])


def wall_ends(ring, tol=WALL_TOL):
    """Douglas-Peucker on a closed ring: the indices of the points where one wall ends and the next
    begins. Between two kept points, the point furthest from the line through them is kept if it is
    more than tol away, and both halves are done again."""
    keep = np.zeros(len(ring), bool)
    keep[[0, -1]] = True
    todo = [(0, len(ring) - 1)]
    while todo:
        a, b = todo.pop()
        if b - a < 2:
            continue
        p, x, chord = ring[a], ring[a + 1:b], ring[b] - ring[a]
        if np.hypot(*chord) > 0:  # distance to the line through p and ring[b]
            d = np.abs(chord[0] * (x[:, 1] - p[1]) - chord[1] * (x[:, 0] - p[0])) / np.hypot(*chord)
        else:  # the whole ring (it ends where it starts): distance to p
            d = np.hypot(*(x - p).T)
        k = np.argmax(d)
        if d[k] > tol:
            keep[a + 1 + k] = True
            todo += [(a, a + 1 + k), (a + 1 + k, b)]
    return np.nonzero(keep)[0]


def main_direction_of(pieces):
    """main_direction of these walls in two passes: the second only lets the walls within
    SNAP_ANGLE of the first answer vote, so diagonal walls (mostly tree edges) don't turn the
    building. The agree value is the first pass's."""
    angle, _, length = map(np.array, zip(*[wall_line([w]) for w in pieces]))
    main, agree = main_direction(angle, length)
    near = np.abs((angle - main + 45) % 90 - 45) <= SNAP_ANGLE
    if near.any():
        main = main_direction(angle[near], length[near])[0]
    return main, agree


def main_direction(angles, lengths):
    """The direction of walls (degrees counter-clockwise from east, mod 90), and how well they agree
    on it (1 = every wall at 0 or 90 degrees to it). Each wall's angle times 4: then walls at right
    angles point the same way (4 x 90 = 360), and their mean (as arrows) is the direction, times 4.
    Weighted by length squared, so a long wall counts far more than a short ragged bit."""
    w = lengths ** 2
    z = np.sum(w * np.exp(4j * np.radians(angles)))
    return np.degrees(np.angle(z) / 4) % 90, abs(z) / w.sum()


def wall_line(pieces):
    """The best straight line through a wall's pieces of pixel outline: its angle (degrees, mod 180),
    a point on it and the wall's length of pixel edge. Fitted to the pixel edges as line pieces (the
    spread of their midpoints, weighted by length, plus each edge's own spread along itself), so a
    staircase gives the line through its middle, not its corners, and a single edge its own direction."""
    edge = np.concatenate([np.diff(w, axis=0) for w in pieces])
    mid = np.concatenate([(w[1:] + w[:-1]) / 2 for w in pieces])
    length = np.hypot(*edge.T)
    centre = length @ mid / length.sum()
    dx, dy = (mid - centre).T
    ex, ey = edge.T
    sxx = length @ (dx * dx + ex * ex / 12)
    syy = length @ (dy * dy + ey * ey / 12)
    sxy = length @ (dx * dy + ex * ey / 12)
    return np.degrees(np.arctan2(2 * sxy, sxx - syy) / 2), centre, length.sum()


def fit_wall(pieces, main):
    """A wall's line: (angle in degrees, offset along its normal, snapped). Turned onto the main
    direction (or square to it) when within SNAP_ANGLE of it."""
    angle, centre, _ = wall_line(pieces)
    off = (angle - main + 45) % 90 - 45  # from the main direction or square to it, -45..45
    snap = abs(off) <= SNAP_ANGLE
    if snap:
        angle -= off
    return angle, normal(angle) @ centre, snap


def normal(angle):
    t = np.radians(angle)
    return np.array([-np.sin(t), np.cos(t)])


def parallel(a1, a2):
    return abs((a1 - a2 + 90) % 180 - 90) < 1e-6


def tree_notch(geom, label, segments, n_roofs, x0, ytop):
    """Length (m) of the diagonal walls (not snapped, at least MIN_DIAGONAL) in the outline of the
    object ``label`` with mostly tree pixels in front of them: there a tree hangs over the roof and
    hides its corner (the review flag). In front of a real wall is ground or another building.
    Each pixel edge of a wall is looked at from its middle, half a pixel to either side."""
    ny, nx = segments.shape

    def label_at(xy):  # 0 outside the grid
        r, c = ((ytop - xy[:, 1]) / RES).astype(int), ((xy[:, 0] - x0) / RES).astype(int)
        ok = (r >= 0) & (r < ny) & (c >= 0) & (c < nx)
        return np.where(ok, segments[np.clip(r, 0, ny - 1), np.clip(c, 0, nx - 1)], 0)

    pieces = [w for rings in polygon_rings(geom) for r in rings for w in wall_pieces(r)]
    main = main_direction_of(pieces)[0]
    total = 0.0
    for w in pieces:
        length = np.hypot(*(w[-1] - w[0]))
        if fit_wall([w], main)[2] or length < MIN_DIAGONAL:
            continue
        p, d = w[:-1], np.diff(w, axis=0)
        n = np.maximum(np.rint(np.hypot(*d.T) / RES).astype(int), 1)  # pixel edges per outline edge
        e = np.repeat(np.arange(len(p)), n)
        mid = p[e] + ((np.arange(len(e)) - np.repeat(np.cumsum(n) - n, n) + 0.5) / n[e])[:, None] * d[e]
        side = np.c_[-d[e, 1], d[e, 0]] / np.hypot(*d[e].T)[:, None] * RES / 2
        a, b = label_at(mid + side), label_at(mid - side)
        if np.mean(np.where(a == label, b, a) > n_roofs) > 0.5:
            total += length
    return total


def fill_small_holes(geom):
    """The geometry without its holes smaller than HOLE_FILL."""
    return from_rings([[r for k, r in enumerate(rings) if k == 0 or ring_area(r) >= HOLE_FILL]
                       for rings in polygon_rings(geom)])


def polygon_rings(geom):
    """The polygons of an ogr Polygon or MultiPolygon, each as its rings (closed (n, 2) arrays, the
    outer ring first)."""
    from osgeo import ogr
    parts = [geom] if geom.GetGeometryType() == ogr.wkbPolygon else [geom.GetGeometryRef(k) for k in range(geom.GetGeometryCount())]
    return [[np.array(p.GetGeometryRef(k).GetPoints())[:, :2] for k in range(p.GetGeometryCount())] for p in parts]


def from_rings(polys):
    """Polygons, each as its rings (polygon_rings), as one ogr Polygon or a MultiPolygon."""
    from osgeo import ogr
    out = []
    for rings in polys:
        p = ogr.Geometry(ogr.wkbPolygon)
        for r in rings:
            ring = ogr.Geometry(ogr.wkbLinearRing)
            for x, y in r:
                ring.AddPoint_2D(float(x), float(y))
            p.AddGeometry(ring)
        out.append(p)
    if len(out) == 1:
        return out[0]
    multi = ogr.Geometry(ogr.wkbMultiPolygon)
    for p in out:
        multi.AddGeometry(p)
    return multi


def ring_area(r):
    """Area of a closed ring (shoelace formula)."""
    return abs(np.sum(r[:-1, 0] * r[1:, 1] - r[1:, 0] * r[:-1, 1])) / 2


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
