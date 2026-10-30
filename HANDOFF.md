# Handoff: tilestream, windowed lidar processing from any EPT

_Last updated 2026-10-30, end of day._

## Goal
A reusable point-cloud querying layer. Give it an EPT (any USGS 3DEP project in
usgs-lidar-public, or any ept.json URL) and an area. It streams windows of points,
each with a halo, to any analysis as they arrive, with no edge effects and nothing big
on disk. Analyses plug in as Python (`--analysis file.py`) or as any command or shell
script (`--exec ./my_tool.sh`). Next comes the first real analysis tool. **Buildings are
not classified in WA_NorthEast_2_B22** (only classes 1, 2, 7, 18), so we will have to
detect them ourselves.

## In progress when we stopped
The querying layer is rewritten and the offline tests pass. The network tests had
**7 of 9 passing**, including both real-data seam tests (Python analysis and `--exec`).
The 2 failures came from a bug in the test-site finder (`test/conftest.py`). It picked a
site at the edge of the data, because a quarter counted as "full" when compared with a
percentile taken over a mostly empty grid.
- **Done:** fixed the finder. It now compares each quarter with the well-filled quarters:
  `0.6 * percentile(count[count > 0], 75)`.
- **Done:** removed the "fills the box" assertion from `test_fetch_small_bbox`.
- **Done:** `test_seam_python_analysis` now requires DSM coverage > 0.9.
- **Not done:** the same > 0.9 coverage threshold in `test_cli_exec_seam`. It's still 0.5 at `test/test_ept.py:109`.
- **Not done:** rerun the full suite after these fixes (command under Tests).

Then: update this file with the results and move on to the first analysis.

## Decision: read from the public EPT with PDAL, not rockyweb LAZ
- rockyweb is slow and unreliable (HTTP 429s; one 59 MB tile took 489 s).
- 3DEP is on AWS as EPT octrees, stored in EPSG:3857 with all LAS attributes. Each
  window queries only its own box.
- Checked against delivered tile 11TMN470250: point counts within 0.002%, class counts
  identical, positions match to a 0.27 cm median after reprojecting to EPSG:6340.

## How to use it
`--ept` is required everywhere. Exactly one area must be given; no area is an error,
so a forgotten `--bbox` can't start a run over all 167 billion points.

    P=~/miniconda3/envs/tilestream/bin/python
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
  5–10× denser than the name suggests (500 m box: 11.6 pts/m² full, 2.6 at 2 m, 0.08 at 8 m).
- **Memory:** a 3×3 km window at full density (~110 M points) does not fit in RAM. The
  first attempt was OOM-killed and took VS Code with it. Use `resolution=` and `dims=`
  for big windows, and run tests under a memory cap.
- **Throughput** (earlier, 1 km windows): about 19 s fetch and 0.5 s analysis per window.
  The fetch is the bottleneck. Estimate for the 3,750-tile list: 16–20 h, resumable.
- **`--all` on WA_NorthEast_2_B22** gives 8,837 windows of 1 km. That's about 10% more
  than the 8,068 tiles, because the 2 km cells overshoot the data edge.

## Tests
Run from the project root, inside a memory cap:

    systemd-run --user --scope -p MemoryMax=6G -p MemorySwapMax=0 \
        ~/miniconda3/envs/tilestream/bin/python -m pytest --ept WA_NorthEast_2_B22   # all, ~1.5 min
    ... -m pytest                                                                     # offline only, ~7 s

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
- Conda env `tilestream` (conda-forge): python 3.12, pdal 2.10 + python-pdal, gdal,
  numpy, scipy, pyproj, laspy/lazrs (validation only), and pytest (added today).
- `tilestream.py` sets PROJ_DATA/GDAL_DATA itself, so the env's python works without
  `conda activate`.
- PDAL prints harmless "Can't load ... libpdal_plugin_reader_hdf.so" errors with no
  trailing newline. They can merge with the next line of output when you filter logs.

## Files
- `src/tilestream.py`: the framework and CLI (info, plan, run, merge).
- `src/example_peaks.py`: example Python analysis (1 m DSM plus local maxima).
- `src/example_dsm.sh`: example `--exec` analysis (1 m DSM with the `pdal` command-line tool).
- `test/conftest.py`: the `--ept` option and the `site` fixture.
- `test/test_tilestream.py`, `test/test_ept.py`: the tests.
- `pyproject.toml`: pytest config (pythonpath `src`, `network` marker).
- `downloadlist_WA_NorthEast_2_B22.txt`: the original USGS list. Optional now; use it with `--list`.

## Next steps
1. Finish the test fixes above and rerun the suite.
2. **First real analysis tool** (probably building detection, since the data has no
   building class). Decide Python or `--exec`, and its spatial reach, which sets `--halo`.
3. Try a modest `--bbox` run with it, check the merged outputs for seams, then scale
   up (`--list` or `--all`, in chunks or overnight). Larger windows (2 km) might fetch
   faster per km², but measure memory first.
