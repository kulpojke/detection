#!/usr/bin/env python3
"""Building detection for tilestream: height layers, roof-or-tree layers, and a table of
objects (buildings and tree crowns) with the features a building rule will use.

Rasters on a 0.5 m grid:
  dtm           ground height: mean of the ground points (class 2) in each pixel; pixels
                without ground points get the height of the nearest pixel that has them
  dsm           top height: highest point in each pixel (noise and bridge decks dropped). The
                scan pattern leaves ~2% of pixels empty by chance; those get the height of the
                nearest pixel with a point if it is within DSM_FILL. Bigger holes (water,
                dropouts) stay NaN.
  ndsm          height above ground, dsm - dtm
  ground_gap    distance (m) to the nearest pixel with a ground point: 0 where the ground was
                measured, growing toward the middle of a hole. A roof stops every pulse, so a
                building leaves a hole with straight edges; trees leave scattered ground points.
                Where ground_gap > ~1 m, the dtm is filled in, not measured.
  multi_return  share of laser pulses that gave more than one return, over each pixel's 3 x 3
                neighbourhood: near 0 on roofs (a hard surface stops the pulse), high in trees
  roughness     how sharply the dsm bends, |Laplacian| (m): ~0 on flat and evenly sloping
                roofs, high in tree crowns
  segments      the objects: 1 = grown from a roof seed, 2 = grown from a tree top, 3 = a
                building's roof found under a tree (roof_under_canopy), 0 = no object, or the
                edge between two objects (so each crown and roof stands out)

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
between crowns and roofs. A roof seed's small pieces flood on their own: one whose flood has
many split pulses (SEED_TREE_MULTI) was a smooth bit in a crown, and its flood is a tree
object (merge_parts). Then tree pixels that are really roof go to the nearest roof: whole
"tree" objects next to a roof with few split pulses (roof sections where a tree top landed),
thin pieces next to a roof (eaves, ridges) and tree patches with roof all around them (rooftop
units). Every tree object stays in one piece. After the rule, a building also gets the roof
hidden under the trees next to it, for its footprint only (roof_under_canopy): pixels where the
pulses that get through the crown end on the roof's surface continued under the tree.

Table "objects": one row per object of at least MIN_AREA. Its point (x, y) is inside the
object (a roof's pixel furthest from any ground point, a tree's top); the window holding that
point keeps the row. Columns:
  roof_seed (1 or 0), area (m^2), height (median ndsm), hole_depth (largest ground_gap),
  hole_share (share of pixels further than HOLE_GAP from any ground point), rectangularity
  (1 for a rectangle, lower for ragged shapes), multi_return (share of split pulses),
  roughness (median), roughness_inside (median without the object's edge pixels: at the edge
  the Laplacian sees the drop to the ground, and on a small object half the pixels are edge),
  ground_slope (degrees: the median slope of the dtm 2-10 m around the object, GROUND_RING),
  ground_tpi (m: how far that ground lies above the ground 20-50 m from the object's point,
  TPI_RING; negative: it lies low, as on a canyon's floor),
  building (the rule's verdict, CODES: 0 = not a building, 1 = building, 2 = shed, container
  or RV, a building under SMALL_STRUCTURE), and with POINT_METRICS the metrics from the
  object's points: n_points, top, h_mean, h_std, h_p50, h_p95, cover, i_mean, i_std (see
  point_metrics), and cover_min (the lowest cover of one flight line: about 0 for a vehicle
  that came or went between two passes; see cover_min; the rule's vehicle guard)

Layer "footprints" (GeoPackage): the roof-seeded objects as polygons, with the same columns
(without roof_seed) and five more:
  outline  how the polygon was made (CODES). A building gets straight walls (straighten): one
           main direction per building, walls within SNAP_ANGLE of it (or square to it) turned
           onto it and placed through the middle of their pixel staircase; diagonal walls of at
           least MIN_DIAGONAL stay. The safety net keeps the exact pixel outline for buildings
           under STRAIGHT_MIN_AREA, and the simplified one (pixel staircases simplified away by
           SIMPLIFY) where the walls don't agree on a direction (ragged, tree-bitten), the result
           is invalid or its area changed too much. Objects that aren't buildings are simplified
           (not under SIMPLIFY_MIN_AREA). Then the roof's own first returns move the straight
           walls to the roof's edge (refine_walls, move_wall, wall_edge): a pixel outline lies a
           little outside the roof, since a pixel is roof if any of its pulses hits the roof.
           Last, a building's parts that are still crinkly (more than SQUARE_CORNERS corners per m
           of outline: pixel outlines, tree-bitten ones) become their main rectangle (square_up),
           from the geometry only; code 6 if that happened to the biggest part.
  review   1 for a tree notch (tree_notch): a diagonal wall with tree in front of it, where a
           tree hangs over the roof and hides part of it (and roof_under_canopy didn't find it).
  under_canopy  m^2 of roof found under trees (in the polygon; area and the rule's features are
           the visible roof's).
  walls_moved, wall_shift  how many walls the points moved, and their median shift (m;
           negative: in). For checking; the polygon's area is then a little under area.
Holes under HOLE_FILL are filled. A footprint is the roof seen from above, eaves included, with
the roof that pulses through overhanging trees show; under a dense crown it is still missing.
The meanings of the codes are stored in the file (coded field domains), so QGIS shows the labels.

Layer "crowns" (GeoPackage): the objects that aren't buildings (building = 0) of at least
CROWN_MIN_AREA, as polygons in the same way, with the same columns (without building). Mostly
tree crowns. roof_seed = 1 marks a roof-seeded object the rule rejected: it is in footprints
too. Neighbouring crowns are simplified one by one, so their shared edges can differ a little.
A crown keeps its whole shape where it hangs over a roof, so it can overlap a footprint.

Summary "flown" (one row per window in flown.csv after merge): gps_first and gps_last, the
window's earliest and latest GpsTime, when the points have it (3DEP: adjusted standard GPS
time, seconds since 1980-01-06 minus 1e9). Says when that piece was flown, e.g. before a fire.

Needs the fields X, Y, Z, Classification, ReturnNumber and NumberOfReturns, and Intensity and
PointSourceId for POINT_METRICS (tilestream reads all fields unless --dims says otherwise).

    python src/tilestream.py run out/ --ept WA_NorthEast_2_B22 --bbox XMIN YMIN XMAX YMAX \\
        --analysis src/buildings.py
    python src/tilestream.py merge out/
"""
import numpy as np
from scipy import ndimage
from scipy.spatial import ConvexHull, cKDTree
from skimage import draw, measure
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
SEED_TREE_MULTI = 0.24  # a seed piece smaller than its building's largest (and than ROOF_PIECE) whose flood has
                        # at least this share of split pulses is a smooth bit in a crown. Chewelah, Michael's
                        # labels of 84 such floods (2026-11-10): 8 trees 0.26-0.33, roof parts up to 0.22
TREE_SMOOTH = 0.5       # m, Gaussian smoothing of the ndsm before finding tree tops
TREE_TOP_RADIUS = 2.5   # m, a tree top is the highest point within this distance; 1.5 m split
                        # big crowns into 2-3 pieces
THIN = 1.0              # m, disk radius: tree pieces narrower than ~2.5 m that touch a roof are roof
                        # (eaves); with 0.5 m, half the eave strips stayed trees
RECLAIM_MULTI = 0.2     # a whole tree object next to a roof is roof below ROOF_MULTI split pulses, or below
RECLAIM_AREA = 25       # this if it has at least RECLAIM_AREA m^2. In Chewelah, 0.3 would take in 4 trees of
                        # 8-14 m; under 25 m^2, 0.15-0.2 includes a 10.7 m high piece of tree
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
SQUARE_CORNERS = 0.3    # corners per m of outline (square_up, the last step): a footprint part with more is crinkly
                        # (Camp Fire: straightened outlines 0.11-0.26, simplified ones 0.39-0.57, pixel outlines
                        # 1.0-1.5) and becomes its main rectangle (main_rectangle): along its main direction, from
SQUARE_COVER = 0.5      # where it covers at least this share of its median width to where it stops, and across the
SQUARE_FINE = 0.25      # same, measured on a grid this fine (m). Only if the rectangle and the part overlap at
SQUARE_MIN_IOU = 0.5    # least this much (intersection / union): less were mostly crowns, rocks and two buildings
                        # in one part
EDGE_SLICE = 0.5        # m (wall_edge, where the roof's own points put a wall): slices this wide along the wall,
EDGE_END = 0.5          # leaving out this much at each end (the corners)
EDGE_FIT = (0.25, 1.5)  # m inside the wall: the roof next to it is fitted to the high first returns there,
EDGE_TOL = 0.3          # and a high first return within this of that fit is on the roof (the fit drops the others)
EDGE_FIT_SD = 0.1       # m: the fit must be at least this good,
EDGE_HAVE = 0.8         # and at least this share of the slices must have a roof point, at least EDGE_SLICES of them
EDGE_SLICES = 4         # (so walls of at least 3 m)
EDGE_OUT = 1.0          # m outside the wall: points up to here are looked at. In the outer half of that strip,
EDGE_FRONT = 0.2        # past the roof, at most this share may be high points off the roof (a tree in front).
                        # Chewelah: the walls move in a median 0.15 m (middle half 0.10-0.20), the pixel bias
EDGE_MAX = 0.5          # m (move_wall): a wall moves at most this far (Chewelah: further were 7 of 858 walls,
                        # mostly next to a building of the same height, whose roof points pushed it out ~1 m)
BUILDING_MULTI = 0.35   # the rule (is_building): over the whole object, a share of split pulses below this,
BUILDING_ROUGH = 0.5    # a median roughness_inside below this (m; as ROOF_ROUGH: a rock is a dome),
BUILDING_SMALL = 25     # m^2: smaller objects need a roughness_inside below BUILDING_ROUGH_SMALL:
BUILDING_ROUGH_SMALL = 0.3  # a shed's simple roof is smoother (0.05-0.15) than a bush (0.36-0.38),
BUILDING_AREA = 4.0     # and an area of at least this (m^2; a 2 x 3 m shed is 6 m^2)
VEHICLE_COVER = 0.2     # and no cover_min below this: one flight line saw the ground there, so it came or went
                        # between passes (Chewelah: 0.00 for 5 vehicles that moved, >= 0.29 for every other object)
SMALL_STRUCTURE = 40    # m^2: a smaller building is a shed, container or RV (40 ft container 30 m^2, big RV 35)
SMALL_MAX_HEIGHT = 12   # m: and the rule: a building smaller than SMALL_STRUCTURE is lower than this (height).
                        # Camp Fire: 106 of 10,709 were higher; all 12-20 m ones looked at were bits of crowns,
                        # bridges and wires (and 49 pieces of wire 71-83 m over water)
BUILDING_MAX_SLOPE = 35  # degrees: and the ground around a building (ground_slope) is less steep than this,
SMALL_MAX_SLOPE = 25    # under SMALL_STRUCTURE less steep than this. Camp Fire, looked at over NAIP: over 35
                        # degrees 0 of 24 were buildings (rocks, cliffs, road fills and bridges), small ones over 25
                        # degrees 0 of 36; at 20-30 degrees 7 of 24 bigger ones were houses
SMALL_MIN_TPI = -8.0    # m: and a small one's ground_tpi is above this: lower lie the floors of canyons, with
                        # their boulders. Camp Fire, over NAIP: of the small ones below -7.5 m 0 of 30 were
                        # buildings (boulders on river beds, trees in ravines), from -7.5 to -5 m 1-3 of 36 (a
                        # trailer at -7.5). Bigger ones aren't judged by it: real houses down to -5.6 m
CANOPY_REACH = 2.0      # m (roof_under_canopy): a building's roof is looked for under trees up to this far
                        # from its visible roof,
CANOPY_FIT = 1.5        # m: on the roof surface continued under the tree: a plane fitted to the dsm of the
CANOPY_FIT_SD = 0.1     # roof pixels within this of the nearest visible roof pixel, which must fit within this (m)
CANOPY_ON = 0.15        # m: a last return within this of that plane hit the roof,
CANOPY_TOL = 0.3        # m: one more than this under it went through where the roof would be (the fit also
                        # drops pixels further off than this)
CANOPY_LAYER = 3        # a patch of roof under a tree has at least this many times as many last returns on
                        # the surface as in the slab just above it (Chewelah: 3,101 to 103 over all the
                        # roof found, a median of 26 per building)
CODES = {"footprints": {"building": {0: "not a building", 1: "building", 2: "shed, container or RV"},
                        "outline": {0: "straightened", 1: "not straightened: too small",
                                    2: "not straightened: walls don't agree on a direction",
                                    3: "not straightened: the result was not a valid shape",
                                    4: "not straightened: the area changed too much",
                                    5: "not straightened: not a building",
                                    6: "main rectangle: the outline was crinkly"},
                        "review": {0: "no", 1: "tree notch: a tree may hide part of the roof"}}}
POINT_METRICS = True    # per-object metrics from the points (heights, cover, intensity; see
                        # point_metrics, and cover_min). Needs the Intensity and PointSourceId fields.
COVER_LINE_SHARE = 0.5  # cover_min: a flight line counts if it reaches at least this share of the object's
                        # pixels (one that grazes the object sees only its edge)
CROWN_MIN_AREA = 5.0    # m^2: smaller tree objects are mostly loose bits just over MIN_HEIGHT (shrubs,
                        # snags): in Nine Mile Falls 44% of the tree objects, but 1% of their area
GROUND = 2
NOISE = (7, 18)         # low and high noise
BRIDGE_DECK = 17        # the USGS lidar specification asks for bridge decks in this class (PlumasNF B2 has them:
                        # the Pulga bridge). A deck is smooth, high and hides the ground like a roof, so it goes
                        # like noise: the dsm shows what is under it. Sets without the class keep their bridges
NAIP_GREEN_NDVI = 0.25  # a NAIP pixel is green above this NDVI (Chewelah 2023, Michael's labels: trees from
                        # 0.29, roof parts up to 0.21)
NAIP_EDGE = 1.0         # m: naip_green counts only roof pixels at least this far from anything that isn't
                        # roof. NAIP shows tall things shifted (in Chewelah 1-3 m), so a crown beside a roof
                        # spills its green onto the roof's edge
NAIP_FLAG_M2 = 4.0      # m^2 of green on a footprint's roof that set naip_flag
SLOPE_SMOOTH = 1.0      # m (ground_slope): the dtm is smoothed this much (Gaussian sigma) before its slope is taken,
GROUND_RING = (2.0, 10.0)  # m: the slope (and the ground of ground_tpi) is read this far around an object: the
                        # ground it stands on (under a roof the dtm is only filled in from around it)
TPI_RING = (20.0, 50.0)  # m (ground_tpi): that ground is compared with the ground this far from the object's
                        # point (within HALO: the point is in the core),
TPI_STEP = 5            # pixels: sampled this often (2.5 m; ~1,000 heights, plenty for a median, and fast)
FEATURES = ("area", "height", "hole_depth", "hole_share", "rectangularity", "multi_return", "roughness",
            "roughness_inside", "ground_slope", "ground_tpi")


def analyze(pts, w):
    x0, ytop, nx, ny = w.grid(RES)
    # flat pixel index of each point; a point exactly on the grid's bottom edge would get row ny
    row = np.minimum(((ytop - pts["Y"]) / RES).astype(np.int64), ny - 1)
    col = ((pts["X"] - x0) / RES).astype(np.int64)
    pixel = row * nx + col

    ground = pts["Classification"] == GROUND
    dtm, ground_gap = fill_from_nearest(mean_per_pixel(pixel[ground], pts["Z"][ground], ny, nx))
    keep = ~np.isin(pts["Classification"], NOISE + (BRIDGE_DECK,))
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
    gy, gx = np.gradient(ndimage.gaussian_filter(dtm, SLOPE_SMOOTH / RES), RES)
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))  # of the ground, for ground_slope

    layers = {"dtm": dtm, "dsm": dsm, "ndsm": ndsm, "ground_gap": ground_gap,
              "multi_return": multi_return, "roughness": roughness}
    grids = {**layers, "pulses": pulses, "split": split, "slope": slope}  # what describe() and objects_table() read
    segments, n_roofs = segment(grids)

    out = {name: Raster(r, x0, ytop, RES, nodata=np.nan) for name, r in layers.items()}
    out["objects"], labels = objects_table(segments, n_roofs, grids, x0, ytop)
    t = out["objects"]
    if POINT_METRICS:
        h = pts["Z"][keep] - dtm.ravel()[pixel[keep]]  # height above the ground
        label = segments.ravel()[pixel[keep]]
        t.update(point_metrics(h, first[keep], pts["Intensity"][keep], label, labels))
        t["cover_min"] = cover_min(h, first[keep], label, pixel[keep], pts["PointSourceId"][keep], labels)
    t["building"] = building_code(t)
    # the roof under trees next to buildings: for their footprints only (the rule and the features
    # judge the visible roof, and the crowns keep their whole shape, over the roof)
    last = keep & (pts["ReturnNumber"] == pts["NumberOfReturns"])
    under = roof_under_canopy(segments, labels[t["building"] > 0], dsm,
                              (pts["X"][last], pts["Y"][last], pts["Z"][last], pixel[last]), x0, ytop)
    with_under = np.where(under > 0, under, segments)
    under_canopy = np.bincount(under.ravel(), minlength=segments.max() + 1)[labels] * RES ** 2
    out["segments"] = Raster(segment_map(with_under, n_roofs, under), x0, ytop, RES, nodata=0)
    # two polygon layers: footprints = the roof-seeded rows, crowns = the rows that aren't buildings.
    # Most roof-seeded rejects are crowns too, so they are in both until we trust the rule.
    roof = t["roof_seed"] == 1
    crown = (t["building"] == 0) & (t["area"] >= CROWN_MIN_AREA)
    traced = trace(np.where(np.isin(segments, labels[roof | crown]), segments, 0), x0, ytop)
    grown = np.unique(under[under > 0])  # the buildings with roof under trees: traced again with it
    traced.update(trace(np.where(np.isin(with_under, grown), with_under, 0), x0, ytop) if len(grown) else {})
    shapes = {i: simplify(g) for i, g in traced.items()}
    # buildings get straight walls where the safety net allows, which the roof's own first returns
    # then move to the roof's edge (refine_walls, move_wall), and a review flag for tree notches
    outline, review = np.full(len(t["x"]), 5), np.zeros(len(t["x"]), int)
    walls_moved, wall_shift = np.zeros(len(t["x"]), int), np.full(len(t["x"]), np.nan)
    if (t["building"] > 0).any():
        fx, fy, fz = pts["X"][first], pts["Y"][first], pts["Z"][first]
        near = (cKDTree(np.c_[fx, fy]), fx, fy, fz, fz - dtm.ravel()[pixel[first]])
    for j in np.flatnonzero(t["building"] > 0):
        i = labels[j]
        geom, _, outline[j] = straighten(traced[i])
        if geom is not None:
            walls = [r[k:k + 2] for rings in polygon_rings(geom) for r in rings for k in range(len(r) - 1)]
            main = main_direction_of(walls)[0]
            others = [traced[k] for k in labels[roof] if k != i]  # pixel outlines: the same in every window
            moved = refine_walls(geom, lambda p0, p1, out: move_wall(p0, p1, out, main, near, with_under,
                                                                     n_roofs, others, x0, ytop))
            if moved is not None:
                geom, walls_moved[j], wall_shift[j] = moved
            geom.Normalize()
            shapes[i] = geom
        review[j] = tree_notch(traced[i], i, with_under, n_roofs, x0, ytop) > 0
        square, squared, main = square_up(shapes[i])  # the last step: crinkly parts become rectangles
        if squared:
            square.Normalize()
            shapes[i] = square
        if main:  # the code tells how the main shape was made, not a small part beside it
            outline[j] = 6

    def layer(rows, drop, **extra):  # the table's rows, without a column that is the same in every row
        return {**{k: v[rows] for k, v in t.items() if k != drop}, **{k: v[rows] for k, v in extra.items()},
                "geometry": np.array([shapes[i].ExportToWkt() for i in labels[rows]])}
    out["footprints"] = layer(roof, "roof_seed", outline=outline, review=review, under_canopy=under_canopy,
                              walls_moved=walls_moved, wall_shift=wall_shift)
    out["crowns"] = layer(crown, "building")
    if "GpsTime" in pts.dtype.names:
        out["flown"] = {"gps_first": float(pts["GpsTime"].min()), "gps_last": float(pts["GpsTime"].max())}
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

    # each seed piece floods with a label of its own: the flood is the same, and merge_parts can
    # see what each piece took
    parts, n_parts = ndimage.label(roofs > 0)
    markers = np.where(tops > 0, tops + n_parts, parts)
    # a small patch next to a taller tree has no top of its own (the tree is higher within
    # TREE_TOP_RADIUS), and the flood never reaches a patch without a seed: seed it at its highest pixel
    patches, n_patches = ndimage.label(high)
    seeded = np.zeros(n_patches + 1, bool)
    seeded[patches[markers > 0]] = True
    unseeded = np.flatnonzero(~seeded[1:]) + 1
    for k, rc in enumerate(ndimage.maximum_position(g["ndsm"], patches, unseeded), markers.max() + 1):
        markers[rc] = k
    segments = watershed(np.where(high, -g["ndsm"], 0), markers, mask=high)
    segments = merge_parts(segments, parts, roofs, n_roofs, g)
    return reclaim_roof(segments, n_roofs, g), n_roofs


def merge_parts(segments, parts, roofs, n_roofs, g):
    """Give the flood of each roof seed piece back to its building. ``segments`` has the pieces
    (``parts``) as labels 1..n_parts, then the trees; ``roofs`` the building each piece seeds.
    A small piece (not its building's largest, under ROOF_PIECE) whose flood has SEED_TREE_MULTI
    or more split pulses was a smooth bit in a dense crown: the 1 m around it hid the tree top,
    so its flood is the crown, and that becomes a tree object. Returns the labels as segment()
    makes them: 1..n_roofs the roofs, then the trees."""
    n_parts = parts.max()
    building = np.zeros(n_parts + 1, np.int64)
    building[parts] = roofs
    seed_area = np.bincount(parts.ravel(), minlength=n_parts + 1) * RES ** 2
    seed_area[0] = 0
    largest = np.zeros(n_roofs + 1)
    np.maximum.at(largest, building, seed_area)  # each building's largest piece
    small = (seed_area < largest[building]) & (seed_area < ROOF_PIECE)
    tree = np.zeros(n_parts + 1, bool)
    if small.any():
        size = segments.max() + 1
        with np.errstate(invalid="ignore", divide="ignore"):  # no pulses: NaN, stays roof
            share = (np.bincount(segments.ravel(), g["split"].ravel(), size)
                     / np.bincount(segments.ravel(), g["pulses"].ravel(), size))
        tree = small & (share[:n_parts + 1] >= SEED_TREE_MULTI)
    new = np.arange(segments.max() + 1) - n_parts + n_roofs  # the trees, right after the roofs
    new[:n_parts + 1] = building
    new[np.flatnonzero(tree)] = new.max() + 1 + np.arange(tree.sum())  # and those floods after them
    return new[segments].astype(segments.dtype)


def reclaim_roof(segments, n_roofs, g):
    """Give the tree pixels that are really roof to a roof:
      - whole tree objects next to a roof with, as a whole, few split pulses (below ROOF_MULTI,
        or below RECLAIM_MULTI from RECLAIM_AREA up): roof sections and roof edges where a tree
        top landed, so the flood made them a "tree". Each goes in one piece to the roof it shares
        the longest border with (longest_border), not pixel by pixel to the nearest roof: that
        could hand part of it to a small roof nearby that it doesn't even touch.
      - thin pieces next to a roof. Eaves, ridges and steps between roof sections look like trees
        (split pulses, sharp bends), and a crown can flood along them deep into a roof. An erosion
        followed by a dilation (an opening) with a disk of radius THIN keeps only the tree pixels
        the disk fits in, so it finds every piece narrower than ~2.5 m.
      - tree patches with roof all around them: rooftop units and raised parts of a building are
        too wide for the opening, but a real tree reaches past the roof edge somewhere, down to
        lower ground.
    The thin pieces and the patches go pixel by pixel to the nearest roof (a strip between two
    buildings is split between them). Taking the thin pieces can cut a tree object in parts.
    The part with its top keeps the label; another part goes to the roof if it touches one, or
    else becomes an object of its own."""
    if n_roofs == 0:
        return segments
    trees = segments > n_roofs
    roof = (segments > 0) & ~trees
    # the row and column of each pixel's nearest roof pixel
    _, (r, c) = ndimage.distance_transform_edt(~roof, return_indices=True)
    size = segments.max() + 1
    with np.errstate(invalid="ignore", divide="ignore"):  # no pulses: NaN, not roof-like
        share = (np.bincount(segments.ravel(), g["split"].ravel(), size)
                 / np.bincount(segments.ravel(), g["pulses"].ravel(), size))
    area = np.bincount(segments.ravel(), minlength=size) * RES ** 2
    roof_like = (share < ROOF_MULTI) | ((share < RECLAIM_MULTI) & (area >= RECLAIM_AREA))
    touching = np.zeros(size, bool)
    touching[segments[trees & ndimage.binary_dilation(roof)]] = True
    whole = trees & (roof_like & touching)[segments]  # whole roof-like objects next to a roof
    take = whole.copy()
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
    best = longest_border(segments, n_roofs)[segments[whole]]  # before the labels change
    segments[take] = segments[r[take], c[take]]
    segments[whole] = best

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


def longest_border(segments, n_roofs):
    """For each label, the roof (1..n_roofs) it shares the longest border with: the most pairs
    of side-by-side pixels; 0 if it touches none. On a tie, the roof whose first pixel comes
    first (top row, then left): positions, unlike label numbers, are the same in every window."""
    size = segments.max() + 1
    best = np.zeros(size, segments.dtype)
    left, right, up, down = segments[:, :-1], segments[:, 1:], segments[:-1], segments[1:]
    a = np.concatenate([left.ravel(), right.ravel(), up.ravel(), down.ravel()])
    b = np.concatenate([right.ravel(), left.ravel(), down.ravel(), up.ravel()])  # a's neighbour
    m = (a > n_roofs) & (b > 0) & (b <= n_roofs)  # an object's pixel beside a roof's
    if not m.any():
        return best
    keys, n = np.unique(a[m].astype(np.int64) * size + b[m], return_counts=True)
    obj, roof = keys // size, keys % size  # each (object, roof) pair, and n its border
    labels, first = np.unique(segments, return_index=True)  # each label's first pixel
    start = np.zeros(size, np.int64)
    start[labels] = first
    order = np.lexsort((-start[roof], n, obj))  # by object, then border, then the earliest roof
    obj, roof = obj[order], roof[order]
    last = np.append(obj[1:] != obj[:-1], True)  # each object's last row: its longest border
    best[obj[last]] = roof[last]
    return best


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
                     **describe(r, c, g), "ground_slope": ring_median(r, c, g["slope"]),
                     "ground_tpi": ground_tpi(r, c, k, g["dtm"])})
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
    NaN (no pulses, no inside) fails the test.
    The vehicle guard: an object below VEHICLE_COVER in cover_min wasn't there in one flight line.
    It is insurance: a vehicle that moved leaves no covered hole (another line saw the ground under
    it), so in Chewelah none got a roof seed. Without cover_min (no POINT_METRICS) or with NaN (no
    line counts) there is no evidence, and the object passes.
    A small structure (under SMALL_STRUCTURE) must be lower than SMALL_MAX_HEIGHT: a shed isn't 12 m
    high. Small smooth objects that high were bits of crowns, bridges and wires.
    The ground: a building stands on ground less steep than BUILDING_MAX_SLOPE (ground_slope), a small
    one less steep than SMALL_MAX_SLOPE. On steeper ground the smooth objects were rocks, cliffs, and
    road fills and bridges. And a small one doesn't stand on a canyon's floor: its ground_tpi is above
    SMALL_MIN_TPI. There the smooth objects with no ground under them were boulders on the river bed.
    Without ground_slope or ground_tpi (or NaN: no ground) there is no evidence: it passes."""
    small = t["area"] < SMALL_STRUCTURE
    rough_max = np.where(t["area"] < BUILDING_SMALL, BUILDING_ROUGH_SMALL, BUILDING_ROUGH)
    moved = np.asarray(t.get("cover_min", np.nan)) < VEHICLE_COVER  # NaN < x is False: passes
    tall_small = small & (t["height"] >= SMALL_MAX_HEIGHT)
    steep = np.asarray(t.get("ground_slope", np.nan)) >= np.where(small, SMALL_MAX_SLOPE, BUILDING_MAX_SLOPE)
    low_small = small & (np.asarray(t.get("ground_tpi", np.nan)) <= SMALL_MIN_TPI)  # NaN <= x is False: passes
    return ((t["roof_seed"] == 1) & (t["multi_return"] < BUILDING_MULTI)
            & (t["roughness_inside"] < rough_max) & (t["area"] >= BUILDING_AREA) & ~moved & ~tall_small
            & ~steep & ~low_small)


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


def roof_under_canopy(segments, owners, dsm, last, x0, ytop):
    """The roof hidden under trees next to the buildings ``owners`` (their labels): a label image
    with the building each pixel of another object belongs to (0: none). Where a tree hangs over a
    roof, part of its pulses get through (Chewelah: 20-55%), and their last returns lie on the roof's
    surface continued under the tree; beyond the roof's edge they reach the ground. ``last`` holds
    the last returns: (x, y, z, pixel). A pixel of another object within CANOPY_REACH of a building,
    with its top not under the continued roof surface (a lower tree beside the wall isn't over the
    roof), is roof:
      - if one of its last returns lies within CANOPY_ON of the surface and none lies more than
        CANOPY_TOL under it (that pulse went through where the roof would be);
      - or, with no last return on or under the surface, if at least 5 of its 8 neighbours are roof
        and one of them was found under the tree (the evidence has gaps: at 3-4 pulses a pixel, a
        third of the pixels get no pulse through).
    Each patch of such pixels must show the thin layer of a surface (CANOPY_LAYER) and connect to
    its building. The surface: a plane fitted to the dsm of the building's pixels within CANOPY_FIT
    of the pixel's nearest building pixel (fit_plane)."""
    ny, nx = segments.shape
    under = np.zeros_like(segments)
    building = np.isin(segments, owners)
    if not building.any():
        return under
    dist, (ir, ic) = ndimage.distance_transform_edt(~building, sampling=RES, return_indices=True)
    cand = (segments > 0) & ~building & (dist <= CANOPY_REACH)
    r, c = np.nonzero(cand)
    er, ec = ir[r, c], ic[r, c]  # each pixel's nearest building pixel
    owner = segments[er, ec]
    # one plane per nearest building pixel, in metres from its centre (x east, y north)
    edges, which = np.unique(er * nx + ec, return_inverse=True)
    planes = np.array([fit_plane(segments, dsm, *divmod(e, nx)) for e in edges]).reshape(-1, 3)[which]

    # each last return in a candidate pixel: its height above the surface there
    index = np.full(ny * nx, -1)
    index[r * nx + c] = np.arange(len(r))
    x, y, z, pixel = last
    j = index[pixel]
    m = j >= 0
    j = j[m]
    dz = z[m] - (planes[j, 0] + planes[j, 1] * (x[m] - (x0 + (ec[j] + 0.5) * RES))
                 + planes[j, 2] * (y[m] - (ytop - (er[j] + 0.5) * RES)))
    on = np.bincount(j[np.abs(dz) <= CANOPY_ON], minlength=len(r))
    below = np.bincount(j[dz < -CANOPY_TOL], minlength=len(r))
    above = np.bincount(j[(dz > CANOPY_ON) & (dz <= 3 * CANOPY_ON)], minlength=len(r))  # the slab just above
    top = planes[:, 0] + planes[:, 1] * (c - ec) * RES + planes[:, 2] * (er - r) * RES  # the surface at the centre
    with np.errstate(invalid="ignore"):  # NaN: no plane, or no dsm
        over = dsm[r, c] >= top - CANOPY_ON  # the pixel's top isn't under the roof

    new = np.zeros((ny, nx), bool)
    new[r, c] = over & (on > 0) & (below == 0)
    box = np.ones((3, 3), int)
    box[1, 1] = 0
    n_roof = ndimage.correlate((building | new).astype(int), box, mode="constant")
    n_new = ndimage.correlate(new.astype(int), box, mode="constant")
    gap = over & (on == 0) & (below == 0) & (n_roof[r, c] >= 5) & (n_new[r, c] > 0)
    new[r[gap], c[gap]] = True
    # each patch shows a surface's thin layer (a crown with last returns at that height by chance
    # has about as many just above), and is connected (4-neighbours) to its own building
    patch, n = ndimage.label(new)
    on_p, above_p = (np.bincount(patch[r, c], v, n + 1) for v in (on, above))
    new &= (on_p >= CANOPY_LAYER * above_p)[patch]
    parts, _ = ndimage.label(building | new)
    size = segments.max() + 1
    mine = np.isin(parts[r, c].astype(np.int64) * size + owner,
                   parts[building].astype(np.int64) * size + segments[building])
    keep = new[r, c] & mine
    under[r[keep], c[keep]] = owner[keep]
    return under


def fit_plane(segments, dsm, row, col):
    """The plane z = a + b dx + c dy (dx east, dy north, in m from the pixel's centre) through the dsm
    of the pixels of the same object within CANOPY_FIT of pixel (row, col): four rounds, each dropping
    the pixels more than CANOPY_TOL off. NaN when fewer than 6 pixels are left or they don't fit
    within CANOPY_FIT_SD (a ridge or a step in the roof)."""
    k = int(CANOPY_FIT / RES)
    dr, dc = np.mgrid[-k:k + 1, -k:k + 1]
    near = np.hypot(dr, dc) * RES <= CANOPY_FIT
    rr, cc = row + dr[near], col + dc[near]
    ok = (rr >= 0) & (rr < segments.shape[0]) & (cc >= 0) & (cc < segments.shape[1])
    rr, cc = rr[ok], cc[ok]
    z = dsm[rr, cc]
    ok = (segments[rr, cc] == segments[row, col]) & np.isfinite(z)
    a = np.c_[np.ones(ok.sum()), (cc[ok] - col) * RES, (row - rr[ok]) * RES]
    z = z[ok]
    inl = np.ones(len(z), bool)
    for _ in range(4):
        if inl.sum() < 6:
            return np.full(3, np.nan)
        coef = np.linalg.lstsq(a[inl], z[inl], rcond=None)[0]
        inl = np.abs(z - a @ coef) <= CANOPY_TOL
    if inl.sum() < 6 or np.std(z[inl] - a[inl] @ coef) > CANOPY_FIT_SD:
        return np.full(3, np.nan)
    return coef


def describe(r, c, g):
    """The FEATURES of one object but the ground's (ground_slope, ground_tpi), from its pixel rows and
    columns and the grids ``g``."""
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


def ring_median(r, c, grid):
    """The median of a grid (the ground's slope or height) in the ring GROUND_RING around an object (its
    pixel rows and columns): of the ground it stands on. NaN if the ring is empty or all NaN (no ground)."""
    near, far = GROUND_RING
    m = int(np.ceil(far / RES))
    r0, c0 = max(r.min() - m, 0), max(c.min() - m, 0)
    r1, c1 = min(r.max() + m + 1, grid.shape[0]), min(c.max() + m + 1, grid.shape[1])
    outside = np.ones((r1 - r0, c1 - c0), bool)
    outside[r - r0, c - c0] = False
    d = ndimage.distance_transform_edt(outside, sampling=RES)  # m to the object
    ring = grid[r0:r1, c0:c1][(d > near) & (d <= far)]
    return np.nanmedian(ring) if np.isfinite(ring).any() else np.nan


def ground_tpi(r, c, k, dtm):
    """The topographic position index of an object (its pixel rows and columns; k: its point): how far
    (m) the ground it stands on (ring_median of the dtm) lies above the ground TPI_RING from its point
    (their median, every TPI_STEP pixels from the point). Negative: it lies low, as on a canyon's floor,
    where the ground rises on both sides. NaN without ground."""
    near, far = TPI_RING
    m = int(far / RES) // TPI_STEP * TPI_STEP
    dy, dx = np.mgrid[-m:m + 1:TPI_STEP, -m:m + 1:TPI_STEP]
    d = np.hypot(dy, dx) * RES
    y, x = r[k] + dy, c[k] + dx
    use = (d > near) & (d <= far) & (y >= 0) & (y < dtm.shape[0]) & (x >= 0) & (x < dtm.shape[1])
    around = dtm[y[use], x[use]]
    if not np.isfinite(around).any():
        return np.nan
    return ring_median(r, c, dtm) - np.nanmedian(around)


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


def wall_edge(p0, p1, out, pts):
    """Where the roof's own first returns put the straight wall from p0 to p1: (shift, why), the shift
    in m along ``out`` (the unit normal pointing out of the building; negative: the wall moves in), or
    NaN and why it can't be measured. A pixel outline lies a little outside the roof: a pixel is roof
    if any of its pulses hits the roof. ``pts`` holds first returns near the wall (noise dropped):
    (x, y, z, height above ground).
    The wall is cut into slices of EDGE_SLICE. The roof next to it is fitted to the high points
    EDGE_FIT inside it, each slice its own height and one slope across (a gable end rises to the
    ridge along the wall), in four rounds that each drop the points more than EDGE_TOL off. In each
    slice the roof point furthest out falls short of the edge by a gap that, for points at random
    with rho per m^2, is exponential with median ln 2 / (rho EDGE_SLICE). So the edge is the median
    of those points over the slices (which ignores the odd gutter, antenna or gap) plus that."""
    x, y, z, h = (np.asarray(v, float) for v in pts)
    length = np.hypot(*(p1 - p0))
    along = (p1 - p0) / length
    a = (x - p0[0]) * along[0] + (y - p0[1]) * along[1]
    d = (x - p0[0]) * out[0] + (y - p0[1]) * out[1]
    n = int((length - 2 * EDGE_END) // EDGE_SLICE)
    k = np.floor((a - EDGE_END) / EDGE_SLICE).astype(int)
    near = (k >= 0) & (k < n) & (d >= -EDGE_FIT[1]) & (d <= EDGE_OUT)
    k, d, z, high = k[near], d[near], z[near], h[near] > MIN_HEIGHT
    inner = high & (d <= -EDGE_FIT[0])
    if n < EDGE_SLICES or inner.sum() < 10:
        return np.nan, "few points"
    fit = np.c_[(k[:, None] == np.arange(n)).astype(float), d]  # a height per slice, one slope across
    inl = inner
    for _ in range(4):
        coef = np.linalg.lstsq(fit[inl], z[inl], rcond=None)[0]
        off = z - fit @ coef
        inl = inner & (np.abs(off) <= EDGE_TOL)
        if inl.sum() < 10:
            return np.nan, "few points"
    # the fit's float sums depend on the order of the points (which differs between windows), but it
    # only decides which points are on the roof; the shift is made of their own distances and a count
    roof = high & (np.abs(off) <= EDGE_TOL)
    outer = np.full(n, -np.inf)
    np.maximum.at(outer, k[roof], d[roof])  # each slice's roof point furthest out
    have = np.isfinite(outer)
    if have.mean() < EDGE_HAVE:
        return np.nan, "few points"
    if np.std(off[inl]) > EDGE_FIT_SD:
        return np.nan, "poor fit"
    front = d > EDGE_OUT / 2
    if front.any() and np.mean(high[front] & ~roof[front]) > EDGE_FRONT:
        return np.nan, "high points in front"
    rho = np.sum(roof & inner) / (n * EDGE_SLICE * (EDGE_FIT[1] - EDGE_FIT[0]))
    return float(np.median(outer[have]) + np.log(2) / (rho * EDGE_SLICE)), ""


def refine_walls(geom, shift):
    """The straightened building ``geom`` with its walls moved: (geometry, number of walls moved,
    their median shift), or None if the result isn't a valid shape (a narrow part whose walls would
    pass each other). ``shift(p0, p1, out)`` gives the move of the wall from p0 to p1, in m along
    ``out``, its unit normal pointing out of the building (into the hole, for a hole's wall); 0 or
    NaN: it stays. Each corner goes to where the moved lines of its two walls cross. Two walls in
    one line each keep their end there, with a jog between them if they move apart."""
    moved, polys = [], []
    for rings in polygon_rings(geom):
        new = []
        for k, ring in enumerate(rings):
            p = ring[:-1]
            p = p[np.hypot(*(p - np.roll(p, 1, axis=0)).T) > 1e-9]  # no walls of length 0
            q = np.roll(p, -1, axis=0)  # wall i runs from p[i] to q[i]
            along = (q - p) / np.hypot(*(q - p).T)[:, None]
            # the building is on the left of a counter-clockwise outer ring, and of a clockwise hole
            left = (signed_area(ring) > 0) == (k == 0)
            out = np.c_[along[:, 1], -along[:, 0]] * (1 if left else -1)
            s = np.array([shift(a, b, o) for a, b, o in zip(p, q, out)], float)
            s[~np.isfinite(s)] = 0
            moved += list(s[s != 0])
            c = np.sum(out * p, axis=1) + s  # wall i's moved line: out[i] . x = c[i]
            corners = []
            for i in range(len(p)):  # corner i, between wall i - 1 and wall i
                a = np.array([out[i - 1], out[i]])
                if abs(np.linalg.det(a)) > 1e-9:
                    corners.append(np.linalg.solve(a, [c[i - 1], c[i]]))
                else:  # in one line
                    corners += [p[i] + s[i - 1] * out[i - 1], p[i] + s[i] * out[i]]
            new.append(np.array(corners + corners[:1]))
        polys.append(new)
    if not moved:
        return geom, 0, np.nan
    result = from_rings(polys)
    return (result, len(moved), float(np.median(moved))) if result.IsValid() else None


def square_up(geom):
    """A footprint with its crinkly parts (more than SQUARE_CORNERS corners per m of outline) as their
    main rectangle (main_rectangle), if it overlaps the part at least SQUARE_MIN_IOU: (geometry,
    whether any part changed, whether the biggest part changed). Geometry only: a pixel outline, or
    one bitten by trees, becomes the rectangle it mostly is. The other parts stay as they are."""
    parts, areas, squared = [], [], []
    for rings in polygon_rings(geom):
        part = from_rings([rings])
        areas.append(part.GetArea())
        squared.append(False)
        if sum(len(r) - 1 for r in rings) > SQUARE_CORNERS * part.Boundary().Length():
            box = main_rectangle(rings)
            if box is not None and part.Intersection(box).GetArea() >= SQUARE_MIN_IOU * part.Union(box).GetArea():
                part, squared[-1] = box, True
        parts.append(part)
    out = parts[0]
    for part in parts[1:]:  # rectangles of two parts can overlap
        out = out.Union(part)
    return out, any(squared), squared[int(np.argmax(areas))]


def main_rectangle(rings):
    """The main rectangle of one polygon (its rings: polygon_rings), or None if it is too small to
    measure. Turned onto its main direction (main_direction_of its walls), the polygon is filled on a
    grid of SQUARE_FINE: along it, the rectangle runs over the columns the polygon covers to at least
    SQUARE_COVER of the median column (cover_ends), and across it the same way by rows. So a narrow
    bit sticking out (a tree's edge) is cut off and a shallow bite (a tree over the roof) filled."""
    main = main_direction_of([w for r in rings for w in wall_pieces(r)])[0]
    t = np.radians(main)
    along, across = np.array([np.cos(t), np.sin(t)]), np.array([-np.sin(t), np.cos(t)])
    pts = np.concatenate(rings)
    u0, v0 = (pts @ along).min(), (pts @ across).min()
    shape = (int(np.ptp(pts @ across) / SQUARE_FINE) + 1, int(np.ptp(pts @ along) / SQUARE_FINE) + 1)
    inside = np.zeros(shape, bool)
    for k, r in enumerate(rings):  # the outer ring, then its holes; a cell is in if its middle is
        rr, cc = draw.polygon((r @ across - v0) / SQUARE_FINE - 0.5, (r @ along - u0) / SQUARE_FINE - 0.5, shape)
        inside[rr, cc] = k == 0
    if not inside.any():
        return None
    (a0, a1), (c0, c1) = cover_ends(inside.sum(axis=0)), cover_ends(inside.sum(axis=1))  # columns, rows
    corners = [(a0, c0), (a1, c0), (a1, c1), (a0, c1), (a0, c0)]
    return from_rings([[np.array([(u0 + a) * along + (v0 + c) * across for a, c in corners])]])


def cover_ends(cover):
    """Where (m from the grid's start) a polygon's cover per column (cells; main_rectangle) starts and
    stops reaching SQUARE_COVER of its median: at each end between the column centres of the last one
    that does and the next, where the cover crosses that level (linearly). So a pixel staircase gives
    its middle, not a cell too much: the cover falls from full to none across it."""
    level = SQUARE_COVER * np.median(cover[cover > 0])
    keep = np.flatnonzero(cover >= level)
    ends = []
    for k, step in ((keep[0], -1), (keep[-1], 1)):
        beyond = cover[k + step] if 0 <= k + step < len(cover) else 0  # < level
        ends.append((k + 0.5 + step * (cover[k] - level) / (cover[k] - beyond)) * SQUARE_FINE)
    return ends


def move_wall(p0, p1, out, main, first, segments, n_roofs, neighbours, x0, ytop):
    """How far the roof's own first returns move the wall from p0 to p1 of a straightened building,
    in m along ``out`` (wall_edge; refine_walls's shift). 0 where the wall stays: where it isn't
    square to the building's main direction (degrees, mod 90; a diagonal is often a tree's edge),
    wall_edge can't measure it, it would move more than EDGE_MAX, more than EDGE_FRONT of the
    pixels 0.75 and 1.25 m in front of it belong to trees (``segments``: labels above n_roofs), or,
    moving out, it would enter one of ``neighbours`` (the pixel outlines of the other roof-seeded
    objects). ``first``: a cKDTree of the first returns' (x, y), and their x, y, z and height above
    the ground (noise dropped)."""
    if abs((np.degrees(np.arctan2(*(p1 - p0)[::-1])) - main + 45) % 90 - 45) > 1:
        return 0.0
    tree, x, y, z, h = first
    length = np.hypot(*(p1 - p0))
    near = np.array(tree.query_ball_point((p0 + p1) / 2, length / 2 + EDGE_FIT[1] + EDGE_OUT), int)
    shift, _ = wall_edge(p0, p1, out, (x[near], y[near], z[near], h[near]))
    if not abs(shift) <= EDGE_MAX:  # NaN too
        return 0.0
    along = (p1 - p0) / length
    a = np.arange(EDGE_END, length - EDGE_END, RES) + RES / 2
    front = np.concatenate([p0 + a[:, None] * along + d * out for d in (0.75, 1.25)])
    if np.mean(label_at(front, segments, x0, ytop) > n_roofs) > EDGE_FRONT:
        return 0.0
    if shift > 0:
        strip = from_rings([[np.array([p0, p1, p1 + shift * out, p0 + shift * out, p0])]])
        inside = (strip.Intersection(g) for g in neighbours if strip.Intersects(g))
        if any(i.GetDimension() == 2 and i.GetArea() > 0.01 for i in inside):  # touching gives a line: no area
            return 0.0
    return shift


def tree_notch(geom, label, segments, n_roofs, x0, ytop):
    """Length (m) of the diagonal walls (not snapped, at least MIN_DIAGONAL) in the outline of the
    object ``label`` with mostly tree pixels in front of them: there a tree hangs over the roof and
    hides its corner (the review flag). In front of a real wall is ground or another building.
    Each pixel edge of a wall is looked at from its middle, half a pixel to either side."""
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
        a, b = label_at(mid + side, segments, x0, ytop), label_at(mid - side, segments, x0, ytop)
        if np.mean(np.where(a == label, b, a) > n_roofs) > 0.5:
            total += length
    return total


def label_at(xy, segments, x0, ytop):
    """The label of the pixel under each point (n, 2) in map coordinates; 0 outside the grid."""
    ny, nx = segments.shape
    r, c = np.floor((ytop - xy[:, 1]) / RES).astype(int), np.floor((xy[:, 0] - x0) / RES).astype(int)
    ok = (r >= 0) & (r < ny) & (c >= 0) & (c < nx)
    return np.where(ok, segments[np.clip(r, 0, ny - 1), np.clip(c, 0, nx - 1)], 0)


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
    """Area of a closed ring."""
    return abs(signed_area(r))


def signed_area(r):
    """The shoelace formula: a closed ring's area, positive if it runs counter-clockwise."""
    return np.sum(r[:-1, 0] * r[1:, 1] - r[1:, 0] * r[:-1, 1]) / 2


def segment_map(segments, n_roofs, under=None):
    """1 for roof-seeded objects, 2 for tree crowns, 3 for a building's roof under a tree (``under``,
    see roof_under_canopy), 0 elsewhere and on the edges between objects."""
    kind = np.where(segments == 0, 0, np.where(segments <= n_roofs, 1, 2)).astype(np.uint8)
    if under is not None:
        kind[under > 0] = 3
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


def naip_green(footprints, naip, segments, block=500.0):
    """NAIP's green on each building's visible roof, as two columns added to the GeoPackage
    ``footprints`` (detect.py's step 4; a check after the run, not part of analyze):
      naip_green  m^2 of its roof pixels (segments = 1, inside its polygon) at least NAIP_EDGE from
                  anything that isn't roof, whose NDVI is above NAIP_GREEN_NDVI and whose green
                  band is at least its blue one (plants are; blue roofs aren't). NULL where NAIP
                  has no data, and for objects that aren't buildings.
      naip_flag   1 if naip_green is at least NAIP_FLAG_M2: perhaps a tree taken as roof (a crown
                  whose smooth bits seeded it, or one with roof all around it); else 0.
    ``naip``: 4 bands (red, green, blue, near infrared), any CRS. ``segments``: the run's segments
    raster. Works in blocks of ``block`` m (by the footprints' x, y): one read of each raster per
    block."""
    from osgeo import gdal, ogr
    gdal.UseExceptions()
    ogr.UseExceptions()
    ds = ogr.Open(str(footprints), 1)
    lay = ds.GetLayer()
    srs = lay.GetSpatialRef().ExportToWkt()
    rows = [(f.GetFID(), f["x"], f["y"], f.GetGeometryRef().Clone()) for f in lay if f["building"] > 0]
    green = {}
    margin = 100.0  # a building whose point is in the block may reach this far out of it
    for key in sorted({(int(x // block), int(y // block)) for _, x, y, _ in rows}):
        x0, y1 = key[0] * block - margin, (key[1] + 1) * block + margin
        n = round((block + 2 * margin) / RES)
        grid = dict(format="MEM", outputBounds=(x0, y1 - n * RES, x0 + n * RES, y1), width=n, height=n, dstSRS=srs)
        seg = gdal.Warp("", str(segments), resampleAlg="near", **grid).ReadAsArray()
        bands = gdal.Warp("", str(naip), resampleAlg="average", **grid).ReadAsArray().astype(np.float32)
        red, nir = bands[0], bands[3]
        has_naip = (red + nir) > 0
        not_blue = bands[1] >= bands[2]  # plants are greener than blue; metal and painted roofs can
        # be blue and still reflect near infrared (a blue roof in Chewelah had NDVI ~0.3)
        with np.errstate(invalid="ignore", divide="ignore"):
            ndvi = np.where(has_naip, (nir - red) / (nir + red), np.nan)
        # each building of the block burnt in as its index + 1
        here = [r for r in rows if (int(r[1] // block), int(r[2] // block)) == key]
        mem = gdal.GetDriverByName("MEM").Create("", n, n, 1, gdal.GDT_Int32)
        mem.SetGeoTransform((x0, RES, 0, y1, 0, -RES))
        v = ogr.GetDriverByName("MEM").CreateDataSource("v")
        vl = v.CreateLayer("l")
        vl.CreateField(ogr.FieldDefn("k", ogr.OFTInteger))
        for k, (_, _, _, g) in enumerate(here, 1):
            ft = ogr.Feature(vl.GetLayerDefn())
            ft.SetGeometry(g)
            ft["k"] = k
            vl.CreateFeature(ft)
        gdal.RasterizeLayer(mem, [1], vl, options=["ATTRIBUTE=k"])
        burnt = mem.ReadAsArray()
        roof = (seg == 1) & (burnt > 0)
        inner = ndimage.distance_transform_edt(roof) * RES >= NAIP_EDGE  # distance to the nearest non-roof pixel
        seen = np.bincount(burnt[roof & has_naip], minlength=len(here) + 1)
        is_green = np.bincount(burnt[inner & (ndvi > NAIP_GREEN_NDVI) & not_blue], minlength=len(here) + 1) * RES ** 2
        for k, (fid, _, _, _) in enumerate(here, 1):
            green[fid] = float(is_green[k]) if seen[k] else None
    for name, kind in (("naip_green", ogr.OFTReal), ("naip_flag", ogr.OFTInteger)):
        if lay.GetLayerDefn().GetFieldIndex(name) < 0:
            lay.CreateField(ogr.FieldDefn(name, kind))
    lay.StartTransaction()
    for f in lay:
        m2 = green.get(f.GetFID())
        f["naip_green"] = m2
        f["naip_flag"] = None if m2 is None else int(m2 >= NAIP_FLAG_M2)
        lay.SetFeature(f)
    lay.CommitTransaction()
    ds = None
    return green
