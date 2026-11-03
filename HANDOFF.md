# Handoff: tilestream, windowed lidar processing from any EPT

_Last updated 2026-11-03 (Claude, after step 4 of building detection: footprints, the
building rule and its codes)._

## Goal
A reusable point-cloud querying layer. Give it an EPT (any USGS 3DEP project in
usgs-lidar-public, or any ept.json URL) and an area. It streams windows of points,
each with a halo, to any analysis as they arrive, with no edge effects and nothing big
on disk. Analyses plug in as Python (`--analysis file.py`) or as any command or shell
script (`--exec ./my_tool.sh`). **Buildings are not classified in WA_NorthEast_2_B22**
(only classes 1, 2, 7, 18), so we detect them ourselves, in a separate analysis:
`src/buildings.py` (see "Building detection"). tilestream stays the reading layer; no
building logic goes into it.

## Status (2026-11-03)
- **Offline suite: 51 passed** (10 network tests skipped), in about 11 s.
- **The network suite was last run on 2026-10-30** (49 of 49). Since then `fetch_points`
  got the empty-area fix (`test_fetch_empty_area_keeps_fields`), and tilestream got the
  GeoPackage output. Run the full suite once (see Tests).
- **tilestream, new since then:**
  - an analysis can return polygons (features), which become GeoPackages;
  - `codes` / `CODES`: the meaning of coded columns is stored inside the GeoPackage.
- **buildings.py:** steps 1–4c are done. It writes footprint polygons with a `building`
  code. See "Building detection".
- Last commit before this work: 26abb17, pushed to https://github.com/kulpojke/detection.
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
  - `segments`: 1 roof, 2 tree.
- **`segment()`:** a watershed on the ndsm, from two kinds of seeds:
  - **roof seeds:** the roof-like pieces of covered holes. A shed without such a piece
    is seeded with its whole hole.
  - **tree tops:** local maxima within 2.5 m.

  Patches without a seed get one at their highest pixel. Afterwards `reclaim_roof()`
  gives "tree" pixels that are really roof to the nearest roof:
  - thin pieces touching a roof (eaves, ridges), found with an opening using a 1 m disk;
  - tree patches with roof all around (rooftop units).
- **`objects`** (table): one row per object, with these features:
  - `area`, `height`, `hole_depth`, `hole_share`, `rectangularity`, `multi_return`;
  - `roughness`, and `roughness_inside` (the same without the edge pixels: on a small
    object, half the pixels are edge, where the Laplacian sees the drop to the ground);
  - `building` (see below).
- **`footprints`** (GeoPackage): every roof-seeded object as a polygon, with the same
  columns. Rejects stay in the layer, so you can see them in QGIS. Outlines come from
  `gdal.Polygonize` and are simplified (0.5 m) only for objects of at least 25 m².
- **The rule, `is_building()`:** over the whole object, it needs all of:
  - split pulses < 0.35;
  - `roughness_inside` < 0.5 (< 0.3 for objects under 25 m²: sheds have simple roofs,
    bushes don't);
  - area ≥ 4 m².
- **`building` codes:** 0 = not a building, 1 = building, 2 = shed, container or RV (a
  building under 40 m²).
- **Test sites** (EPSG:6340, 500 m):
  - Chewelah, `446750 5347000 447250 5347500`: a town;
  - Nine Mile Falls, `457000 5291750 457500 5292250`: forest with houses.

  Checked by Michael in QGIS:
  - a rock at (457380, 5292240), which the rule rejects (rough inside);
  - a 7.5 m² shed at (457368, 5292215);
  - a tree crown that got a roof seed at (457059, 5291980), which the rule rejects.
- **Last results** (2026-11-03):
  - Chewelah: 181 footprints (114 buildings, 45 small structures, 22 not).
  - Nine Mile: 6 footprints (3, 1, 2).
  - Four 250 m windows give exactly the same rasters, objects and footprints as one
    500 m window. This was checked by hand; there is no automatic real-data seam test
    for buildings.py yet.
- **Known limits:**
  - Vehicles, containers and dumpsters look like sheds from above (code 2).
  - `reclaim_roof` can absorb a crown hanging over a roof if its link to the outside is
    thinner than ~2.5 m. Example: the house at (446776, 5347492).
  - Thin things sticking out of a wall can become little roof spikes.
  - Big diagonal outlines grow ~8%, because simplifying keeps the outer staircase corners.
  - Big tree crowns are split into several pieces.
  - A building without a roof seed is missed (`missed_building`).
- **Decisions:**
  - The minimum building area stays 4 m². 8 m² was considered, to drop vehicles and
    dumpsters, but the checked shed is 7.5 m².

## Tests
Run from the project root, inside a memory cap:

    PYTHONNOUSERSITE=1 systemd-run --user --scope -p MemoryMax=6G -p MemorySwapMax=0 \
        ~/miniforge3/envs/tilestream/bin/python -m pytest --ept WA_NorthEast_2_B22   # all, ~2 min
    ... -m pytest                                                                     # offline only, ~10 s

`test/README.md` explains every test, and how to check that a new test fails without its fix.

- **`test/test_tilestream.py`** (offline, 51 tests): synthetic points stand in for the
  EPT. The synthetic data includes a slope, location-based noise points and a hole.
  The buildings.py tests use small hand-made arrays and real values from the test sites.
  - window planning and exactly-once coverage
  - UTM zone choice
  - `plan()` for each kind of area
  - cropping rasters, GeoTIFFs and CSVs
  - run, merge, resume, settings guard, empty-window skipping
  - GeoPackage output and codes, through merge
  - buildings.py: segmentation, `reclaim_roof`, outlines, `roughness_inside`, the rule
    and the codes
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
  `conda activate`.
- PDAL prints harmless "(PDAL Error) Can't load library ... libhdf5_cpp.so.320" errors
  (the HDF reader plugin) with no trailing newline. They can merge with the next line of
  output when you filter logs.

## Files
- `src/tilestream.py`: the framework and CLI (info, plan, run, merge).
- `src/example_peaks.py`: example Python analysis (1 m DSM plus local maxima).
- `src/example_dsm.sh`: example `--exec` analysis (1 m DSM with the `pdal` command-line tool).
- `src/buildings.py`: building detection (an `--analysis` file; see "Building detection").
- `test/conftest.py`: the `--ept` option and the `site` fixture.
- `test/test_tilestream.py`, `test/test_ept.py`: the tests.
- `test/README.md`: what each test checks.
- `pyproject.toml`: pytest config (pythonpath `src`, `network` marker).
- `downloadlist_WA_NorthEast_2_B22.txt`: the original USGS list. Optional now; use it with `--list`.

## Next steps
1. **Tree crowns.** Michael wants crown polygons too, but first fewer artificially split
   crowns. His idea:
   - take the crown pixels of the dsm;
   - apply a mean filter over crown pixels only;
   - find the tops and run the watershed again, inside the crowns.

   Plan: first measure the splitting in Nine Mile, then try 2–3 filter sizes side by
   side, then build it into `segment()`. The crowns then get a code or a layer of their own.
2. Run the full network suite once (not run since 2026-10-30).
3. Later for buildings:
   - straighten outlines (4d);
   - a real-data seam test for buildings.py (a network test);
   - a clue for vehicles and containers (context, or a second scan);
   - the `reclaim_roof` thin-neck case.
4. Decide the permanent fix for the `~/.local` packages (see Environment).
5. Scale up: a modest real run with buildings.py (a few km²). Check the merged outputs,
   then go larger (`--list` or `--all`, in chunks or overnight). Measure memory first for
   larger windows.
