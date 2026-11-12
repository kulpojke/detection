# Tests

Two kinds of tests:

- **Offline** (`test_tilestream.py`): no network. Points come from a synthetic surface,
  so they run in about 10 s and give the same result every time.
- **Network** (`test_ept.py`): read a real USGS EPT from S3. They are slower (minutes) and
  are skipped unless you pass `--ept`.

## Running

Always set `PYTHONNOUSERSITE=1`. Without it, old packages in `~/.local` shadow the env's
own packages, and `import pdal` fails (see HANDOFF.md).

    # offline only (network tests are skipped)
    PYTHONNOUSERSITE=1 ~/miniforge3/envs/tilestream/bin/python -m pytest

    # everything, under a memory cap
    PYTHONNOUSERSITE=1 systemd-run --user --scope -p MemoryMax=6G -p MemorySwapMax=0 \
        ~/miniforge3/envs/tilestream/bin/python -m pytest --ept WA_NorthEast_2_B22

    # one test, by part of its name
    ... -m pytest -k segment_keeps

`pyproject.toml` puts `src/` on the import path (`pythonpath = ["src"]`), so the tests
import `tilestream`, `buildings` and `example_peaks` directly. This setting comes before
`PYTHONPATH`; to test another copy of a module, see "Adding a test".

## The main idea: seam tests

tilestream cuts an area into windows, analyses each window with a halo (extra margin),
then crops the result back to the window's core. If that works, the windows can't leave a
trace. So the strongest test is: **run a grid of small windows and one big window over
the same area, and require identical output** (every pixel, every table row). These are
the tests with `seam` or `seamless` in their name. If halo, crop, or ownership of points
on a window edge goes wrong, they fail.

## conftest.py (shared setup)

- `--ept NAME`: the EPT for the network tests. Without it, every test marked `network`
  is skipped.
- `site` fixture: picks a real 500 m test square inside the EPT. It looks at the 10
  octree cells nearest the middle of the footprint and reads them thinned (16 m). It then
  keeps a square aligned to the 250 m grid, with data in all four 250 m quarters and the
  most relief (tall objects or steep ground, which give a seam something to break). So no
  test is tied to one place: any EPT works.

## test_tilestream.py (offline)

Two helpers make the fake lidar:

- `synthetic_points(bounds)`: a 0.5 m lattice of points over a sloping surface with a
  peak every ~40 m. A few points are noise (class 7, 50 m too high), chosen by their
  location and not their position in the array. A 100 x 100 m square has no points at
  all. Because each point depends only on where it is, any window gets exactly the
  points a whole-area read would.
- `fake_ept`: replaces `ts.fetch_points` with `synthetic_points` and records every call,
  so a test can count how many windows were fetched.

**Area of interest**
- `test_load_tiles`: reads a USGS download list and finds each tile's UTM zone and 1 km
  bounds from its name (e.g. `11TMN470250`).
- `test_load_tiles_errors`: refuses names it can't parse, and lists that mix UTM zones.
- `test_utm_srs`: chooses the right UTM EPSG code: NAD83(2011) in the US, WGS 84 for
  Hawaii and outside the US.
- `test_load_bboxes`: reads a sites CSV with or without a `name` column.

**Windows**
- `test_grid_window_counts`: the right number of windows over an L-shaped area for three
  window sizes. Each core is exactly `core x core`, the halo is the core grown on every
  side, and every name is unique.
- `test_grid_windows_cover_boxes_exactly_once`: every point, including points exactly on
  window edges, is owned by exactly one window. Core bounds are half-open (`xmin <= x <
  xmax`), so a point on an edge belongs to only one side.
- `test_grid_windows_serpentine`: with `order="rows"` the windows snake back and forth,
  so each window follows a neighbour.
- `test_grid_windows_clip`: `bbox=` keeps only the windows touching that box.
- `test_bbox_windows_names_and_halo`: site names become safe file names (`my site` ->
  `my_site`). Sites without names get `site_00000`, ... Duplicate names are refused.
- `test_bbox_windows_lonlat_encloses_box`: a lon/lat box becomes a slightly curved shape in
  UTM; its window must still enclose all of it.
- `test_plan_without_ept`: `--bbox`, `--bboxes` and download-list plans need no EPT, and
  find the srs from the area. Wrong combinations of area options are refused.

**Rasters and cropping**
- `test_window_grid_snaps_and_covers_halo`: a window's pixel grid starts on a multiple of
  the resolution (so pixels of neighbouring windows line up exactly) and covers the halo.
- `test_crop_raster_exact_for_grid_window`: cropping to a 500 m core gives 500 x 500
  pixels at the right corner.
- `test_crop_raster_snaps_bbox_outward`: a site with odd coordinates is cropped to whole
  pixels, rounded outward so the site is fully covered. This also works for multi-band
  rasters.
- `test_crop_raster_rejects_raster_not_covering_core`: an analysis that returns a raster
  smaller than the core is an error, not a silent gap.
- `test_crop_geotiff_matches_crop_raster`: cropping a GeoTIFF file (for `--exec` outputs)
  gives the same pixels, position and nodata as cropping in memory.
- `test_crop_csv`: keeps the rows whose `x, y` lies in the core (half-open: `x = 10` is
  out). A CSV without `x, y` columns is kept whole.

**run / merge**
- `test_run_merge_is_seamless`: the seam test on synthetic points, with
  `example_peaks.py` in 250 m and 500 m windows. The mosaic and peaks are identical to one
  big window's, noise is dropped, and `stats.csv` has one row per window.
- `test_point_on_grid_bottom_edge` (for `example_peaks` and `buildings`): a point exactly
  on the grid's bottom edge used to get row `ny`, one past the end, and crash. It must land
  in the last row.
- `test_run_resumes_and_limits`: `limit=3` does 3 windows. A second run does only the
  rest (counted with `fake_ept`), and a third run fetches nothing.
- `test_run_refuses_to_mix_settings`: an output folder started with one srs can't be
  continued with another (`_run.json`).
- `test_run_skips_empty_windows`: a window without points isn't analysed, but is marked
  done with 0 points, so a resume doesn't fetch it again.
- `test_run_bbox_sites`: named sites of odd sizes give one file per site, with the right
  size and corner. Each site's peaks lie inside its own site.
- `test_run_merge_features`: a table with a `geometry` column (WKT) becomes a GeoPackage
  per window, and `merge` joins them into one. The toy analysis `squares` returns 20 m
  squares, some of them across a core edge. Each square must be written once and whole, by
  the window that owns its `x, y`, so a grid gives the same features as one big window.
  Integer, float and text columns survive, and NaN becomes an empty (NULL) field. A coded
  column (`codes=`) gets its labels stored in the file as a field domain, and the merged
  file still has them.

**buildings.py**
- `test_rectangularity`: a rectangle scores 1, a 45-degree rotated rectangle more than
  0.9 (its edges are pixel steps), an L-shape exactly 341/441, and a disc about pi/4.
- `test_segment_keeps_small_patch_next_to_tree`: a 2 x 2 m patch half a metre from a
  taller tree has no tree top of its own (the tree is higher within `TREE_TOP_RADIUS`),
  but must still become its own object. Before the fix, such patches (sheds!) vanished.
- `test_segment_separates_tree_over_roof`: a tree hangs over a roof edge. Its crown also
  hides the ground, so the roof's covered hole runs on under it. The crown must still
  become a tree of its own: the roof seed is only the roof-like part of the hole. (A pixel
  or two at the crown's pointed edge may go to the roof; see `test_reclaim_roof`.)
- `test_segment_seeds_small_shed`: on a 2 x 3 m shed every pixel is near the edge, so no
  pixel looks like roof by itself (high 3 x 3 `multi_return`). The whole hole is then the
  seed, and the shed is still a roof.
- `test_segment_seeds_roof_in_hole_under_tree`: a dense tree against a house hides the ground
  too, so the house's covered hole runs on under the tree, and over the whole hole too many
  pulses split (the test checks that). The house is still seeded from its roof-like piece of
  at least `ROOF_PIECE` and stays a roof, and the tree stays a tree. A dense crown on its own,
  with a smooth 9 m² bit at its top, gets no seed. Fails without the second route, when small
  pieces seed too, and when the whole hole becomes the seed.
- `test_segment_small_seed_in_crown`: a tree over a roof edge, as in
  `test_segment_separates_tree_over_roof`, but with a 1 m² smooth bit (no split pulses) next
  to its top. That bit is a roof-like piece of the roof's hole, so it is seeded with the
  roof, and no tree top may lie within 1 m of it: its flood takes the crown. The crown must
  still become a tree (`merge_parts`, `SEED_TREE_MULTI`). Fails without `merge_parts`: the
  tree top is then in the roof.
- `test_is_building`: the rule, on real objects from Nine Mile Falls and Chewelah: the house
  and the shed are buildings; a crown with a roof seed (split pulses, rough) and the rock
  (rough inside) are not. A 16 m² bush (0.36 inside) fails, because small objects must be
  smoother than `BUILDING_ROUGH_SMALL`, while a 180 m² house just as rough (0.40) passes.
  A tree-seeded object, one under `BUILDING_AREA`, and one without pulses (NaN) all fail.
  It works on a whole table and on one row. `building_code` turns this into the `building`
  column: the shed (under `SMALL_STRUCTURE`) gets 2, the houses 1, the rest 0. The vehicle
  guard: the same shed with `cover_min` 0 (one flight line saw the ground there) fails, with
  0.63 (its real value) or NaN it passes, and without a `cover_min` column (the table above)
  too. Fails when the guard is gone, when NaN fails, and when a missing column counts as 0.
  A small structure must be lower than `SMALL_MAX_HEIGHT` (12 m): the shed at 11.9 m passes,
  at 12 m it fails (a piece of wire or bridge), and a house at 14 m passes. The ground: a
  house on ground of 34.9° passes, at 35° (`BUILDING_MAX_SLOPE`) it fails; the shed at 24.9°
  passes, at 25° (`SMALL_MAX_SLOPE`) it fails; with `ground_slope` NaN (no ground) it passes.
  The canyon floor: the shed with `ground_tpi` −7.9 m passes, at −8 m (`SMALL_MIN_TPI`) it
  fails (a boulder on a river bed); a house at −20 m passes (big ones aren't judged by it);
  NaN passes. Fails when the rule is gone or also judges big ones.
- `test_ring_median`: `ring_median` (`ground_slope`) reads the ring 2-10 m around a 5 x 5 m
  object (`GROUND_RING`): the slope grid is 30° there and 0 everywhere else (under the object,
  the 2 m next to it, beyond 10 m), so it must give exactly 30, also for an object at the grid's
  top edge (the ring cut off). A slope grid of NaN (no ground points) gives NaN.
- `test_ground_tpi`: a canyon around a 4.5 x 4.5 m object: the floor at 100 m out to 15 m from
  its point, the walls at 120 m from 20 m (nothing in between), so `ground_tpi` is −20; also
  with the grid cut off 2.5 m left of the point (the median of the ground that is there). Flat
  ground gives 0, no ground NaN. Ground only within 20 m of the point and beyond 50 m gives NaN:
  `TPI_RING` reads neither. Fails without ring_median (the floor's height) and without either
  end of `TPI_RING`. Without the check for the grid's edges, `test_analyze_layers` fails (its
  40 m grid is smaller than `TPI_RING`: negative indices).
- `test_describe_roughness_inside`: on a 3 x 4 m flat shed half the pixels are edge pixels,
  so its median `roughness` is high (> 1), but `roughness_inside` is 0. A dome-shaped rock
  stays rough inside (a paraboloid: Laplacian 0.625 everywhere).
- `test_reclaim_roof`: tree pixels that are really roof go to the roof: a 1 m strip along
  the roof edge (an eave; the opening removes it) and a 3 x 3 m patch with roof all around
  it (a rooftop unit). A crown hanging over the roof edge stays a tree (but for a few pixels
  at its pointed edge), and so does a thin hedge that doesn't touch the roof.
- `test_reclaim_roof_roof_like_object`: a whole "tree" object against a roof with few split
  pulses (0.1, below `ROOF_MULTI`) is a roof section where a tree top landed: it goes to the
  roof. A crown against the other side, with many split pulses, stays a tree.
- `test_reclaim_roof_bigger_roof_like_object`: from `RECLAIM_AREA` (25 m²) up, a whole "tree"
  object against a roof with fewer than `RECLAIM_MULTI` (0.2) split pulses goes to the roof
  (a 36 m² one with 0.17, like "edge of building" in Chewelah). A 12 m² one with 0.17 stays
  a tree (small ones need fewer than `ROOF_MULTI`), and so does a 30 m² one with 0.25. Fails
  with 0.15 for all, with 0.2 at every size, and with 0.3.
- `test_reclaim_roof_whole_object_to_longest_border`: a whole roof-like object goes in one piece
  to the roof it shares the longest border with (`longest_border`), even where a small roof on
  its far side is nearer to some of its pixels (Chewelah fp66 got a loose piece that way). Between
  two equal roofs with equal borders, the upper one (first in the image) gets it. Run twice,
  with the roof labels swapped: positions decide, not label numbers, so every window agrees.
  Fails with the nearest roof pixel, with ties by label number, with the shortest border, and
  with no tie rule.
- `test_reclaim_roof_keeps_objects_whole`: two "trees" flooded along the roof's edges from a
  top beside the roof. The strips go to the roof, which cuts each object in parts. The part
  with the top keeps the label; a cut-off part next to the roof goes to the roof; one that
  only touches the strip (now roof) becomes an object of its own. Every object ends up in one
  piece. Before the fix, such objects became multipolygons in `crowns.gpkg` (fid 364 in
  Chewelah: three parts of a roof's edge).
- `test_outlines`: `trace` then `simplify`. A 2 x 3 m shed's outline is exactly its 4 pixel-edge
  corners, in map coordinates. A 54 m² rectangle turned 45 degrees loses its pixel staircase, with about the
  same area. A 15 m² one (under `SIMPLIFY_MIN_AREA`) keeps its exact staircase: simplified,
  it grew 15%, because simplifying keeps the outer corners of a staircase.
- `test_straighten`: shapes turned 30 degrees, drawn in 0.5 m pixels (a pixel is in if its
  centre is), come back from `straighten` with their true corners (each within 0.15 m) and
  area (within 2%): a 12 x 8 m rectangle, an L, a rectangle with a diagonal wall (kept), a
  6 x 6 m garage, and a rectangle with a 3.2 m cut corner. That cut is shorter than
  `MIN_DIAGONAL`, so it counts as a ragged bit and the walls meet at the corner, although the
  corner is further than `MAX_SHIFT` from the middle of the cut (square walls always meet).
- `test_straighten_keeps_neighbours_apart`: two buildings 1 m apart stay more than 0.75 m
  apart: a wall moves to the middle of its pixel staircase, not outward.
- `test_straighten_safety_net`: a 2 x 3 m shed is too small (code 1), and a disc's walls point
  every way, so they don't agree on a direction (code 2); neither gets a new outline.
- `test_main_rectangle`: the pixel outline of a 10 x 4 m rectangle turned 30°, 12° and 0°
  gives a rectangle within 0.07 m of 10 x 4, in the turned direction (±1°). Fails without
  the interpolated ends (`cover_ends`: a cell too big on every side). A 10 x 6 m rectangle with
  a 2 x 2 m bite out of a corner and a 1 x 3 m bit sticking out of the top gives 10 x 6 (bite
  filled, bit cut off); fails when every column counts (no `SQUARE_COVER`).
- `test_square_up`: the turned pixel outline (1.5 corners per m) becomes a 4-corner rectangle
  of ~40 m²; a plain rectangle (not crinkly) and a 24-corner star like a crown (its rectangle
  overlaps it 0.3, under `SQUARE_MIN_IOU`) stay; two such sheds in one MultiPolygon each get
  their own rectangle; a shed beside a 20 x 12 m house (not crinkly) changes, but not the
  biggest part (no code 6). Fails without the corner test or the overlap guard, or when the
  biggest part's flag is any part's.
- `test_trace_fills_small_holes`: a one-pixel hole in a roof is filled, a 2 x 2 m light well
  stays.
- `test_tree_notch`: a tree over a roof's corner leaves a diagonal wall with tree in front of
  it, a tree notch of about 5.7 m. The same diagonal with ground in front is a real wall (0),
  and so is a straight wall with a tree beside it.
- `test_wall_edge`: `wall_edge` on random first returns (`wall_scene`, 14 per m², a fixed seed
  per roof) around a 10 m wall drawn 0.2 m outside the roof's true edge, as a pixel outline
  would be. Over 20 roofs the mean shift is within 3 cm of the truth (one wall scatters about
  ±3.5 cm) on a flat roof, one sloping down to its edge, a gable end (a ridge along the wall:
  one plane doesn't fit it) and with the wall inside the roof (it moves out). Shuffled points
  give exactly the same shift. A roof without points along 3 m of the wall ("few points"), a
  rough roof (0.15 m noise: "poor fit") and a tree in front ("high points in front") aren't
  measured. Without the ln 2 / (ρs) term, the per-slice heights, the slope across, the
  height test or any of the three checks, it fails.
- `test_refine_walls`: `refine_walls` on polygons turned 30° (`polygon`), so no normal lies
  along an axis. A 12 x 8 m rectangle with every wall 0.2 m in becomes 11.6 x 7.6 m, whether
  its ring runs counter-clockwise or clockwise. With a courtyard (a hole), the courtyard grows
  0.2 m on every side. In an L, moving only the bottom wall moves only its two corners. Two
  6 x 6 m rooms joined by a 0.4 m neck, all walls 0.3 m in, give no valid shape (None). With
  nothing moved, the same geometry comes back. Fails if it takes every ring's building side
  as the left, ignores that a hole runs the other way, skips the validity check or moves
  the walls the wrong way.
- `test_move_wall`: `move_wall` on `wall_scene`'s points, with a small segments raster (the
  building at y < 0). It passes `wall_edge`'s shift on for a wall square to the main
  direction. The wall stays (0) at 20° off the main direction (a diagonal), with its edge
  0.7 m in (more than `EDGE_MAX`), with a crown's pixels in front of it, and, moving out by
  0.3 m, into a neighbour 0.1 m out (one 0.6 m out doesn't stop it). Each of those four
  checks is needed for the test to pass.
- `test_roof_under_canopy`: `roof_under_canopy` on a hand-built scene (`canopy_scene`): a
  10 x 10 m roof sloping east and north (so a wrong sign in either direction shows) and a
  crown over its east edge, 2 m over the roof and 4 m beyond it, four pulses per pixel. Where
  half the pulses reach the roof's surface, the pixels go to the building, exactly the 2 x 4 m
  under the crown. Beyond the edge one pulse reaches the ground and one hits a branch at the
  roof's height: the ground hit keeps those pixels tree. A pixel without a pulse through, with
  roof around it, fills in; a lone pixel just beyond the edge with only roof-height returns
  isn't connected and stays out. Shuffled points give the same result. Nothing is added for a
  dense crown (no pulse through), a tree lower than the roof, returns at the roof's height only
  by chance (as many in the slab just above: no thin layer), or an object that isn't a
  building. Fails without each of these rules, and with either sign of the north slope flipped.

- `test_analyze_layers`: the whole `analyze()` on synthetic points (`scene_points`): one
  pulse every 0.25 m over flat ground, with a house (flat roof), a tree (a cone whose pulses
  split and also reach the ground), a smooth dome (a "rock") and a tiny bush. The house is a
  footprint with code 1, the tree a crown. The rock gets a roof seed but the rule rejects it
  (rough inside), so it is in both layers (`building` 0, `roof_seed` 1). The bush is an
  object but too small for a crown. The house gets straight walls (`outline` 0: 4 corners,
  10 x 12 m; this caught a bug where a wall that is a single pixel edge had no direction), the
  rock `outline` 5 (not a building), neither a `review` flag; only footprints have these
  columns, and neither has roof under trees (`under_canopy` 0). The rock has no walls moved
  (`walls_moved` 0, `wall_shift` empty). The tree's polygon lies around
  its top, with about the cone's area. The point metrics fit: the house is a flat 4 m roof (`h_std` 0, `cover` 1,
  its intensity, and `cover_min` 1: the scene has one flight line), and the tree's `top` is its
  highest point, with its own intensity. The ground is flat: `ground_slope` and `ground_tpi` 0.
- `test_analyze_drops_bridge_deck`: the same scene with the house's points labelled bridge
  deck (class 17, `BRIDGE_DECK`) by the lidar. They are dropped like noise, so no object is
  left there, only a hole in the dsm (NaN at the house's middle: the deck hid what is under
  it). The rock is the only footprint. With the class kept, the deck is a building (code 1).
- `test_analyze_roof_under_tree`: the whole `analyze()` on the house with a crown over its
  north-east corner (`tree_over_house`; every other pulse in the crown gets through). The rule
  sees the visible roof (under 113 m²); the footprint gets the roof under the tree back
  (`under_canopy` 5-10 m²): the whole 10 x 12 m with 4 straight walls and no `review` flag. The
  crown keeps its whole shape over the footprint, and `segments` marks the roof under it (3).
  Fails with the step switched off.
- `test_analyze_moves_walls`: the whole `analyze()` on a 10 x 12 m house measured at random
  places, 14 first returns per m² (`random_house`), its walls off the pixel grid. The pixel
  outline lies outside the roof (`area` over 124 m²); the points move all 4 straight walls in
  (`walls_moved` 4, `wall_shift` between -0.2 and -0.05 m), to the true 120 m² (within 2 m²).
  `area` stays the pixel area. Shuffled points give the same footprint. Fails if `analyze`
  doesn't use `refine_walls`.
- `test_point_metrics`: heights, cover and intensity per object from a handful of points,
  including a return under `MIN_HEIGHT` (counts for `cover`, not for heights), a second
  return (counts for heights, not for intensity), and an object with nothing above
  `MIN_HEIGHT` (NaN). Shuffled points give exactly the same result: the points can arrive in
  another order in each window.
- `test_cover_min`: the vehicle clue, the lowest cover of one flight line. A "vehicle" there in
  one line and gone in the other gets 0. A "building" there in both gets 1, while a third line
  that reaches only 1 of its 4 pixels (it grazes the edge and sees the ground) doesn't count, and
  neither does a second return. An object without points gets NaN. Shuffled points give the
  same result.

**After the run: NAIP, detect.py**
- `test_naip_green`: an 8 x 8 m roof with a 3 x 3 m green patch in its middle (a tree taken as
  roof), a green 0.5 m strip along its edge (a crown beside it, shifted onto the roof in NAIP)
  and a blue patch with a high NDVI (a metal roof). Only the patch counts: `naip_green` 9 m²,
  `naip_flag` 1. An object that isn't a building gets NULL. Without the edge rule it would be
  13 m², without the blue check 10 m².
- `test_keep_both_and_join_parts`: two lidar sets in one square (detect.py). A building both
  saw whole keeps the bigger outline (`keep_both`); a building cut by the edge between them
  (each saw a part, overlapping 1 m) becomes one footprint (`join_parts`): the union, with the
  areas added up less the overlap, and both sets in `lidar`.
- `test_add_flown_and_flown_areas`: when the lidar was flown (detect.py, `--before`). Set A was
  flown in two parts, before a date and after it, with a strip of both between them; set B only
  before. Each footprint gets the dates under its corners from its own set(s) (`add_flown`):
  before, across the edge, after, a building of both sets, and NULL where its set has no map.
  `flown.gpkg` has the three periods with the right areas, and the km² inside the area
  (`flown_areas`). Without NaN outside a set's map, the area outside B's map would count as
  "before".

**--exec analyses** (a shell command per window instead of a Python function)
- `test_exec_contract`: what the command gets. The arguments are its own, then the LAZ
  file, then the output folder. The environment variables are `TS_NAME`, `TS_SRS`,
  `TS_CORE` and `TS_HALO`, and the env's `bin/` is on `PATH`, so `pdal` is found. CSVs with
  `x, y` are cropped to the core; other files are kept per window. The work folder and LAZ
  are removed afterwards.
- `test_exec_failure_stops_run_and_keeps_window_todo`: a command that fails stops the
  run, and the window isn't marked done. Its work folder stays for debugging, but the LAZ
  is removed.
- `test_exec_example_dsm_is_seamless`: the seam test for `example_dsm.sh`: eight 250 m
  windows against one 1000 x 500 m window.

## test_ept.py (network, real data)

- `test_info`: the EPT's metadata: it has points, X/Y/Z fields and a sensible footprint,
  and the test site lies inside its data.
- `test_fetch_small_bbox`: a 100 m box gives more than 1 point/m², the usual fields, and
  only points inside the box.
- `test_fetch_dims_keeps_only_requested_fields`: `dims=` returns only those fields.
- `test_fetch_empty_area_keeps_fields`: a box with no data gives an empty array that still
  has the fields of a full read. Before the fix it had none, and analyses crashed with
  `KeyError: 'Classification'` in data gaps.
- `test_fetch_resolution_thins`: `resolution=` really thins (coarser means fewer points).
- `test_fetch_big_window_thinned`: a 3 x 3 km read at 8 m stays small (under 5 M points).
- `test_seam_python_analysis`: the seam test on real data: four 250 m windows against
  one 500 m window with `example_peaks.py`. The DSM and peaks are identical, and the site
  is over 90% data (so the test isn't passing on empty windows).
- `test_cli_exec_seam`: the same through the command line, with `--exec example_dsm.sh`:
  a `--bbox` grid against the site as one `--bboxes` window.
- `test_cli_python_analysis_lonlat_sites`: `--analysis` with lon/lat sites. The srs is
  chosen automatically and recorded once in `_run.json`, and each site's raster covers
  its box.
- `test_plan_all_covers_site`: `--all` plans 1 km windows over the whole EPT, and the
  site's centre falls in exactly one of them.

## Not covered yet

- A seam test for `buildings.py` (all its rasters, the objects table and the footprints). It
  was checked by hand on Chewelah and Nine Mile Falls: identical (this found float noise in
  `rectangularity`, now fixed). The synthetic points have no buildings or trees, so this
  needs real data (a network test).
- The roof seeds in `segment()` (covered holes, few split pulses, smooth).

## Adding a test

- Offline if possible: use `synthetic_points`, or build small arrays by hand like
  `test_segment_keeps_small_patch_next_to_tree`.
- A test for a bug should fail without the fix. To check, put a copy of the module
  without the fix (or with the code you want the test to catch broken) in another folder.
  Then run pytest with `-c /dev/null`, which ignores `pyproject.toml`, so `PYTHONPATH`
  decides which copy is imported:

      PYTHONNOUSERSITE=1 PYTHONPATH=/path/to/copy:src ~/miniforge3/envs/tilestream/bin/python \
          -m pytest -c /dev/null --rootdir=. -p no:cacheprovider test/test_tilestream.py -k NAME
