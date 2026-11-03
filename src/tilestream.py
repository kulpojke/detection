#!/usr/bin/env python3
"""Windowed, edge-effect-free processing of lidar from any Entwine (EPT) point cloud,
e.g. the USGS 3DEP projects in usgs-lidar-public on S3.

Nothing big ever lands on disk. Points are pulled straight from the EPT octree,
one window at a time, and handed to an analysis as they arrive:

  1. The area is cut into core windows in a metric CRS (by default the UTM zone
     at the centre of the area): a grid of any size over a bbox, a USGS download
     list or the whole EPT footprint (``grid_windows``), or one window per
     user-supplied bbox for site checks (``bbox_windows``).
  2. Each window is fetched with a ``halo`` (metres) around its core, so
     analysis near the core edge sees the same data it would in one giant
     mosaic. Halos past the edge of the area still get real data.
  3. Outputs are cropped back to the core. Cores tile the plane without
     overlap (half-open bounds), so every output location is produced exactly
     once and per-window outputs mosaic seamlessly.
  4. The next window is fetched in a background thread while the current one
     is analysed (PDAL releases the GIL), so at most ``prefetch + 1`` windows
     of points are in memory.

An analysis is either a Python file defining ``analyze(points, window)`` (see
example_peaks.py) or any command, e.g. a shell script, run once per window on a
temporary LAZ of its points (see example_dsm.sh and ``exec_analysis``).

Rule: ``halo`` must be >= the spatial reach of the analysis (filter radius,
search distance, neighbourhood size, ...). Otherwise results near core edges
will differ from a whole-area run.

Runs are resumable: finished windows are recorded in ``<out>/_done`` and skipped.

CLI (EPT = usgs-lidar-public project name, e.g. WA_NorthEast_2_B22, or an ept.json URL;
AREA = --bbox XMIN YMIN XMAX YMAX [--bbox-crs CRS] | --bboxes CSV | --list FILE | --all):
    python src/tilestream.py info  --ept EPT
    python src/tilestream.py plan  --ept EPT AREA [--core METRES] [--halo METRES]
    python src/tilestream.py run   OUT --ept EPT AREA (--analysis FILE.py | --exec "CMD ...") [--halo METRES]
    python src/tilestream.py merge OUT
"""
from __future__ import annotations

import argparse
import csv
import functools
import importlib.util
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

# Running the env's python without `conda activate` leaves PROJ/GDAL without their data dirs.
for _var, _sub in (("PROJ_DATA", "share/proj"), ("GDAL_DATA", "share/gdal")):
    if _var not in os.environ and (Path(sys.prefix) / _sub).is_dir():
        os.environ[_var] = str(Path(sys.prefix) / _sub)

EPT_ROOT = "https://s3-us-west-2.amazonaws.com/usgs-lidar-public"
LONLAT = "EPSG:4326"

Bounds = tuple[float, float, float, float]  # xmin, ymin, xmax, ymax


# --------------------------------------------------------------------------- EPT metadata

def ept_url(project: str) -> str:
    """URL of an EPT, from a usgs-lidar-public project name (e.g. WA_NorthEast_2_B22) or URL."""
    return project if "/" in project else f"{EPT_ROOT}/{project}/ept.json"


@functools.lru_cache(maxsize=None)
def _get_json(url: str, retries: int = 5) -> dict:
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.load(r)
        except OSError:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)


def ept_info(ept: str) -> dict:
    """The EPT's ept.json: bounds, boundsConforming, points, schema, srs, ..."""
    return _get_json(ept_url(ept))


def ept_crs(ept: str) -> str:
    s = ept_info(ept)["srs"]
    return f"{s['authority']}:{s['horizontal']}" if s.get("authority") and s.get("horizontal") else s["wkt"]


def footprint(ept: str, crs: str = LONLAT) -> Bounds:
    """Bounding box of the EPT's points (boundsConforming), in ``crs``."""
    from pyproj import Transformer
    b = ept_info(ept)["boundsConforming"]
    return Transformer.from_crs(ept_crs(ept), crs, always_xy=True).transform_bounds(b[0], b[1], b[3], b[4], densify_pts=21)


def occupied_boxes(ept: str, srs: str) -> list[Bounds]:
    """XY boxes (in ``srs``) of the deepest octree nodes in the EPT's root hierarchy file:
    a cheap, slightly generous map of where there is data (~2 km cells for a big project)."""
    from pyproj import Transformer
    hierarchy = _get_json(ept_url(ept).rsplit("/", 1)[0] + "/ept-hierarchy/0-0-0-0.json")
    keys = [tuple(map(int, k.split("-"))) for k in hierarchy]
    depth = max(k[0] for k in keys)
    x0, y0, _, x1, _, _ = ept_info(ept)["bounds"]
    size = (x1 - x0) / 2 ** depth
    tr = Transformer.from_crs(ept_crs(ept), srs, always_xy=True)
    cells = sorted({(x, y) for d, x, y, _ in keys if d == depth})
    return [tr.transform_bounds(x0 + i * size, y0 + j * size, x0 + (i + 1) * size, y0 + (j + 1) * size,
                                densify_pts=5) for i, j in cells]


def utm_srs(lon: float, lat: float, zone: int | None = None) -> str:
    """NAD83(2011) / UTM (3DEP's usual datum) for the zone at lon/lat; WGS 84 / UTM where
    NAD83(2011) has no UTM CRS (Hawaii, Puerto Rico, outside the US) or south of the equator."""
    from pyproj.aoi import AreaOfInterest
    from pyproj.database import query_utm_crs_info
    zone = zone or int((lon + 180) // 6) % 60 + 1
    if lat >= 0:
        for c in query_utm_crs_info(datum_name="NAD83(2011)", area_of_interest=AreaOfInterest(lon, lat, lon, lat)):
            if c.name.endswith(f"zone {zone}N"):
                return f"{c.auth_name}:{c.code}"
    return f"EPSG:{(32600 if lat >= 0 else 32700) + zone}"


def transform_bounds(b: Bounds, src: str, dst: str) -> Bounds:
    """Box enclosing ``b`` after reprojection (edges densified)."""
    if src == dst:
        return tuple(map(float, b))
    from pyproj import Transformer
    return Transformer.from_crs(src, dst, always_xy=True).transform_bounds(*b, densify_pts=21)


def _centre(b: Bounds) -> tuple[float, float]:
    return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2


# --------------------------------------------------------------------------- area of interest

TILE_M = 1000.0
# USGS LPC download lists, e.g. USGS_LPC_WA_NorthEast_B22_11TMN438236.laz -> zone 11, band T,
# square MN, easting 438 km, northing (5)236 km
TILE_RE = re.compile(r"_(\d{1,2})([C-HJ-NP-X])([A-HJ-NP-Z]{2})(\d{3})(\d{3})\.laz$")
MGRS_BANDS = "CDEFGHJKLMNPQRSTUVWX"


@dataclass(frozen=True)
class Tile:
    name: str
    zone: int
    xmin: float
    ymin: float

    @property
    def bounds(self) -> Bounds:
        return self.xmin, self.ymin, self.xmin + TILE_M, self.ymin + TILE_M


def _northing_km(band: str, km3: int) -> int:
    """Tile names keep only northing km mod 1000; recover the rest from the latitude band."""
    lat0 = -80 + 8 * MGRS_BANDS.index(band)
    if lat0 < 0:
        raise ValueError("southern-hemisphere tiles are not supported")
    lo, hi = lat0 * 110.6 - 30, (lat0 + 8) * 110.9 + 30  # ~km per degree of latitude
    hits = [k * 1000 + km3 for k in range(10) if lo <= k * 1000 + km3 <= hi]
    if len(hits) != 1:
        raise ValueError(f"cannot resolve northing for band {band}, {km3:03d}")
    return hits[0]


def load_tiles(list_path: str | Path) -> list[Tile]:
    """Footprints (UTM, 1 km) of the tiles in a USGS LPC download list whose tile names
    end in MGRS-style ``_11TMN470250.laz``."""
    tiles = []
    for url in Path(list_path).read_text().split():
        m = TILE_RE.search(url)
        if not m:
            raise ValueError(f"unrecognised tile name: {url}")
        tiles.append(Tile(url.rsplit("/", 1)[-1], int(m[1]),
                          int(m[4]) * TILE_M, _northing_km(m[2], int(m[5])) * TILE_M))
    if len({t.zone for t in tiles}) > 1:
        raise ValueError("tiles span several UTM zones; split the list per zone")
    return tiles


def load_bboxes(csv_path: str | Path) -> tuple[list[Bounds], list[str] | None]:
    """Read a CSV with columns xmin, ymin, xmax, ymax and optionally name."""
    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))
    boxes = [tuple(float(r[k]) for k in ("xmin", "ymin", "xmax", "ymax")) for r in rows]
    return boxes, [r["name"] for r in rows] if rows and "name" in rows[0] else None


# --------------------------------------------------------------------------- windows

@dataclass(frozen=True)
class Window:
    name: str
    core: Bounds
    halo: Bounds

    def in_core(self, x, y):
        """Half-open ownership test: each location belongs to exactly one window."""
        x0, y0, x1, y1 = self.core
        return (x >= x0) & (x < x1) & (y >= y0) & (y < y1)

    def grid(self, res: float) -> tuple[float, float, int, int]:
        """(x_left, y_top, nx, ny) of a north-up grid covering the halo, snapped to
        global multiples of ``res`` so the core crop lands exactly on pixel edges."""
        hx0, hy0, hx1, hy1 = self.halo
        x0, y0 = math.floor(hx0 / res) * res, math.floor(hy0 / res) * res
        x1, y1 = math.ceil(hx1 / res) * res, math.ceil(hy1 / res) * res
        return x0, y1, round((x1 - x0) / res), round((y1 - y0) / res)


def grid_windows(boxes: list[Bounds], core: float = 1000.0, halo: float = 50.0,
                 order: str = "rows", bbox: Bounds | None = None) -> list[Window]:
    """``core``-metre square windows on a grid anchored at multiples of ``core``,
    covering every box (tile footprints, an area, occupied EPT cells, ...) and, if
    given, touching ``bbox``. Serpentine order ("rows": south to north; "cols": west to east)."""
    size = float(core)
    blocks = {(i, j) for x0, y0, x1, y1 in boxes
              for i in range(math.floor(x0 / size), math.ceil(x1 / size))
              for j in range(math.floor(y0 / size), math.ceil(y1 / size))}
    if bbox:
        bx0, by0, bx1, by1 = bbox
        blocks = {(i, j) for i, j in blocks
                  if i * size < bx1 and (i + 1) * size > bx0 and j * size < by1 and (j + 1) * size > by0}
    if order == "rows":
        key = lambda ij: (ij[1], ij[0] if ij[1] % 2 == 0 else -ij[0])
    elif order == "cols":
        key = lambda ij: (ij[0], ij[1] if ij[0] % 2 == 0 else -ij[1])
    else:
        raise ValueError(f"unknown order {order!r}")
    windows = []
    for i, j in sorted(blocks, key=key):
        c = (i * size, j * size, (i + 1) * size, (j + 1) * size)
        windows.append(Window(f"{c[0]:.0f}_{c[1]:.0f}", c, (c[0] - halo, c[1] - halo, c[2] + halo, c[3] + halo)))
    return windows


def bbox_windows(bboxes: list[Bounds], halo: float, names: list[str] | None = None,
                 crs: str | None = None, srs: str | None = None) -> list[Window]:
    """One window per bbox (core = the bbox itself), for site checks. Bboxes in another
    ``crs`` (e.g. "EPSG:4326", lon/lat) become their enclosing box in ``srs``.
    Unlike grid windows, overlapping bboxes are processed independently."""
    if crs and crs != srs:
        bboxes = [transform_bounds(b, crs, srs) for b in bboxes]
    names = [re.sub(r"[^\w.-]", "_", str(n)) for n in names] if names else [f"site_{i:05d}" for i in range(len(bboxes))]
    if len(set(names)) != len(names):
        raise ValueError("bbox names must be unique")
    return [Window(n, tuple(map(float, b)), (b[0] - halo, b[1] - halo, b[2] + halo, b[3] + halo))
            for n, b in zip(names, bboxes)]


def plan(ept: str | None, halo: float, *, bbox: Bounds | None = None, bboxes: list[Bounds] | None = None,
         names: list[str] | None = None, bbox_crs: str = LONLAT, tiles: list[Tile] | None = None,
         everything: bool = False, core: float = 1000.0, srs: str | None = None,
         order: str = "rows") -> tuple[list[Window], str]:
    """Windows and their CRS for one of: a grid over ``bbox``; one window per box in
    ``bboxes``; a grid over download-list ``tiles``; a grid over the whole EPT footprint
    (``everything``, skipping empty octree cells). ``bbox`` also clips tiles/everything.
    ``srs`` defaults to the UTM zone at the centre of the area (the tiles' zone for a list)."""
    if sum(x is not None and x is not False for x in (bboxes, tiles, everything)) > 1 or (bboxes and bbox):
        raise ValueError("give one area: --bbox, --bboxes, --list or --all (--bbox may also clip --list/--all)")
    if bboxes is not None:
        ll = [transform_bounds(b, bbox_crs, LONLAT) for b in bboxes]
        srs = srs or utm_srs(*_centre((min(b[0] for b in ll), min(b[1] for b in ll),
                                       max(b[2] for b in ll), max(b[3] for b in ll))))
        return bbox_windows(bboxes, halo, names, bbox_crs, srs), srs
    if tiles is not None:
        if not srs:
            from pyproj import Transformer
            zone = tiles[0].zone
            lon, lat = Transformer.from_crs(f"EPSG:{32600 + zone}", LONLAT, always_xy=True).transform(
                tiles[0].xmin + TILE_M / 2, tiles[0].ymin + TILE_M / 2)
            srs = utm_srs(lon, lat, zone)
        boxes = [t.bounds for t in tiles]
    elif everything:
        srs = srs or utm_srs(*_centre(transform_bounds(bbox, bbox_crs, LONLAT) if bbox else footprint(ept)))
        boxes = occupied_boxes(ept, srs)
    elif bbox:
        srs = srs or utm_srs(*_centre(transform_bounds(bbox, bbox_crs, LONLAT)))
        boxes = [transform_bounds(bbox, bbox_crs, srs)]
    else:
        raise ValueError("no area: give --bbox, --bboxes, --list or --all")
    clip = transform_bounds(bbox, bbox_crs, srs) if bbox else None
    return grid_windows(boxes, core, halo, order, clip), srs


# --------------------------------------------------------------------------- fetching

def fetch_points(ept: str, bounds: Bounds, srs: str, dims: tuple[str, ...] | None = None,
                 resolution: float | None = None, threads: int = 8, retries: int = 6) -> np.ndarray:
    """Points inside ``bounds`` (half-open, in ``srs``) from an EPT, as a structured array
    with PDAL dimension names (X, Y, Z, Classification, ReturnNumber, ...).

    ``resolution`` (ground metres) thins the read to the EPT octree levels down to about
    that point spacing (in practice several times denser); None reads full density.
    """
    import pdal
    from pyproj import Transformer
    crs = ept_crs(ept)
    x0, y0, x1, y1 = bounds
    pad = 5.0  # m, absorbs datum-shift wobble between the two CRSs
    to_ept = Transformer.from_crs(srs, crs, always_xy=True)
    q = to_ept.transform_bounds(x0 - pad, y0 - pad, x1 + pad, y1 + pad, densify_pts=21)
    reader = {"type": "readers.ept", "filename": ept_url(ept), "threads": threads,
              "bounds": f"([{q[0]}, {q[2]}], [{q[1]}, {q[3]}])"}
    if resolution:
        cx, cy = _centre(bounds)  # EPT units per ground metre (e.g. Web Mercator stretch)
        (ax, bx), (ay, by) = to_ept.transform([cx, cx + 100], [cy, cy])
        reader["resolution"] = resolution * math.hypot(bx - ax, by - ay) / 100
    pipeline = json.dumps([reader, {"type": "filters.reprojection", "out_srs": srs}])
    delay = 10.0
    for attempt in range(retries):
        try:
            p = pdal.Pipeline(pipeline)
            p.execute()
            break
        except RuntimeError as e:
            if attempt == retries - 1:
                raise
            print(f"  ! EPT query failed ({str(e)[:120]}); retry {attempt + 1}/{retries} in {delay:.0f}s", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 300)
    if p.arrays:
        a = p.arrays[0]
    else:  # no points: an empty array with the fields and types a full read would have
        kind = {"floating": "f", "signed": "i", "unsigned": "u"}
        a = np.empty(0, [(d["name"], f"{kind[d['type']]}{d['size']}") for d in p.schema["schema"]["dimensions"]])
    a = a[(a["X"] >= x0) & (a["X"] < x1) & (a["Y"] >= y0) & (a["Y"] < y1)]
    if dims:
        from numpy.lib import recfunctions as rfn
        a = rfn.repack_fields(a[list(dims)])
    return a


def write_laz(path: Path, pts: np.ndarray, srs: str):
    """Write points (non-empty) to a LAS 1.4 / LAZ file at millimetre precision."""
    import pdal
    opts = {"type": "writers.las", "filename": str(path), "compression": True, "a_srs": srs,
            "minor_version": 4, "dataformat_id": 6, "extra_dims": "all"}
    # Offsets on a fixed 1 km lattice (not PDAL's "auto" = per-file minimum), so a point is
    # rounded to the same millimetre in every window that contains it.
    for a in "xyz":
        opts[f"scale_{a}"] = 0.001
        opts[f"offset_{a}"] = math.floor(float(pts[a.upper()].min()) / 1000) * 1000
    pdal.Pipeline(json.dumps([opts]), arrays=[pts]).execute()


# --------------------------------------------------------------------------- outputs

@dataclass
class Raster:
    """North-up grid. ``data`` is (ny, nx) or (bands, ny, nx); (x0, y0) is the top-left corner.
    Build it on ``window.grid(res)`` so the core crop is exact."""
    data: np.ndarray
    x0: float
    y0: float
    res: float
    nodata: float | None = None


def _core_pixels(core: Bounds, x0: float, y0: float, res: float, nx: int, ny: int) -> tuple[int, int, int, int]:
    """(col0, row0, col1, row1) of the pixels touching the core, for a grid at top-left (x0, y0)."""
    cx0, cy0, cx1, cy1 = core
    eps = 1e-6
    c0, r0 = math.floor((cx0 - x0) / res + eps), math.floor((y0 - cy1) / res + eps)
    c1, r1 = math.ceil((cx1 - x0) / res - eps), math.ceil((y0 - cy0) / res - eps)
    if c0 < 0 or r0 < 0 or c1 > nx or r1 > ny:
        raise ValueError("raster does not cover the window core; build it over the window halo")
    return c0, r0, c1, r1


def crop_raster(r: Raster, core: Bounds) -> Raster:
    """Crop to the pixels touching the core. Exact for grid windows whose size is a
    multiple of res; arbitrary bboxes are snapped outward to whole pixels."""
    ny, nx = r.data.shape[-2:]
    c0, r0, c1, r1 = _core_pixels(core, r.x0, r.y0, r.res, nx, ny)
    return Raster(r.data[..., r0:r1, c0:c1], r.x0 + c0 * r.res, r.y0 - r0 * r.res, r.res, r.nodata)


def crop_geotiff(src: Path, dst: Path, core: Bounds):
    """crop_raster for a north-up GeoTIFF written by an external tool."""
    from osgeo import gdal
    gdal.UseExceptions()
    ds = gdal.Open(str(src))
    x0, res, rx, y0, ry, yres = ds.GetGeoTransform()
    if rx or ry or not math.isclose(res, -yres):
        raise ValueError(f"{src}: only north-up rasters with square pixels can be cropped")
    c0, r0, c1, r1 = _core_pixels(core, x0, y0, res, ds.RasterXSize, ds.RasterYSize)
    tmp = dst.with_name(dst.name + ".tmp")
    gdal.Translate(str(tmp), ds, format="GTiff", srcWin=[c0, r0, c1 - c0, r1 - r0],
                   creationOptions=["COMPRESS=DEFLATE", "TILED=YES"])
    ds = None
    os.replace(tmp, dst)


def crop_csv(src: Path, dst: Path, window: Window):
    """Keep the rows of a CSV whose x, y columns fall in the window core (all rows if it has none)."""
    with open(src, newline="") as f:
        reader = csv.DictReader(f)
        rows, fields = list(reader), reader.fieldnames or []
    if "x" in fields and "y" in fields:
        rows = [r for r in rows if window.in_core(float(r["x"]), float(r["y"]))]
    tmp = dst.with_name(dst.name + ".tmp")
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, dst)


def write_geotiff(path: Path, r: Raster, srs: str):
    from osgeo import gdal, osr
    gdal.UseExceptions()
    data = r.data if r.data.ndim == 3 else r.data[None]
    if data.dtype == np.bool_:
        data = data.astype(np.uint8)
    tmp = path.with_name(path.name + ".tmp")
    ds = gdal.GetDriverByName("GTiff").Create(str(tmp), data.shape[2], data.shape[1], data.shape[0],
                                              gdal.GetDataTypeByName(data.dtype.name),
                                              ["COMPRESS=DEFLATE", "TILED=YES"])
    ds.SetGeoTransform((r.x0, r.res, 0, r.y0, 0, -r.res))
    sr = osr.SpatialReference()
    sr.SetFromUserInput(srs)
    ds.SetProjection(sr.ExportToWkt())
    for b in range(data.shape[0]):
        band = ds.GetRasterBand(b + 1)
        if r.nodata is not None:
            band.SetNoDataValue(r.nodata)
        band.WriteArray(data[b])
    ds = None
    os.replace(tmp, path)


def save_table(path: Path, table: dict[str, np.ndarray], window: Window):
    keep = window.in_core(np.asarray(table["x"]), np.asarray(table["y"]))
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        np.savez_compressed(f, **{k: np.asarray(v)[keep] for k, v in table.items()})
    os.replace(tmp, path)


def save_features(path: Path, table: dict[str, np.ndarray], window: Window, srs: str,
                  codes: dict[str, dict[int, str]] | None = None):
    """A table with a "geometry" column (WKT, in ``srs``) as a GeoPackage layer, the other
    columns as attributes. As in save_table, a row is kept if its x, y lies in the core, so a
    polygon that reaches into a neighbouring window is written whole, by one window only.
    ``codes`` ({column: {code: label}}) is stored as a coded field domain, so the file itself
    says what each code means (QGIS shows the labels)."""
    from osgeo import ogr, osr
    ogr.UseExceptions()
    keep = window.in_core(np.asarray(table["x"]), np.asarray(table["y"]))
    cols = {k: np.asarray(v)[keep] for k, v in table.items()}
    geoms = cols.pop("geometry")
    kinds = {"f": ogr.OFTReal, "i": ogr.OFTInteger64, "u": ogr.OFTInteger64, "b": ogr.OFTInteger}  # else text
    sr = osr.SpatialReference()
    sr.SetFromUserInput(srs)
    tmp = path.with_suffix(".tmp.gpkg")  # the driver wants a .gpkg name; merge skips .tmp.gpkg
    tmp.unlink(missing_ok=True)  # left by a crash: the driver won't overwrite it
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(tmp))
    layer = ds.CreateLayer(path.parent.name, sr, ogr.wkbUnknown)  # any geometry type
    for k, v in cols.items():
        field = ogr.FieldDefn(k, kinds.get(v.dtype.kind, ogr.OFTString))
        if k in (codes or {}):
            ds.AddFieldDomain(ogr.CreateCodedFieldDomain(k, "", field.GetType(), ogr.OFSTNone,
                                                         {str(c): label for c, label in codes[k].items()}))
            field.SetDomainName(k)
        layer.CreateField(field)
    layer.StartTransaction()  # one commit for all features, much faster than one each
    for i, wkt in enumerate(geoms):
        f = ogr.Feature(layer.GetLayerDefn())
        f.SetGeometry(ogr.CreateGeometryFromWkt(str(wkt)))
        for k, v in cols.items():
            value = v[i].item()  # numpy -> plain Python value
            if value != value:  # NaN: SQLite has none, so leave the field empty (NULL)
                f.SetFieldNull(k)
            else:
                f.SetField(k, value)
        layer.CreateFeature(f)
    layer.CommitTransaction()
    ds = None  # closes the file
    os.replace(tmp, path)


# --------------------------------------------------------------------------- analyses

Analysis = Callable[[np.ndarray, Window], dict[str, "Raster | dict"]]


def _is_summary(r) -> bool:
    return isinstance(r, dict) and all(np.ndim(v) == 0 for v in r.values())


def load_analysis(path: str | Path):
    """Import a Python analysis file: it must define ``analyze(points, window)`` and may
    define ``HALO`` (metres) and ``CODES`` (what coded columns mean; see run)."""
    spec = importlib.util.spec_from_file_location(Path(path).stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "analyze", None)):
        raise ValueError(f"{path} does not define analyze(points, window)")
    return module


def exec_analysis(cmd: str | list[str], out_dir: str | Path, srs: str) -> Analysis:
    """Wrap a command (e.g. "./my_tool.sh --opt 3") as an analysis. For each window it runs

        CMD... WINDOW.laz WORK_DIR

    with the window's points (core + halo, in ``srs``) in a temporary LAZ, the conda
    env's bin/ first on PATH (so ``pdal`` and ``gdal_*`` match this env), and the
    environment variables TS_NAME, TS_SRS, TS_CORE and TS_HALO ("xmin ymin xmax ymax").
    Each file the command writes to WORK_DIR as <name>.<ext> becomes OUT/<name>/<window>.<ext>:
    GeoTIFFs are cropped to the core (put them on a grid aligned to multiples of the
    resolution so the crop is exact), CSVs with x and y columns lose rows outside the
    core, a <name>.json of scalars becomes a summary row in OUT/<name>.csv after merge,
    anything else is kept as is. That includes a GeoPackage: it isn't cropped, but merge
    still joins them, so write only the features you own (e.g. whose point is in TS_CORE).
    A non-zero exit stops the run with that window not done.
    """
    argv = shlex.split(cmd) if isinstance(cmd, str) else list(cmd)
    out_dir = Path(out_dir)
    path = f"{Path(sys.prefix) / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"

    def analyze(pts, w):
        work = out_dir / "_work" / w.name
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        laz = work.with_suffix(".laz")
        try:
            write_laz(laz, pts, srs)
            env = {**os.environ, "PATH": path, "TS_NAME": w.name, "TS_SRS": srs,
                   "TS_CORE": " ".join(map(repr, w.core)), "TS_HALO": " ".join(map(repr, w.halo))}
            code = subprocess.run([*argv, str(laz), str(work)], env=env).returncode
        finally:
            laz.unlink(missing_ok=True)
        if code:
            raise RuntimeError(f"{argv[0]} exited with {code} on window {w.name}; its output is in {work}")
        summaries = {}
        for f in sorted(work.iterdir()):
            if f.suffix == ".json" and f.is_file():
                summaries[f.stem] = json.loads(f.read_text())
                if not _is_summary(summaries[f.stem]):
                    raise ValueError(f"{f.name} on window {w.name}: a summary must be a JSON object of scalars")
                continue
            d = out_dir / f.stem
            d.mkdir(exist_ok=True)
            dst = d / f"{w.name}{f.suffix}"
            if f.suffix.lower() in (".tif", ".tiff"):
                crop_geotiff(f, dst, w.core)
            elif f.suffix == ".csv":
                crop_csv(f, dst, w)
            else:
                shutil.rmtree(dst, ignore_errors=True) if dst.is_dir() else dst.unlink(missing_ok=True)
                os.replace(f, dst)
        shutil.rmtree(work)
        return summaries

    return analyze


# --------------------------------------------------------------------------- driver

def run(windows: list[Window], analyze: Analysis, out_dir: str | Path, *, ept: str, srs: str,
        dims: tuple[str, ...] | None = None, resolution: float | None = None,
        prefetch: int = 1, threads: int = 8, limit: int | None = None,
        codes: dict[str, dict[str, dict[int, str]]] | None = None):
    """Process windows (from plan, grid_windows or bbox_windows): fetch core+halo points,
    analyze, crop to core, save. Windows with no points are recorded as done without
    calling ``analyze``.

    ``ept`` is a usgs-lidar-public project name or an ept.json URL.
    ``analyze(points, window)`` gets a structured array cropped to window.halo and
    returns {output_name: result}, where a result is one of
      * Raster  -> cropped to the core, out_dir/<name>/<window>.tif
      * table   -> dict of equal-length arrays with "x" and "y"; rows outside the
                   core dropped, out_dir/<name>/<window>.npz
      * features-> a table that also has "geometry" (WKT polygons, lines or points, in
                   srs); rows whose x, y is outside the core dropped, the rest kept
                   whole, out_dir/<name>/<window>.gpkg
      * summary -> dict of scalars (e.g. {"building": True}); one row per window
                   in out_dir/<name>.csv after ``merge``
    ``codes`` ({output_name: {column: {code: label}}}) says what the codes in a features
    column mean; it is stored in the GeoPackage (see save_features).
    """
    out_dir = Path(out_dir)
    done_dir = out_dir / "_done"
    done_dir.mkdir(parents=True, exist_ok=True)
    settings = {"ept": ept_url(ept), "srs": srs, "dims": list(dims) if dims else None, "resolution": resolution}
    settings_file = out_dir / "_run.json"
    if settings_file.exists() and json.loads(settings_file.read_text()) != settings:
        raise ValueError(f"{out_dir} holds a run with other settings ({settings_file.read_text()}); "
                         "use a new output directory")
    settings_file.write_text(json.dumps(settings))
    todo = [w for w in windows if not (done_dir / f"{w.name}.json").exists()]
    print(f"{len(windows)} windows, {len(windows) - len(todo)} already done, "
          f"{min(len(todo), limit or len(todo))} to run; source {ept_url(ept)}, {srs}", flush=True)
    todo = todo[:limit] if limit else todo

    pool = ThreadPoolExecutor(max(prefetch, 1))
    fetch = lambda w: (time.time(), fetch_points(ept, w.halo, srs, dims, resolution, threads), time.time())
    futures = {k: pool.submit(fetch, todo[k]) for k in range(min(prefetch + 1, len(todo)))}
    try:
        for step, w in enumerate(todo):
            t0 = time.time()
            f0, pts, f1 = futures.pop(step).result()
            if step + prefetch + 1 < len(todo):
                futures[step + prefetch + 1] = pool.submit(fetch, todo[step + prefetch + 1])
            t1 = time.time()
            results = (analyze(pts, w) or {}) if len(pts) else {}
            t2 = time.time()
            summaries = {}
            for out_name, r in results.items():
                if _is_summary(r):
                    summaries[out_name] = {k: np.asarray(v).item() for k, v in r.items()}
                    continue
                d = out_dir / out_name
                d.mkdir(exist_ok=True)
                if isinstance(r, Raster):
                    write_geotiff(d / f"{w.name}.tif", crop_raster(r, w.core), srs)
                elif "geometry" in r:
                    save_features(d / f"{w.name}.gpkg", r, w, srs, (codes or {}).get(out_name))
                else:
                    save_table(d / f"{w.name}.npz", r, w)
            n = len(pts)
            del pts, results
            (done_dir / f"{w.name}.json").write_text(json.dumps(
                {"core": w.core, "points": n, "fetch_s": f1 - f0, "wait_s": t1 - t0, "analyze_s": t2 - t1,
                 "summaries": summaries}))
            print(f"[{step + 1}/{len(todo)}] {w.name}  {n / 1e6:6.2f} Mpts  fetch {f1 - f0:5.1f}s  "
                  f"waited {t1 - t0:5.1f}s  analyze {t2 - t1:5.1f}s" + ("" if n else "  (empty, skipped)"),
                  flush=True)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def merge(out_dir: str | Path):
    """Mosaic raster outputs as VRTs, concatenate tables into one .npz each, GeoPackages into
    one .gpkg each and CSVs into one .csv each (with a window column), and collect summaries
    into one .csv each."""
    out_dir = Path(out_dir)
    for d in sorted(p for p in out_dir.iterdir() if p.is_dir() and not p.name.startswith("_")):
        tifs, npzs, csvs = sorted(d.glob("*.tif")), sorted(d.glob("*.npz")), sorted(d.glob("*.csv"))
        gpkgs = sorted(p for p in d.glob("*.gpkg") if not p.name.endswith(".tmp.gpkg"))
        if tifs:
            from osgeo import gdal
            gdal.UseExceptions()
            gdal.BuildVRT(str(out_dir / f"{d.name}.vrt"), [str(t) for t in tifs])
            print(f"{d.name}.vrt  <- {len(tifs)} tiles")
        if npzs:
            cols = defaultdict(list)
            for p in npzs:
                with np.load(p) as z:
                    for k in z.files:
                        cols[k].append(z[k])
            np.savez_compressed(out_dir / f"{d.name}.npz", **{k: np.concatenate(v) for k, v in cols.items()})
            print(f"{d.name}.npz  <- {len(npzs)} windows, {sum(map(len, cols['x'])):,} rows")
        if gpkgs:
            from osgeo import gdal
            gdal.UseExceptions()
            target = out_dir / f"{d.name}.gpkg"
            target.unlink(missing_ok=True)
            for p in gpkgs:  # the first creates the file, the others add to it
                gdal.VectorTranslate(str(target), str(p), accessMode="append", layerName=d.name)
            ds = gdal.OpenEx(str(target))  # keep ds: a layer is gone once its dataset is
            print(f"{d.name}.gpkg  <- {len(gpkgs)} windows, {ds.GetLayer(0).GetFeatureCount():,} features")
        if csvs:
            rows = []
            for p in csvs:
                with open(p, newline="") as f:
                    rows += [{"window": p.stem, **r} for r in csv.DictReader(f)]
            _write_csv(out_dir / f"{d.name}.csv", rows)
            print(f"{d.name}.csv  <- {len(csvs)} windows, {len(rows):,} rows")
    rows = defaultdict(list)
    for p in sorted((out_dir / "_done").glob("*.json")):
        meta = json.loads(p.read_text())
        for out_name, summary in meta.get("summaries", {}).items():
            rows[out_name].append(dict(zip(("window", "xmin", "ymin", "xmax", "ymax"), (p.stem, *meta["core"])),
                                       **summary))
    for out_name, rs in rows.items():
        _write_csv(out_dir / f"{out_name}.csv", rs)
        print(f"{out_name}.csv  <- {len(rs)} windows")


def _write_csv(path: Path, rows: list[dict]):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(dict.fromkeys(k for r in rows for k in r)))
        w.writeheader()
        w.writerows(rows)


# --------------------------------------------------------------------------- CLI

def _add_area_args(p: argparse.ArgumentParser):
    p.add_argument("--ept", required=True, help="usgs-lidar-public project name or ept.json URL")
    g = p.add_argument_group("area: one of --bbox, --bboxes, --list, --all (--bbox also clips --list/--all)")
    g.add_argument("--bbox", type=float, nargs=4, metavar=("XMIN", "YMIN", "XMAX", "YMAX"), help="grid over this box")
    g.add_argument("--bboxes", metavar="CSV", help="one window per row: xmin,ymin,xmax,ymax[,name]")
    g.add_argument("--bbox-crs", default=LONLAT, help="CRS of --bbox/--bboxes (default lon/lat, EPSG:4326)")
    g.add_argument("--list", metavar="FILE", help="grid over the tiles of a USGS LPC download list")
    g.add_argument("--all", action="store_true", help="grid over the whole EPT footprint")
    p.add_argument("--core", type=float, default=1000.0, help="grid window size, m (default 1000)")
    p.add_argument("--srs", help="output CRS (default: UTM zone at the centre of the area)")
    p.add_argument("--order", choices=("rows", "cols"), default="rows")


def _plan_from_args(a, halo: float) -> tuple[list[Window], str]:
    boxes, names = load_bboxes(a.bboxes) if a.bboxes else (None, None)
    return plan(a.ept, halo, bbox=tuple(a.bbox) if a.bbox else None, bboxes=boxes, names=names,
                bbox_crs=a.bbox_crs, tiles=load_tiles(a.list) if a.list else None, everything=a.all,
                core=a.core, srs=a.srs, order=a.order)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("info", help="footprint, size, fields and default CRS of an EPT")
    i.add_argument("--ept", required=True)
    p = sub.add_parser("plan", help="show window count and per-window size")
    _add_area_args(p)
    p.add_argument("--halo", type=float, default=50.0)
    r = sub.add_parser("run", help="run an analysis over the windows")
    r.add_argument("out_dir")
    _add_area_args(r)
    how = r.add_mutually_exclusive_group(required=True)
    how.add_argument("--analysis", metavar="FILE.py", help="Python file defining analyze(points, window)")
    how.add_argument("--exec", metavar='"CMD ..."', help="command run per window as: CMD... WINDOW.laz WORK_DIR")
    r.add_argument("--halo", type=float, help="m; default HALO from the --analysis file (required with --exec)")
    r.add_argument("--dims", help="only fetch these fields, e.g. X,Y,Z,Classification")
    r.add_argument("--resolution", type=float, help="thin to ~this point spacing, m")
    r.add_argument("--limit", type=int, help="only run this many windows (testing)")
    r.add_argument("--prefetch", type=int, default=1)
    r.add_argument("--threads", type=int, default=8)
    m = sub.add_parser("merge", help="mosaic/concatenate per-window outputs")
    m.add_argument("out_dir")
    a = ap.parse_args()

    try:
        if a.cmd == "merge":
            return merge(a.out_dir)
        if a.cmd == "info":
            info, fp = ept_info(a.ept), footprint(a.ept)
            cells = occupied_boxes(a.ept, utm_srs(*_centre(fp)))
            area = sum((b[2] - b[0]) * (b[3] - b[1]) for b in cells) / 1e6
            print(f"{ept_url(a.ept)}\n{info['points']:,} points, stored in {ept_crs(a.ept)}\n"
                  f"footprint lon/lat {', '.join(f'{v:.4f}' for v in fp)}\n"
                  f"default output CRS {utm_srs(*_centre(fp))}\n"
                  f"data in {len(cells)} octree cells, <= {area:,.0f} km^2 (~{info['points'] / area / 1e6:.1f} pts/m^2)\n"
                  f"fields: {', '.join(d['name'] for d in info['schema'])}")
            return
        if a.cmd == "plan":
            windows, srs = _plan_from_args(a, a.halo)
            area = (a.core + 2 * a.halo) ** 2 / 1e6
            print(f"{len(windows)} windows of {a.core:g} m + {a.halo:g} m halo in {srs}")
            print(f"per window ~{area:.3f} km^2 fetched; area overhead {area / (a.core / 1000) ** 2:.2f}x")
            return
        if a.analysis:
            module = load_analysis(a.analysis)
            halo = a.halo if a.halo is not None else getattr(module, "HALO", None)
            if halo is None:
                ap.error(f"--halo is required: {a.analysis} has no HALO")
            windows, srs = _plan_from_args(a, halo)
            analyze, codes = module.analyze, getattr(module, "CODES", None)
        else:
            if a.halo is None:
                ap.error("--halo is required with --exec")
            windows, srs = _plan_from_args(a, a.halo)
            analyze, codes = exec_analysis(a.exec, a.out_dir, srs), None
        run(windows, analyze, a.out_dir, ept=a.ept, srs=srs, dims=tuple(a.dims.split(",")) if a.dims else None,
            resolution=a.resolution, prefetch=a.prefetch, threads=a.threads, limit=a.limit, codes=codes)
    except (ValueError, RuntimeError) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    # analyses do `import tilestream`; make that this module, not a second copy
    sys.modules.setdefault("tilestream", sys.modules[__name__])
    main()
