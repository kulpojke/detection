# Handoff: tilestream, windowed lidar processing from any EPT

_Last updated 2026-11-10 (Claude, after `cover_min`, `ROOF_PIECE`, the merge limit,
`longest_border` (runs 22-24), the vehicle guard, the walls measurement (step 1), the roof
under the canopy (runs 25-27), `wall_edge` (walls step 2), moving the walls (step 3, run 28)
the small seeds in crowns (`merge_parts`, run 29, from Michael's survey labels), and
`detect.py` with the NAIP check (`naip_green`), the flight dates (`--before`) and the Camp Fire
pre-fire run (`runs/camp_fire_prefire`))._

## Goal
A reusable point-cloud querying layer. Give it an EPT (any USGS 3DEP project in
usgs-lidar-public, or any ept.json URL) and an area. It streams windows of points,
each with a halo, to any analysis as they arrive, with no edge effects and nothing big
on disk. Analyses plug in as Python (`--analysis file.py`) or as any command or shell
script (`--exec ./my_tool.sh`). **Buildings are not classified in WA_NorthEast_2_B22**
(only classes 1, 2, 7, 18), so we detect them ourselves, in a separate analysis:
`src/buildings.py` (see "Building detection"). tilestream stays the reading layer; no
building logic goes into it.

## Status (2026-11-10)
- **Camp Fire, pre-fire buildings (2026-11-10, Michael's request, for someone else):**
  `src/detect.py` ran buildings.py over the whole fire perimeter from both pre-fire sets
  (`runs/camp_fire_prefire/`, git-ignored; its `README.md` says where everything came from).
  A first, automatic result: nobody has checked it building by building yet. See "Camp Fire run".
  **B4 is only partly pre-fire:** a strip through central and eastern Paradise was flown
  2018-07-07, everything west of it after the fire (2018-12-29, 2019-02-21, 2019-08-09). So
  western Paradise has no pre-fire buildings; `detect.py --before` leaves its survivors out.
- **Offline suite: 78 passed** (10 network tests skipped), in about 25 s.
- **The network suite runs in CI on every push** (see Tests). The first run (2026-11-05,
  commit `8966d9e`) passed: 10 of 10, in 2 min 46 s.
- **First use case: the 2018 Camp Fire** (Paradise, CA). `camp_lidar_memo.md` lists the
  pre-fire (Jul 2018), post-fire (2019) and recovery (Nov 2021) 3DEP sets, all with EPTs.
  Paradise pre-fire is only in `USGS_LPC_CA_NoCAL_Wildfires_B4_2018`, **QL2** on paper (≥ 2
  pulses/m²) but measured (2026-11-10, central Paradise) at **9.8 pulses/m²** (17.7 points,
  5.2 ground points per m²), flown 2018-07-07, so the Chewelah thresholds fit. But only a strip
  of B4 is pre-fire (above); the memo's "87%, all of Paradise" was wrong. tilestream reads
  these EPTs (EPSG:3857 like WA; the default CRS becomes NAD83(2011) UTM 10N, EPSG:6339).
- **tilestream, new since then:**
  - an analysis can return polygons (features), which become GeoPackages;
  - `codes` / `CODES`: the meaning of coded columns is stored inside the GeoPackage.
- **buildings.py:** steps 1–4d are done. It writes footprint polygons with a `building`
  code, crown polygons, and metrics per object from the points (heights, cover,
  intensity, and `cover_min`, the vehicle clue). Buildings get straight walls (4d), with an
  `outline` code that says how each polygon was made, and a `review` flag for tree notches.
  Since 2026-11-09 footprints also get the roof under overhanging trees (`under_canopy`).
  See "Building detection".
- Repository: https://github.com/kulpojke/detection.
- Earlier (2026-10-30), for reference:
  - The network test-site finder (`test/conftest.py`) picks the 500 m square at
    (445000, 5300000) in EPSG:6340. There, full density is 28.3 pts/m², 3.9 at
    `resolution=2` and 0.19 at `resolution=8`.
  - A 3 km window at `resolution=8` read 0.65 M points in 2.3 s.

## Decision: read from the public EPT with PDAL, not rockyweb LAZ
- rockyweb is slow and unreliable (HTTP 429s; one 59 MB tile took 489 s).
- 3DEP is on AWS as EPT octrees, stored in EPSG:3857 with all LAS attributes. Each
  window queries only its own box.
- Checked against delivered tile 11TMN470250: point counts within 0.002%, class counts
  identical, positions match to a 0.27 cm median after reprojecting to EPSG:6340.

## How to use it
`--ept` is required everywhere. Exactly one area must be given; no area is an error,
so a forgotten `--bbox` can't start a run over all 167 billion points.

    export PYTHONNOUSERSITE=1                                  # see Environment
    P=~/miniforge3/envs/tilestream/bin/python
    $P src/tilestream.py info --ept WA_NorthEast_2_B22        # footprint, points, fields, default CRS
    $P src/tilestream.py plan --ept WA_NorthEast_2_B22 --bbox -117.42 47.40 -117.38 47.42 --core 250
    $P src/tilestream.py run out/ --ept WA_NorthEast_2_B22 --bbox -117.42 47.40 -117.38 47.42 \
        --analysis src/example_peaks.py                        # or: --exec src/example_dsm.sh --halo 5
    $P src/tilestream.py merge out/

- **One command for buildings over an area: `src/detect.py`** (2026-11-10). It plans 500 m
  squares that touch the area and have data, runs buildings.py over them with tilestream in
  `--jobs` processes, combines several lidar sets, adds the NAIP check and writes a README:

      $P src/detect.py runs/NAME --ept EPT [EPT ...] --area AREA.geojson [--naip NAIP.vrt] \
          [--before 2018-11-08] [--jobs 12] [--note "a line for the README"]   # or --bbox ...
  Rerun the same command to resume (finished squares are skipped; combining, NAIP and the
  README run again). Where two sets have points in one square, objects come from both: the
  bigger outline of the same object seen twice (`keep_both`, overlap > half), and the parts of a
  building cut by the data's edge joined (`join_parts`). The rasters and `windows.gpkg` take each
  square from the set with the most points. Every run maps when each 10 m of each set was flown
  (`flown/<set>.tif`, from a thinned read of the GPS times: ~4 min per 800 squares) and gives
  each footprint `flown_first` / `flown_last`; with `--before DATE`, `buildings.gpkg` keeps only
  the buildings flown before it and `flown.gpkg` shows where. For a long run, freeze the code first (copy `src/`
  into the run's `code/` and run that `detect.py`), so edits to `src/` don't reach processes
  that start later.
- **Areas:**
  - `--bbox XMIN YMIN XMAX YMAX`: a grid over a box.
  - `--bboxes sites.csv`: one window per row (`xmin,ymin,xmax,ymax[,name]`).
  - `--list FILE`: a grid over a USGS download list. Only works with MGRS-style tile names.
  - `--all`: a grid over the whole EPT footprint. Empty areas are skipped using the
    EPT's root tree-index file (about 2 km cells), so no points are read to plan it.
  - `--bbox` together with `--list` or `--all` clips them.
  - `--bbox-crs` sets the CRS of the bboxes. The default is lon/lat (EPSG:4326).
- **Output CRS:** NAD83(2011) UTM for the zone at the centre of the area (the tiles' zone
  for `--list`). Falls back to WGS 84 UTM outside the continental US and Alaska.
  `--srs` overrides it.
- **Other options:** `--core` (window size, m), `--halo`, `--dims X,Y,Z,...` (fetch only
  some fields), `--resolution` (thinning), `--limit`, `--prefetch`.
- **`--analysis file.py`:** the file defines `analyze(points, window)` and optionally
  `HALO` and `CODES`. It returns `{name: result}`. Build rasters on `window.grid(res)`.
  A result is one of:
  - **a Raster:** cropped to the core;
  - **a table** (a dict of arrays) with `x`, `y`: rows outside the core are dropped,
    and merge joins the windows into `out/<name>.npz`;
  - **features:** a table that also has a `geometry` column (WKT, in the output CRS).
    It is saved as a GeoPackage per window. A row is kept by the window whose core holds
    its `x, y`, and its polygon is written whole, never cut at a window edge. `merge`
    joins the windows into `out/<name>.gpkg`;
  - **a summary:** a dict of scalars.
- **`CODES`** (or `run(..., codes=...)`): `{output: {column: {code: label}}}`. The labels
  are stored in the GeoPackage as a coded field domain, so the file says what each code
  means, and QGIS shows the labels. `merge` keeps them.
- **`--exec "CMD args"`:** runs once per window as `CMD args WINDOW.laz WORK_DIR`.
  - The conda env's `bin/` is first on PATH, so `pdal` and `gdal_*` match the env.
  - Environment variables: `TS_NAME`, `TS_SRS`, `TS_CORE`, `TS_HALO` ("xmin ymin xmax ymax").
  - Each output file `<name>.<ext>` becomes `out/<name>/<window>.<ext>`:
    - GeoTIFFs are cropped to the core. Use a grid aligned to multiples of the resolution.
    - CSVs with x,y columns lose rows outside the core.
    - `<name>.json` (an object of scalars) becomes a row of `out/<name>.csv` after merge.
  - A non-zero exit stops the run with that window not done; its partial output stays in `out/_work/`.
  - `--halo` is required with `--exec`.
- **Resuming:** finished windows are in `out/_done/`. A run's settings (EPT, CRS, dims,
  resolution) are saved in `out/_run.json`, and resuming with different settings is refused.
- **Empty windows** (no points) are recorded as done without running the analysis.

## Design notes
- **Tile names:** `src/tilestream.py` registers itself as `tilestream` when run as a
  script, so analyses that `import tilestream` get the same module (needed for the
  `isinstance(..., Raster)` checks).
- **LAZ offsets:** the temporary LAZ uses offsets on a fixed 1 km lattice and 1 mm
  scale. PDAL's `offset: auto` rounded the same point differently in each window and
  caused sub-mm seams. A test caught it.
- **Thinning:** `resolution=` is approximate. EPT picks tree levels, so you get about
  5–10× denser than the name suggests (today's 500 m test site: 28.3 pts/m² full, 3.9 at 2 m,
  0.19 at 8 m). The older figures (11.6, 2.6 and 0.08) came from the old test site at the
  data edge, which was probably partly empty.
- **Memory:** a 3×3 km window at full density (~110 M points) does not fit in RAM. The
  first attempt was OOM-killed and took VS Code with it. Use `resolution=` and `dims=`
  for big windows, and run tests under a memory cap.
- **Throughput** (earlier, 1 km windows): about 19 s fetch and 0.5 s analysis per window.
  The fetch is the bottleneck. Estimate for the 3,750-tile list: 16–20 h, resumable.
- **`--all` on WA_NorthEast_2_B22** gives 8,837 windows of 1 km. That's about 10% more
  than the 8,068 tiles, because the 2 km cells overshoot the data edge.

## Building detection (`src/buildings.py`)
A `--analysis` file for tilestream, with `HALO = 50` m: an object must be seen whole, and
under a building the ground is filled in from around it.

    $P src/tilestream.py run out/ --ept WA_NorthEast_2_B22 --analysis src/buildings.py \
        --bbox 446750 5347000 447250 5347500 --bbox-crs EPSG:6340 --core 250
    $P src/tilestream.py merge out/

- **Rasters** (0.5 m):
  - `dtm`, `dsm` and `ndsm`;
  - `ground_gap`: the distance to the nearest ground point; roofs leave holes with
    straight edges;
  - `multi_return`: the share of split pulses;
  - `roughness`: |Laplacian| of the dsm;
  - `segments`: 1 roof, 2 tree, 3 a building's roof found under a tree.
- **`segment()`:** a watershed on the ndsm, from two kinds of seeds:
  - **roof seeds:** the roof-like pieces of covered holes. A shed without such a piece
    is seeded with its whole hole. A hole that fails the roof test as a whole (because it
    runs on under trees) is seeded from its roof-like pieces of at least `ROOF_PIECE`
    (20 m²) only; without such a piece it gets no seed (added 2026-11-09).
  - **tree tops:** local maxima within 2.5 m.

  Patches without a seed get one at their highest pixel. Each piece of a roof seed floods
  with its own label (the flood is the same: label numbers don't change its order), and
  `merge_parts()` gives the floods back to their building, except a small piece's (not its
  building's largest, under `ROOF_PIECE`) whose flood has ≥ `SEED_TREE_MULTI` (0.24) split
  pulses: a smooth bit in a dense crown, whose 1 m keeps the tree top out, so its flood is
  the crown. That flood becomes a tree object (added 2026-11-10). Afterwards `reclaim_roof()`
  gives "tree" pixels that are really roof to the nearest roof:
  - whole "tree" objects touching a roof with fewer split pulses than `ROOF_MULTI` (0.15),
    or than `RECLAIM_MULTI` (0.2) from `RECLAIM_AREA` (25 m²) up: roof sections and roof
    edges where a tree top landed. Each goes in one piece to the roof it shares the longest
    border with (`longest_border`; on a tie, the roof that comes first in the image, so every
    window agrees), not pixel by pixel to the nearest roof (added 2026-11-09);
  - thin pieces touching a roof (eaves, ridges), found with an opening using a 1 m disk;
  - tree patches with roof all around (rooftop units).

  Then every tree object is made one piece again. The opening can cut an object in parts:
  - the part with its top keeps the label;
  - a cut-off part next to the (original) roof goes to the roof;
  - any other cut-off part becomes an object of its own.

  Before this fix, Chewelah had 11 crowns that were multipolygons.
- **`objects`** (table): one row per object, with these features:
  - `area`, `height`, `hole_depth`, `hole_share`, `rectangularity`, `multi_return`;
  - `roughness`, and `roughness_inside` (the same without the edge pixels: on a small
    object, half the pixels are edge, where the Laplacian sees the drop to the ground);
  - with `POINT_METRICS = True`, metrics from the object's points:
    - `n_points`;
    - `top`, `h_mean`, `h_std`, `h_p50`, `h_p95`: the returns above 2 m;
    - `cover`: the share of first returns above 2 m;
    - `i_mean`, `i_std`: the intensity of the first returns above 2 m;
    - `cover_min` (`cover_min()`, added 2026-11-09): the lowest `cover` of any one flight
      line (`PointSourceId`). A vehicle that came or went between two passes is there in
      one line and gone in another (cover ~0 there); a building is there in every line.
      Only lines that reach at least half of the object's pixels count
      (`COVER_LINE_SHARE`): a line that grazes the object sees only its edge.

    This needs `Intensity` and `PointSourceId`; tilestream reads every field unless `--dims`
    says otherwise.
    Heights are sorted per object before summing, so the result doesn't depend on point
    order, which can differ per window;
  - `building` (see below).
- **`footprints`** (GeoPackage): every roof-seeded object as a polygon, with the same
  columns plus `outline`, `review` and `under_canopy` (below). Rejects stay in the layer, so you can see them
  in QGIS. Outlines come from `gdal.Polygonize` (`trace()`), with holes under 1 m² filled
  (`HOLE_FILL`; in Chewelah 109 of 120 holes, single low pixels in a roof). Buildings
  (codes 1 and 2) get straight walls; everything else, and buildings the safety net turns
  down, is simplified (0.5 m), but only from 25 m² up.
- **Straightening, `straighten()`** (step 4d), per building:
  1. Each ring of the pixel outline is cut into walls with Douglas-Peucker (`WALL_TOL`
     0.75 m), starting at the point furthest from the centre (a real corner).
  2. One main direction per building: each wall is fitted as a line through the middle of
     its pixel staircase (the pixel edges as line pieces), then its angle × 4, weighted by
     length², averaged as arrows. `agree` (0–1) says how well the walls agree on it. In a
     second pass only the walls within 15° vote, so tree edges don't turn the building.
  3. A wall within `SNAP_ANGLE` (15°) of the main direction, or square to it, is turned
     onto it. A wall that doesn't snap and is shorter than `MIN_DIAGONAL` (4 m) is a ragged
     bit or a corner cut off by the pixels: it is dropped. Longer ones stay as diagonal walls.
  4. Neighbouring parallel walls less than 0.75 m apart become one. Neighbouring walls
     meet where their lines cross: square walls always, a diagonal only within `MAX_SHIFT`
     (1.5 m) of the old corner; otherwise, and for parallel walls, a jog.

  Rings under 25 m² (small holes, small parts) keep their pixel outline.
- **`outline` codes** (the safety net): 0 straightened; not straightened because 1 too small
  (< 25 m², pixel outline), 2 the walls don't agree (`agree` < 0.7: ragged, mostly
  tree-bitten), 3 the result wasn't a valid shape, 4 the area changed more than 10%,
  5 not a building. Codes 2–5 get the simplified outline.
- **`review`, `tree_notch()`:** 1 if a diagonal wall (unsnapped, ≥ 4 m) has mostly tree
  pixels just in front of it: a tree hangs over the roof and hides part of it. In front of a
  real wall is ground or another building. Checked against the DSM by Michael: it does "a
  reasonable job of telling us that we should review a building".
- **Roof under the canopy, `roof_under_canopy()`** (added 2026-11-09), after the rule, for
  buildings only. Where a tree hangs over a roof, part of its pulses get through, and their last
  returns lie on the roof's surface continued under the tree; beyond the roof's edge they reach
  the ground. A pixel of another object within 2 m (`CANOPY_REACH`) of a building is roof if:
  - its top isn't under the continued surface (a lower tree beside the wall isn't over it);
  - and one of its last returns lies within 0.15 m (`CANOPY_ON`) of the surface, and none more
    than 0.3 m (`CANOPY_TOL`) under it: that pulse went through where the roof would be;
  - or it has no last return on or under the surface, but at least 5 of its 8 neighbours are
    roof, one of them found under the tree (at 3–4 pulses a pixel, a third of the pixels get no
    pulse through).

  Each patch must show a surface's thin layer: at least 3× (`CANOPY_LAYER`) as many last returns
  on the surface as in the 0.3 m slab just above (a crown with returns at that height by chance
  has about as many), and connect to its building. The surface: a plane fitted to the dsm of the
  building's pixels within 1.5 m (`CANOPY_FIT`) of the nearest visible roof pixel (four rounds,
  dropping pixels > 0.3 m off; it must fit within 0.1 m, else no surface there: a ridge or step).
  The rule and every feature still judge the visible roof (`area` too), so no code changes. Only
  the footprint polygon gets the extra pixels, and `under_canopy` says how many m². Crowns keep
  their whole shape, so a crown overlaps the footprint where it hangs over the roof.
- **`crowns`** (GeoPackage): every object with `building` = 0 of at least 5 m²
  (`CROWN_MIN_AREA`), with `roof_seed` in place of `building`. `roof_seed` = 1 is a reject
  that is also in `footprints`; most of those are trees. Smaller tree objects are mostly loose
  bits just over 2 m: in Nine Mile 44% of the tree objects, but 1% of their area.
- **`flown`** (summary, `flown.csv` after merge; added 2026-11-10): each window's earliest and
  latest `GpsTime` (`gps_first`, `gps_last`), so every square says when it was flown.
  detect.py turns them into dates in `windows.gpkg` and the README.
- **NAIP check, `naip_green()`** (2026-11-10; run after the merge by detect.py's `--naip`, not
  part of `analyze`): `naip_green` = m² of a building's visible roof (`segments` = 1, inside its
  polygon) at least 1 m (`NAIP_EDGE`) from anything that isn't roof, where NAIP's NDVI > 0.25
  (`NAIP_GREEN_NDVI`) and green ≥ blue; `naip_flag` = 1 from 4 m² (`NAIP_FLAG_M2`). NULL for
  non-buildings and where NAIP has no data. Why each part: theoretical_basis.md §8 (a blue
  metal roof in Chewelah had NDVI > 0.25 over 1,100 m²; NAIP shifts crowns 1–3 m onto roof
  edges). Chewelah run 29 with NAIP 2023: 16 of 167 flagged (fid 61's leftover trees: 5.75 m²,
  flagged; fp 37's: 3.75, not); of the four greenest, two real (a tree or hedge as a building,
  tree parts in a roof), a green-painted roof, street trees leaning onto a white roof. Paradise
  test (NAIP 2018): 16 of 319; the three greenest all hold trees.
- **The rule, `is_building()`:** only roof-seeded objects can be buildings. Over the whole
  object, it needs all of:
  - split pulses < 0.35;
  - `roughness_inside` < 0.5 (< 0.3 for objects under 25 m²: sheds have simple roofs,
    bushes don't);
  - area ≥ 4 m²;
  - `cover_min` not below 0.2 (`VEHICLE_COVER`, the vehicle guard, added 2026-11-09). NaN,
    or no `cover_min` column, passes: it means no evidence.
- **`building` codes:** 0 = not a building, 1 = building, 2 = shed, container or RV (a
  building under 40 m²).
- **Test sites** (EPSG:6340, 500 m):
  - Chewelah, `446750 5347000 447250 5347500`: a town;
  - Nine Mile Falls, `457000 5291750 457500 5292250`: forest with houses.

  Checked by Michael in QGIS:
  - a rock at (457380, 5292240), which the rule rejects (rough inside);
  - a 7.5 m² shed at (457368, 5292215);
  - a tree crown that got a roof seed at (457059, 5291980), which the rule rejects.
- **Last results** (2026-11-05, run 20):
  - Chewelah: 181 footprints (114 buildings, 45 small structures, 22 not) and 372 crowns.
    Straightened: 103 of the 114 buildings (10 walls don't agree, 1 area) and 6 of the 45
    small structures (36 too small, 3 walls don't agree). `review`: 26. The straightened
    area differs from the pixel area by −0.1% (median; 80% within −2.2% and +1.7%). No
    invalid polygons; no footprints touch that didn't before.
  - Nine Mile: 6 footprints (3, 1, 2) and 2,618 crowns (median 46 m², 22.6 m high). The 3
    buildings are straightened; the shed is too small.
  - Four 250 m windows give exactly the same rasters, objects, footprints and crowns (all
    columns) as one 500 m window. This was checked by hand; there is no automatic
    real-data seam test for buildings.py yet.
  - Synthetic shapes turned 30° (rectangle, L, a diagonal wall, a 6 × 6 m garage, a cut
    corner) come back with each true corner within 0.15 m (in fact 2–8 cm) and their area
    within 2%. Simplifying made big diagonal outlines ~8% too big.
- **Run 21** (2026-11-09, Chewelah, with `cover_min`; default 1 km windows, so it covers
  2 km²): inside the site, the same 181 footprints and 372 crowns as run 20, with the same
  columns, plus `cover_min`. One exception: a ~4,600 m² building on the site's east edge,
  which run 20 cut off where its area ended (447,300). `cover_min`: at least 0.59 for
  buildings and 0.44 for small structures; 8 of the 372 crowns are under 0.2.
- **Run 22** (2026-11-09, both sites, 250 m windows as in run 20, with `ROOF_PIECE`):
  - Chewelah: 189 footprints (8 new, 1 changed, none gone) and 366 crowns. Checked by
    Michael over NAIP (`tmp_symlinks/new_footprints_run22.geojson`, column `what`):
    - 6 are buildings that were missed: fp16, fp19, fp21, fp120, fp123, fp151 (the 3
      unknown holes among them);
    - fp175 is building A in one piece, but unsure: its lower part may be a yard raised by a
      retaining wall (see Known limits);
    - fp29 (446778, 5347109) is a hedgerow.
  - fp16 includes a 31 m² roof-like piece under the labelled "tree pair". NAIP shows it isn't
    green (NDVI −0.17, like the house's −0.20), so it is roof. fp20 grew 39 → 40 m² and so went
    from code 2 to 1.
  - The "DSM suggests building" piece is a roof object now, but its point (the hole's deepest
    pixel, y = 5347503.8) is just past the site's north edge, so no window of this run keeps
    it. A larger run would.
  - Nine Mile: the same footprints; one crown changed a little.
- **Runs 23 and 24** (2026-11-09, both sites, 250 m windows), the merge limit:
  - Run 23 (`RECLAIM_MULTI`/`RECLAIM_AREA`, pixels still to the nearest roof): fp68 took in
    "edge of building" (+103 m²) and fp164 the piece at (447099, 5347393) (+44 m²; Michael:
    correct, its interior ring probably a pipe between roofs). Side effect: fp66, a 3.5 m²
    roof-seeded bit next to fp68 (code 0), got a loose 4.8 m² piece of "edge of building"
    that it doesn't even touch (0 m of shared border; fp68 shares 32 m), because those pixels
    were nearer to it. It became 8 m² and a "shed" (code 2). Michael: wrong, one part
    probably a tree crown, the other building edge.
  - Run 24 (`longest_border`): fp66 is back to 3.5 m² with code 0, and fp68 has the whole
    piece. fp59 and fp60, which touch, traded 1–2 m². Nothing else changed; Nine Mile
    unchanged in both runs.
  - fp89, a 19 m tree with a roof seed (code 0, rejected by the rule), took in a 38 m² piece
    of tree with 0.16 split pulses: `reclaim_roof` treats every roof-seeded object as a roof,
    rejected ones too. It stays code 0, so no building changes.
  - Seam check: the four 250 m windows against two 500 m windows give the same footprints
    and crowns (all columns), except the big building on the east edge, which the smaller
    run cuts off where its area ends.
  - Review files: `tmp_symlinks/changed_footprints_run23.geojson` (column `what`).
- **Runs 25–27** (2026-11-09, both sites, 250 m windows), the roof under the canopy (run 27 is
  the final version; 25 had a stricter "crown over it" test, 26 no layer test):
  - Chewelah: 49 of 167 buildings got roof under trees, 306 m² in all (median 4.75 m², the
    most 26 m²). No `building` code changed. `review` flags 32 → 13. Straightening: 8 buildings
    now straightened that weren't (`outline` 2 or 1 → 0), 3 worse: fid14 0 → 4 (it was already
    at 9.7% of the 10% area limit), fid120 0 → 2 and fid123 2 → 4 (two neighbours with a ragged
    1–3 pixel strip of fill). Nine Mile: 0.75 m² on one building, nothing else.
  - The 2026-11-04 examples: the notches at (447076, 5347170), (446834, 5347208) and
    (446786, 5347425) filled (10.75, 9.5, 8.5 m²) and lost their flag. The tree that stands on
    the ground at (447157, 5347292): nothing. The "dense crown" at (446960, 5347018), where the
    11-04 measurement (one plane per wall) found no layer, got 7.5 m²: per pixel there are 88
    last returns on the surface against 1 in the slab above, so there is roof under it.
  - The thin layer: over all the roof found, 3,101 last returns on the surface against 103 in
    the 0.3 m slab just above (a median of 26 to 1 per building); the layer test dropped only 2 m².
  - Seam check: four 250 m windows against two 500 m windows give the same footprints (all
    columns, `under_canopy` too), except the big building on the east edge, as before.
  - Review file: `tmp_symlinks/under_canopy_run27.geojson` (the 49 buildings, with `outline` and
    `review` of run 24 and 27, and an empty `what`); `chewelah_segments.vrt` shows the roof under
    trees as 3. Not checked by Michael yet.
- **Known limits:**
  - Vehicles, containers and dumpsters look like sheds from above (code 2). A vehicle that
    moved between flight lines gets `cover_min` ~0, but it rarely becomes a footprint anyway:
    the other lines saw the ground under it, so it leaves no covered hole and gets no roof
    seed. A vehicle parked through all passes looks like a shed, `cover_min` included.
    The rule's vehicle guard (`cover_min` < 0.2 is no building) is insurance: in Chewelah
    and Nine Mile no roof-seeded object is below 0.33, so it changes nothing there. It is
    meant for vehicles that still get a seed, e.g. where the other line's road points
    weren't classified as ground (post-fire Paradise will have many trucks and machines).
  - **Small buildings without a roof seed are still missed** (e.g. the 6.5 m² shed at
    446757, 5347286: `hole_share` 0.08). `cover_min` alone can't bring them in: of the 15
    small seedless candidates (≤ 21 m²) it removes the 5 moving vehicles (0.00), but the
    shed (0.54) is still 1 among 10: a parked car (0.50), a utility box (0.49), bushes
    (0.34–0.69), the log pile (0.34) and unknown things (0.29, 0.38).
  - `reclaim_roof` can absorb a crown hanging over a roof if its link to the outside is
    thinner than ~2.5 m. Example: the house at (446776, 5347492), fid 61. Since run 29 that
    crown is a tree again, but only because the seed rule freed the crowns next to it (the
    patch now touches them, so it no longer has roof all around); the thin-neck case itself
    is unchanged.
  - Thin things sticking out of a wall can become little roof spikes.
  - Simplified outlines (rejects, crowns, buildings the safety net turns down) of big
    diagonal shapes grow ~8%, because simplifying keeps the outer staircase corners.
  - **The roof under overhanging trees is found only where pulses get through** and only up
    to 2 m from the visible roof. Under a crown no pulse gets through, or further in, it is
    still missing: 13 notches keep their `review` flag in Chewelah. `review` only looks at
    diagonal walls of 4 m or more, so a notch with right angles, or one under 4 m, isn't flagged.
  - **The roof found under a tree is ragged** (pixel by pixel), so it can stop the walls from
    agreeing (fid120, fid123 in run 27). An opening would also remove the many fills that are
    one pixel wide.
  - Buildings under 25 m² keep their pixel staircase (their walls are too short to fit).
  - At 0.5 m pixels a pixel is roof if any of its ~4 pulses hits the roof, so outlines sit
    outside the roof edge: measured 0.15 m for straight walls (see "Measured on 2026-11-09"),
    ~6% area on a 10 × 10 m house. Lidar also sees the eaves, not the walls.
  - **Buildings without a roof seed were missed** and ended up in `crowns`, e.g. in
    Chewelah (447148, 5347158), (446796, 5347161), `missed_building`. The cause (found
    2026-11-09): `segment()` judged a covered hole only as a whole, and these holes run on
    under neighbouring trees (up to 432 m² for a 135 m² building). The whole hole then has
    0.18–0.29 split pulses (limit 0.15) and got no seed, although it holds roof-like pieces of
    30–95 m². Fixed with `ROOF_PIECE` (run 22). A building whose roof-like pieces are all
    under 20 m² is still missed.
  - **A dense, trimmed hedge looks like a flat roof** (fp29 in run 22: few split pulses,
    smooth, 3.4 m wide, 2.2 m deep, like the smaller buildings). Only NAIP shows it: NDVI
    0.34–0.40, while every other building over 60 m² in Chewelah is at 0.13 or less.
  - **A terrace on a steep slope may look like a flat roof** (perhaps fp175: a flat surface at
    518 m, 8 m above the street to its south and east, 2 m below the slope to its north). If
    the ground classification marks a terrace as "not ground", it leaves a covered hole like
    a roof. Lidar alone can't tell it from a building dug into the hillside.
  - A roof part touching a building with 0.2–0.35 split pulses (0.15–0.35 under 25 m²)
    stays a "crown". Why not looser (measured on run 20's crowns touching a building): 0.3
    from 25 m² up would merge 9 in Chewelah, of which 4 are clearly trees (8–14 m high, up to
    327 m²); 0.2 at every size would also take a 7.8 m², 10.7 m high piece of tree. Nine
    Mile: none either way, because its crowns have 0.5+. Chewelah's trees split much less
    (25% of its crowns ≥ 25 m² are under 0.32), so the risk depends on the forest. Height
    would be a better clue (a tree piece stands well above the roof), not used yet.
  - A rejected roof-seeded tree (code 0) can take in neighbouring pieces of tree in
    `reclaim_roof` (fp89 in run 23). Harmless for buildings.
  - A pile of logs is a footprint (Chewelah 142, ~(447110, 5347018), 158 m²): it looks like a
    roof on every feature. Left alone for now.
  - A few footprints have a one-pixel part that touches the roof only at a corner.
  - Neighbouring crowns are simplified one by one, so their shared edges can differ a
    little.
- **Decisions:**
  - The minimum building area stays 4 m². 8 m² was considered, to drop vehicles and
    dumpsters, but the checked shed is 7.5 m².
  - Crowns are not split into too many pieces. In Nine Mile, only 0–1% of neighbouring
    crowns have a saddle less than 1 m deep. The mean-filter idea (smooth the crown pixels,
    then a second watershed) was set aside: tried at 0.5–1.5 m, it barely changed anything.
  - **The rule applies only to roof-seeded objects.** Applying it to all objects ("option
    a") was tried and rejected: in Chewelah it added 26 footprints, many of them trees.
  - **Straightening keeps diagonal walls** (not right angles only), and flags tree notches
    with `review` instead of filling them in. Only buildings (codes 1 and 2) are
    straightened. The main direction comes from the walls, not from the smallest rectangle
    around the building: that rectangle is pulled off by bumps and tree bites (11° on a big
    building in Chewelah).
  - **`RES` stays 0.5 m**, even where the data are denser. At 0.25 m there is about one
    pulse per pixel (a third of the pixels empty), and every threshold (roughness, `THIN`,
    `multi_return`, `MIN_AREA`) would need retuning. Better: refine the walls with the
    points (Next steps).
- **Measured on 2026-11-04** (for later steps):
  - Density, full-density reads: Chewelah 15.1 pulses/m² (16.2 returns), Nine Mile 12.0
    (19.3 returns): ~3–4 pulses per 0.5 m pixel. As far as we know the EPT holds the full
    OPR point cloud; not checked yet.
  - **The roof under the canopy is often visible.** Under 6 flagged tree notches and 2
    trees beside a wall, 20–55% of the pulses reach the roof or the ground. Where a roof is
    under the crown, the last returns form a thin layer exactly on the roof facet's plane,
    continued under the tree: at (447076, 5347170), 120 last returns within ±0.15 m of it
    and none in the 0.3 m slabs just above and below; at (446834, 5347208), 48 against 10
    and 0. Not everywhere: under the dense crown at (446960, 5347018) there is no such
    layer (but per pixel there is: see runs 25–27), and at (447157, 5347292) the tree stands mostly on ground (no notch after all).
    The two trees beside a wall had a layer too (98 and 52): they also hang over the roof
    edge, so there the footprint ends where the tree starts. Method: fit a plane to the
    roof's first returns within 1.5 m of the wall (four rounds, each dropping the points more
    than 0.3 m off), continue it under the tree pixels within 2 m of the wall, and count the
    last returns there by their height above it.
- **Measured and labelled on 2026-11-06 to 11-09:**
  - **Seedless candidates, labelled by Michael:** the 24 crowns of run 20 that have no roof
    seed but pass the rule ("option a" again), in `tmp_symlinks/seedless_candidates_chewelah.geojson`
    (column `what`; local only, since `tmp_symlinks/` isn't in git). The big ones (≥ 47 m²):
    8 building pieces with 0.06–0.17 split pulses, and 1 tree pair with 0.26. The small ones
    (≤ 21 m²): 6 vehicles, bushes, a utility box, part of a log pile, unknown man-made
    things, and one small building or shed (446757, 5347286; 6.5 m²).
  - **Chewelah was flown in 4 lines:** `PointSourceId` 356, 357 and 358 on 2022-07-16
    (20:36, 20:41, 21:00 UTC), and 400 on 2022-07-19. Each moving vehicle is there in one
    line only (cover 0.70–0.91 there, 0.00 in the others); one came and went within that
    evening. Every footprint has cover ≥ 0.44 in every line. A parked car that stayed has
    ≥ 0.50.
  - **Why the building pieces have no seed:** see Known limits. Judging each hole by its
    roof-like pieces instead: of the 2,297 holes in Chewelah without a seed, only 8 have a
    roof-like piece of ≥ 20 m². 5 are labelled buildings; 3 were unknown, at (447149, 5347185),
    (446778, 5347165) and (446778, 5347099). In run 22, Michael found the first two to be
    buildings and the third a hedgerow. The "DSM suggests building" piece lies just past the
    site edge.
  - **NAIP** for Chewelah, the site plus 50 m: `tmp_symlinks/naip/naip_chewelah_2021.tif`
    (2021-07-10) and `..._2023.tif` (2023-07-01). Washington has no 2022 NAIP. They are
    0.6 m, 4 bands (R, G, B, near-infrared; bands 4-1-2 show vegetation in red), cut from
    tile `m_4811743_sw_11_060` on Microsoft Planetary Computer without resampling, in EPSG:26911.
  - The scripts are in the session scratchpad (`vehicle_clue.py`, `why_no_seed.py`,
    `holes_count.py`), which is temporary.
- **Measured on 2026-11-09** (refining the walls, step 1; buildings.py unchanged):
  - Method (`walls_step1.py` in the scratchpad; `tmp_symlinks/walls_step1_chewelah.gpkg`: layers
    `walls_old`, `walls_new`, `wall_points`): for each straight wall of ≥ 3 m of run 24, fit the
    roof surface to the first returns 0.25–1.5 m inside it (each 0.5 m slice its own height, one
    slope across: that also fits gable ends, where the roof rises along the wall to the ridge;
    one plane for the whole wall failed on 287 walls). Roof points: within 0.3 m of it. In each
    0.5 m slice along the wall, the roof point furthest out; the edge is the median of those plus
    ln 2 / (ρ · 0.5 m), the median gap between the last point and the edge at ρ roof points per m².
  - Of 858 walls, 559 (6.4 km) qualify: shift median **−0.15 m** (inward), middle half −0.20 to
    −0.10, 95% between −0.32 and −0.02; about ±3 cm per wall; 14 roof points per m². The others:
    poor fit (138, mostly big flat roofs, perhaps parapets), diagonal (52), few points (44), a tree
    or high points in front (52), into a neighbour (6), shift over 0.5 m (7).
  - **Michael's rule: a wall may not move out into a neighbour** (2026-11-09: fp33 wall 4 would
    overlap fp25). It caught 6 walls: five against a building next door at the same height,
    whose roof points pushed the edge out ~1 m, and fp140 wall 2 (+0.18 m into fp131), which only
    this rule catches.
  - Against the ground side: the innermost ground point lies a median 0.16 m inside the roof's
    edge (slanted pulses under the eaves), so the edge is placed from the roof side only. Not
    measured: a beam that just catches the edge may put a roof point a little outside it.
  - Roof under the canopy: see runs 25–27. Scripts: `canopy_compare.py`, `canopy_map.py`
    (a character map of the evidence per pixel), `canopy_layer.py`.

## Camp Fire run (2026-11-10)
Michael's request, to pass on to someone: building footprints from before the fire. A first,
automatic result, a test case to refine later. Everything is in `runs/camp_fire_prefire/`
(git-ignored); its `README.md` has the exact command, the sources and the columns.
- **Inputs:** the NIFC perimeter (CAL FIRE, 153,336 acres) in `inputs/`; B4 2018 (QL2) and
  PlumasNF B2 2018 (QL1). 2,801 squares of 500 m touch the perimeter: B4 has data in 814,
  B2 in 1,722, 2,378 have points. NAIP 2018 (flown 2018-07-16 to 07-21): 35 tiles, about
  15 GB, in `tmp_symlinks/camp_fire_inputs/naip_2018/` (`naip_2018.vrt`). Planetary
  Computer's token expires after about an hour; `naip_fetch.py` (scratchpad) skips the
  tiles it has, so rerun it.
- **Run:** the squares ran 16:11–18:55 with 12 processes, from a frozen copy of the code
  (`code/`). Each later run of the same command redoes only combine (~15 min), the date
  maps (~6 min, kept in `flown/`), NAIP (~15 min), the README and the zip.
- **B4's dates (found in this run):** B4 was flown before the fire (2018-07-07) only in a
  strip through central and eastern Paradise; west of it on 2018-12-29, 2019-02-21 and
  2019-08-09. Of the 620 km², 423 (68%) have pre-fire lidar, 99 (16%) only post-fire (B4's
  west), 98 (16%) none (the southwest edge). Hence `--before` and `flown.gpkg`;
  `camp_lidar_memo.md` is corrected.
- **Results:** 28,513 buildings; **24,440 flown before the fire** in `buildings.gpkg` (15,862
  code 1, 8,578 code 2; 11,490 from B4, 12,910 from B2, 40 joined across the seam). The
  others stay in `footprints.gpkg`: 4,036 flown after the fire (survivors, western Paradise)
  and 37 on the edge between two B4 flights. The kept buildings were all flown 2018-07-07 to
  07-18. Median roof 83 m² (5th percentile 5.5, 95th 360). Crowns: 4.98 M (4.6 GB).
- **NAIP flag:** 21% flagged, 2–8% in central Paradise, up to 67% in east Paradise. There,
  shifting NAIP 3 m west takes two-thirds of the green off the roofs: the tall pines' lean,
  not trees in roofs. The README says so; aligning NAIP to the lidar's crowns per block is a
  next step.
- **To pass on:** `share/camp_fire_prefire.zip` (26 MB): README, buildings, footprints,
  windows, flown and inputs.
- **False buildings found:** bridges and power-line wires over the canyons and reservoirs
  (smooth, high, no ground under them). The Pulga railway bridge (631109, 4397408) is a code 1
  building. 105 code 2 objects stand more than 12 m up (most are wire pieces, e.g. 31 over the
  reservoir at 628800, 4393050, 72-83 m above the water; 74 are above 20 m). The README gives
  the count and a filter (`NOT (building = 2 AND height > 12)`); no rule in the code yet. A
  rule could use height for code 2 and the wire class or water under an object.
- **Checked:** the dates; the counts add up; a central Paradise box flags the same buildings
  as the earlier test; pictures over NAIP in central and east Paradise, of the reservoir and
  the bridge. **Not checked:** buildings one by one, the other code 2 objects in the forest
  (35% of the buildings; many are small), the thresholds on QL1 B2.

## Tests
Run from the project root, inside a memory cap:

    PYTHONNOUSERSITE=1 systemd-run --user --scope -p MemoryMax=6G -p MemorySwapMax=0 \
        ~/miniforge3/envs/tilestream/bin/python -m pytest --ept WA_NorthEast_2_B22   # all, ~2 min
    ... -m pytest                                                                     # offline only, ~10 s

`test/README.md` explains every test, and how to check that a new test fails without its fix.

**CI** (GitHub Actions, `.github/workflows/network-tests.yml`, added 2026-11-05): on every
push, and by hand from the Actions tab, GitHub builds the env from `environment.yml` (cached
until that file changes) and runs the network tests only
(`pytest -m network --ept WA_NorthEast_2_B22`). A new push to the same branch cancels a run
that is still going. The offline tests don't run in CI yet. The first run (2026-11-05) passed.

- **`test/test_tilestream.py`** (offline, 75 tests): synthetic points stand in for the
  EPT. The synthetic data includes a slope, location-based noise points and a hole.
  The buildings.py tests use small hand-made arrays and real values from the test sites.
  - window planning and exactly-once coverage
  - UTM zone choice
  - `plan()` for each kind of area
  - cropping rasters, GeoTIFFs and CSVs
  - run, merge, resume, settings guard, empty-window skipping
  - GeoPackage output and codes, through merge
  - buildings.py: segmentation, `reclaim_roof` (each step, and objects in one piece),
    outlines and hole filling, straightening (turned shapes, neighbours 1 m apart, the
    safety net), the tree-notch flag, `roughness_inside`, point metrics and `cover_min`
    (independent of point order), the rule and its vehicle guard, the codes, the roof under
    the canopy, and the whole `analyze()` on two synthetic scenes (which layer gets what, and a
    tree over a house's corner)
  - the `--exec` contract (arguments, environment variables, PATH, output handling, failure)
  - seam tests for both the Python analysis and `example_dsm.sh`
- **`test/test_ept.py`** (marked `network`, skipped without `--ept`): no hardcoded
  location. The `site` fixture in `conftest.py` picks a 500 m test square inside whatever
  EPT is given. It tries the tree-index cells nearest the middle of the footprint, does a
  thinned read of each, and takes the most rugged square whose four quarters are all well
  filled. The tests cover:
  - fetch, `dims=` and thinning
  - a 3 km window at `resolution=8`
  - real-data seam tests for the Python analysis and `--exec`
  - the CLI with lon/lat sites
  - `--all` planning

## Environment
- Conda env `tilestream` at `~/miniforge3/envs/tilestream`, recreated on 2026-10-30 with:

      mamba create -n tilestream -c conda-forge --override-channels python=3.12 pdal \
          python-pdal gdal numpy scipy scikit-image pyproj laspy lazrs-python pytest

  Versions: python 3.12, PDAL 2.10.2, python-pdal 3.5.5, GDAL 3.13.3, numpy 2.5.4,
  scipy 1.18.1, scikit-image 0.26.0 (added 2026-11-02, for the watershed in buildings.py),
  pyproj 3.8.0, laspy 2.7.0 (with lazrs, for validation only), pytest 9.2.
- **`environment.yml`** pins those same versions (conda-forge only), for CI and for a new
  machine: `mamba env create -f environment.yml`. When you change the local env, change
  this file too, so CI keeps testing what runs locally.
- **`PYTHONNOUSERSITE=1` is needed.** `~/.local/lib/python3.12/site-packages` holds old
  `pip install --user` packages (pandas 2.3.3, scipy 1.16.2, geopandas, shapely, sklearn
  and more, from 2025-10-02). Every Python 3.12 searches that folder before its own
  packages, so without the variable:
  - `import pdal` fails, because python-pdal imports that pandas and it is missing pytz
    and dateutil;
  - analyses would silently get the old scipy instead of the env's.

  The variable is inherited by subprocesses (the CLI tests, `--exec` scripts). A
  permanent fix is still to be decided: move that folder away if nothing uses it, or
  keep exporting the variable.
- `tilestream.py` sets PROJ_DATA/GDAL_DATA itself, so the env's python works without
  `conda activate`. A script that uses GDAL's coordinate systems without importing
  tilestream needs `PROJ_DATA=~/miniforge3/envs/tilestream/share/proj` (else "PROJ:
  proj_create_from_database: Open of .../share/proj failed").
- PDAL prints harmless "(PDAL Error) Can't load library ... libhdf5_cpp.so.320" errors
  (the HDF reader plugin) with no trailing newline. They can merge with the next line of
  output when you filter logs.

## Files
- `src/tilestream.py`: the framework and CLI (info, plan, run, merge).
- `src/example_peaks.py`: example Python analysis (1 m DSM plus local maxima).
- `src/example_dsm.sh`: example `--exec` analysis (1 m DSM with the `pdal` command-line tool).
- `src/buildings.py`: building detection (an `--analysis` file; see "Building detection").
- `src/detect.py`: buildings over a whole area in one command (see "How to use it").
- `runs/` (git-ignored): detect.py runs. `runs/camp_fire_prefire/`: the Camp Fire pre-fire
  buildings (`buildings.gpkg`), with `README.md`, `inputs/` (the perimeter, the NAIP tile list),
  `code/` (the code the squares ran with), `flown/` and `flown.gpkg` (the flight dates) and
  `share/camp_fire_prefire.zip` (what to pass on).
- `tmp_symlinks/camp_fire_inputs/` (git-ignored): the perimeter as downloaded, NAIP 2018
  (`naip_2018/`, 35 tiles as they are, `tiles.json`, `naip_2018.vrt`), the STAC search results.
- `test/conftest.py`: the `--ept` option and the `site` fixture.
- `test/test_tilestream.py`, `test/test_ept.py`: the tests.
- `test/README.md`: what each test checks.
- `pyproject.toml`: pytest config (pythonpath `src`, `network` marker).
- `environment.yml`: the conda env, with pinned versions.
- `.github/workflows/network-tests.yml`: CI, the network tests on every push.
- `downloadlist_WA_NorthEast_2_B22.txt`: the original USGS list. Optional now; use it with `--list`.
- `camp_lidar_memo.md`: Michael's notes on the Camp Fire lidar (datasets, EPT links, caveats).
- `theoretical_basis.md`: how each step works and why (with mermaid diagrams); keep it in step with the code.
  A web page made from it (one self-contained HTML file, kept by hand in step with the .md) is
  published privately on claude.ai: https://claude.ai/artifact/A479foxZU67XFnEWae5gKx (last
  updated 2026-11-10: merge_parts, detect.py, NAIP, flight dates). Michael would like it on GitHub Pages later.

## Next steps
1. **Camp Fire:** Michael looks at `runs/camp_fire_prefire` (see "Camp Fire run": start with
   the code 2 objects in the forest, `review` = 1, and the squares where the two sets meet:
   `lidar` with a "+"). Open: western Paradise has no pre-fire lidar. Other sources for its
   buildings? (Ideas, not checked: CAL FIRE's damage inspection (DINS) points of the
   structures the fire reached; building footprints made from imagery.) Then the NAIP
   alignment (below), perhaps the post-fire set (`USGS_LPC_CA_NoCal_Wildfires_CampFire_2018`,
   QL1, 2019) for what burned, and the recovery sets (2021). Done 2026-11-10: the density and
   dates, the run, the flight dates.
   - **NAIP alignment:** before counting green, shift NAIP per block (e.g. 250 m) to where
     its NDVI best matches the lidar's crowns (nDSM), so tall trees no longer lean onto
     roofs. Measured: 3 m in east Paradise (see "Camp Fire run").
   - **Bridges and wires:** a rule against them (see "Camp Fire run"), e.g. a height limit for
     code 2 and a look at the classes under an object (water, wire).
2. Later for buildings:
   - **NAIP as a review column: done 2026-11-10 as `naip_green` / `naip_flag`** (see Building
     detection; detect.py `--naip`). The plan it came from (agreed with Michael 2026-11-09), e.g. `green_share`: the
     share of pixels with NDVI above ~0.25 inside the footprint shrunk by 1–2 m, from the
     NAIP nearest in time. Only from ~25 m², as a flag (like `review`), not a verdict, and
     optional, so buildings.py still works without it. Why: colour catches what the shape
     can't (hedges, smooth trees, green yards). Caveats: NAIP is orthorectified to the
     ground, so roofs and trees lean (a 5 m roof shifts 1–2 m near a frame's edge, a 20 m
     tree 5–8 m), and its position is only good to a few metres; a different date; shadows;
     colours differ between years (2023 is much brighter than 2021). Test it first on the
     labelled objects (the 24 candidates and the 9 footprints of run 22). Lidar intensity
     alone doesn't separate the hedge (fp120, a building, is brighter).
     **Measured 2026-11-10** on Michael's labels (`reclaim_survey.gpkg`, 12 trees, 80 not):
     2023 NDVI flags 10 of 12 trees and none of the 80 (median > 0.25; trees ≥ 0.29, others
     ≤ 0.21); 2021 only 3. The lidar was flown 2022-07-16 to 07-19 (GpsTime). The lean, over
     all Chewelah crowns (`naip_shift.py`): 2021 east, 1 m for 2–6 m crowns, 2 m for 6–12 m,
     3 m above; 2023 west, 1.5–2.5 m. So a small patch on a roof edge can lose its green:
     id 85 (fp 121, a 4.8 m crown) is green on only 39% of its pixels in 2023 and its median
     is roof (`tmp_symlinks/seed85.png`, script `seed_pic.py`). Use the green share, not the
     median: > ~0.35 flags 11 of 12 and none of the 80 (highest 0.29, a narrow gap). Next:
     test a footprint-level green measure on run 29 before writing code, since trees beside a
     house will lean their green onto its roof.
     The same check could clean the **crowns**: Michael found (2026-11-10) that Chewelah run
     27 crowns fid 4 (446851.25 5347246.75) and fid 186 (446860.25 5347263.25) are railroad
     crossing signals, not trees. Both are 11–13 m² and ~6.7 m tall; compared with the 362
     crowns they have low `cover` (0.52, 0.56; p10 0.64) and a high `i_std` (6860, 10639;
     p90 5968): open metal frames. Only two examples, so no rule yet; NDVI should show them
     as not green.
   - **Refine the walls with the points.** Step 1 (measuring) is done, see "Measured on
     2026-11-09": walls move in 0.15 m on average, ±3 cm each. Step 2 is done (2026-11-10):
     `wall_edge()` in buildings.py, not used yet, with `test_wall_edge`. It holds the checks
     that need only the points ("few points", "poor fit", "high points in front"). On the 858
     walls of run 24 it agrees with step 1: on the 559 eligible walls a median −0.003 m
     different (90% within 2 cm; up to 0.10 m on short walls with ~5 points/m², where the
     ln 2 / (ρs) term is large), and every step-1 reason maps onto its own. (Script
     `wall_edge_vs_step1.py` in the scratchpad.) Step 3 (2026-11-10), agreed with Michael:
     3a `refine_walls()` moves a straightened building's walls (corners where the moved lines
     cross; an invalid result keeps the straightened walls), with `test_refine_walls`. 3b
     `move_wall()`, refine_walls's shift, adds the checks that need the polygon or the rasters
     (square walls only, at most `EDGE_MAX` 0.5 m, no tree pixels in front, no moving out into
     another roof-seeded object's pixel outline: Michael's rule), and `analyze()` uses it
     (a cKDTree over the window's first returns); new footprint columns `walls_moved` and
     `wall_shift`, for checking. Tests `test_move_wall`, `test_analyze_moves_walls` (a house at
     random points: pixel outline over 124 m², moved to 120 ± 2). 3c, run 28 (scratchpad
     `chewelah28`, `ninemile28`, `chewelah28_500`; compared with run 27 by `walls_compare.py`):
     only the geometry changed, and only where walls moved. Chewelah: of 117 straightened
     buildings, 115 had walls moved (592 walls); the median shift per building is −0.157 m
     (p5 −0.235, p95 −0.088), polygon areas −3.0% (median −6.8 m²); pixel area / polygon
     area went from 0.999 to 1.027. 1128 m² moved in and 17 m² out; 12 buildings had over
     0.5 m² move out, most by 1 m² or less against 3–50 m² in. The exception is 447183.25
     5347208.75 (3.6 m² out, 3.0 in): worth a look in QGIS. Nine Mile: 3 buildings,
     10 walls, −0.12 m, −3.6%, nothing out. Seam check (500 m windows): all identical
     except 447239.75 5347431.25, which the 250 m run cuts at x 447300 (the data's edge; run
     27 too). Analysis ~4–5 s per 250 m window, as before (the fetch is the slow part).
     Two GDAL warnings in the logs: "Self-intersection" is GEOS reporting a moved shape
     that `refine_walls` then rejects (outside the site there); "OGR_G_Area() called against
     non-surface geometry" came from a strip touching a neighbour at a corner (a point), and
     `move_wall` now measures only area intersections. Step 4: theoretical_basis.md §7
     describes moving the walls (2026-11-10), and the web page is updated. On the synthetic 0.25 m lattice (`scene_points`), the walls
     move 4 cm in although the pixel outline is exact: the ln 2 / (ρs) term assumes random
     points; on a regular pattern it is off by 2–4 cm either way.
     Michael still wanted to look at fp114, fp94, fp111 and fp40 in the GeoPackage.
     At QL2 densities (2/m², Paradise pre-fire) `wall_edge` mostly says "few points", and the
     walls it does measure scatter ±0.15 m (synthetic): it would need a density limit there.
     Since run 27, walls next to roof found under trees have no roof first returns there:
     they are "tree in front" and keep their place.
   - **Roof under the canopy: Michael checked run 27** (`tmp_symlinks/under_canopy_run27.geojson`).
     Then perhaps: smooth the ragged fills without losing the 1-pixel strips, and look past 2 m
     under big crowns. First findings (2026-11-10): fid 182 and fid 61 each hold a tree, but
     neither comes from roof_under_canopy (both were in run 24 already; scratchpad
     `canopy_check.py`, `reclaim_check.py`, pictures `tmp_symlinks/canopy_fid*.png`):
     - fid 61, tree 467 over the middle of the roof (20.5 m², top 4.3 m above it): reclaim_roof's
       thin pieces cut its 1–1.5 m link to the ground, and the rest, with roof all around, was
       taken as a rooftop unit.
     - fid 182's upper-left corner (17 m², split share 0.38, roughness 1.85; the roof 0.05,
       0.07): segment() seeded the tree itself, from a 4-pixel smooth bit (exactly `MIN_AREA`,
       1 m²). Every roof-like piece of a hole gets the building's label, and no tree top may
       lie within 1 m of a roof seed, so the roof flooded the whole tree.
     - NAIP NDVI on these two: 2021 misses both (the corner −0.17), 2023 catches both (+0.37,
       +0.47) but also calls 34% of fid 61's own roof green. So agreed: fix with the lidar,
       NDVI stays a review flag (see the NAIP item above).
     - **Survey for Michael to label** (`tmp_symlinks/reclaim_survey.gpkg`, a real file; script
       `reclaim_survey.py` in the scratchpad, which won't overwrite labels): layer
       `roof_all_around` (13 Chewelah patches taken because roof surrounds them; only fid 61's
       is green, and it alone has last returns > 1 m under the roof, `to_ground` 0.17) and
       `small_seeds` (84 smaller seed pieces in a building with a bigger one, with what each
       one's flood took; `hint` "roof-like" for 38 with split share < 0.15 and roughness
       < 0.5). Columns: split share, roughness, height above the roof around it, `through`
       (last returns within 0.3 m of that roof), `to_ground` (more than 1 m under it), NDVI
       2021/2023. Michael fills `what` (tree / unit / roof / …); the rules come from that.
     - **Labelled by Michael (2026-11-10), all 97.** `roof_all_around`: 12 rooftop boxes and
       fid 61's tree; no lidar rule separates one tree (judging "enclosed" before the thin cut
       also throws out 3 boxes), so that step stays. `small_seeds`: 11 trees/shrubs, 3 mixed,
       1 unclear, the rest roof parts, porches, pads, HVAC; none of the 38 "roof-like" is a
       tree. The seed's size doesn't separate them (trees from 1–4.5 m² seeds, roof parts as
       small); the flood's split share does: trees 0.26–0.33, roof parts ≤ 0.22, and id 54
       (fp 164, "edge of roof, perhaps with shrubs below") at 0.26. So `SEED_TREE_MULTI` = 0.24.
       Misses: ids 14, 21 (fp 61), 94 (fp 37), all green in 2023.
     - **Run 29** (scratchpad `chewelah29`; `seed_rule_check.py`, `seed_compare.py`): with the
       rule off, `segment()` gives the committed labels exactly (Chewelah and Nine Mile).
       Nine Mile: no change. Chewelah: 11 of 189 footprints change, no building is lost or
       gained; −344 m² of `area` on buildings. fp 90 −126 (a 127 m² tree, 8 m over the roof),
       fp 61 −61 (two seeded trees, and tree 467: see the thin-neck limitation), fp 182 −48
       (Michael's corner), fp 164 −34.5 (id 54; `under_canopy` 2.75 → 13.75 and `review`
       0 → 1: worth a look), fp 126 −33.5, fp 121 −27 (id 85), fp 71 −15.5 (survey id 33,
       listed there as fp 70). `review` went 1 → 0 on fp 90 and fp 126. All 12 rooftop boxes
       stay in their footprints. fp 172 (`building` 0, so not in the survey) gives 7.5 m².
     - Agreed (2026-11-10): the lidar rule (done) **and** the NDVI review flag (next; see the
       NAIP item above).
   - Check that the EPT holds the full OPR point cloud: count one OPR LAZ tile's points in
     Chewelah against an EPT read of the same box.
   - a real-data seam test for buildings.py (a network test);
   - the `reclaim_roof` thin-neck case.
   - Point metrics are on by a constant (`POINT_METRICS`). In a final command-line version
     they might become an option; tilestream has no way yet to pass options to an analysis.
3. Decide the permanent fix for the `~/.local` packages (see Environment).
   Also later: put the theoretical_basis web page on GitHub Pages (e.g. `docs/index.html`;
   the HTML is one file with no outside parts, so the repo's copy must stay in step with
   the .md).
4. Scale up: a modest real run with buildings.py (a few km²). Check the merged outputs,
   then go larger (`--list` or `--all`, in chunks or overnight). Measure memory first for
   larger windows.
