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
`PYTHONPATH`, so you can't point pytest at another copy of a module that way.

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

**buildings.py**
- `test_rectangularity`: a rectangle scores 1, a 45-degree rotated rectangle more than
  0.9 (its edges are pixel steps), an L-shape exactly 341/441, and a disc about pi/4.
- `test_segment_keeps_small_patch_next_to_tree`: a 2 x 2 m patch half a metre from a
  taller tree has no tree top of its own (the tree is higher within `TREE_TOP_RADIUS`),
  but must still become its own object. Before the fix, such patches (sheds!) vanished.

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

- A seam test for `buildings.py` (all its rasters and the objects table). It was checked
  by hand on Chewelah: identical. The synthetic points have no buildings or trees, so this
  needs real data (a network test).
- The roof seeds in `segment()` (covered holes, few split pulses, smooth).

## Adding a test

- Offline if possible: use `synthetic_points`, or build small arrays by hand like
  `test_segment_keeps_small_patch_next_to_tree`.
- A test for a bug should fail without the fix. To check, put a copy of the module
  without the fix in another folder and call the test function directly (pytest itself
  always uses `src/`):

      PYTHONNOUSERSITE=1 PYTHONPATH=/path/to/copy:src:test ~/miniforge3/envs/tilestream/bin/python \
          -c "import test_tilestream as t; t.test_segment_keeps_small_patch_next_to_tree()"
