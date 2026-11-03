# Handoff: tilestream, windowed lidar processing from any EPT

_Last updated 2026-10-30, evening (Claude, after the test fixes and a full test run)._

## Goal
A reusable point-cloud querying layer. Give it an EPT (any USGS 3DEP project in
usgs-lidar-public, or any ept.json URL) and an area. It streams windows of points,
each with a halo, to any analysis as they arrive, with no edge effects and nothing big
on disk. Analyses plug in as Python (`--analysis file.py`) or as any command or shell
script (`--exec ./my_tool.sh`). **Buildings are not classified in WA_NorthEast_2_B22**
(only classes 1, 2, 7, 18), so we will have to detect them ourselves. That detection
will be a separate analysis script, not part of tilestream, and it is not the current
task: the focus is on making the tile reading work properly.

## Status (2026-10-30, evening)
**The full suite passes: 49 of 49** (40 offline, 9 network), in about 2 minutes.
- Test fixes from the morning, all done:
  - The test-site finder (`test/conftest.py`) compares each quarter with the
    well-filled quarters (`0.6 * percentile(count[count > 0], 75)`). It used to pick a
    site at the edge of the data.
  - `test_fetch_small_bbox` no longer asserts that the points fill the box.
  - Both real-data seam tests (`test_seam_python_analysis`, `test_cli_exec_seam`) now
    require DSM coverage > 0.9.
- The finder now picks the 500 m square at (445000, 5300000) in EPSG:6340 (relief score
  453 m). There, full density is 28.3 pts/m², 3.9 at `resolution=2` and 0.19 at `resolution=8`.
- `info` and `plan` from "How to use it" work. The 3 km window at `resolution=8` read
  0.65 M points in 2.3 s.
- This is a new machine. The env is now `~/miniforge3/envs/tilestream` (there is no
  `~/miniconda3` here), and Python must be run with `PYTHONNOUSERSITE=1`. See Environment.

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
  `HALO`. It returns `{name: Raster | table with x/y | summary dict of scalars}`. Build
  rasters on `window.grid(res)`.
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

## Tests
Run from the project root, inside a memory cap:

    PYTHONNOUSERSITE=1 systemd-run --user --scope -p MemoryMax=6G -p MemorySwapMax=0 \
        ~/miniforge3/envs/tilestream/bin/python -m pytest --ept WA_NorthEast_2_B22   # all, ~2 min
    ... -m pytest                                                                     # offline only, ~10 s

- **`test/test_tilestream.py`** (offline, 40 tests): synthetic points stand in for the
  EPT. The synthetic data includes a slope, location-based noise points and a hole.
  - window planning and exactly-once coverage
  - UTM zone choice
  - `plan()` for each kind of area
  - cropping rasters, GeoTIFFs and CSVs
  - run, merge, resume, settings guard, empty-window skipping
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
- `test/conftest.py`: the `--ept` option and the `site` fixture.
- `test/test_tilestream.py`, `test/test_ept.py`: the tests.
- `pyproject.toml`: pytest config (pythonpath `src`, `network` marker).
- `downloadlist_WA_NorthEast_2_B22.txt`: the original USGS list. Optional now; use it with `--list`.

## Next steps
1. Decide the permanent fix for the `~/.local` packages (see Environment).
2. Keep the focus on the reading layer. Try a modest real `--bbox` run with an example
   analysis, check the merged outputs for seams, then scale up (`--list` or `--all`, in
   chunks or overnight). Larger windows (2 km) might fetch faster per km², but measure
   memory first.
3. Later, as a separate analysis script: building detection. Decide Python or `--exec`,
   and its spatial reach, which sets `--halo`.
