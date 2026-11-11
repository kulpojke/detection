#!/usr/bin/env python3
"""Building footprints from 3DEP lidar over an area, in one command (buildings.py through tilestream).

    python src/detect.py OUT --ept EPT [EPT ...] --area AREA.geojson [--naip NAIP.tif] [--before DATE] [--jobs 12]
    python src/detect.py OUT --ept EPT --bbox XMIN YMIN XMAX YMAX [--bbox-crs EPSG:4326] ...

Rerun the same command to resume: finished windows are skipped, the later steps run again.

Steps:
  1. plan     squares of --core m on a grid in the output CRS (NAD83(2011) UTM at the area's
              centre) that touch the area; for each EPT the squares where it has data (its
              octree cells, ~2 km, so generous at the edges: an empty square costs little)
  2. run      buildings.py over each EPT's squares with tilestream, --jobs processes at once
              (OUT/lidar/<EPT>/, logs in OUT/logs/)
  3. combine  the footprints and crowns into OUT/footprints.gpkg and OUT/crowns.gpkg (column
              lidar): where two EPTs have points in a square, from both (keep_both, join_parts);
              each square's rasters into OUT/<name>.vrt and its row in OUT/windows.gpkg (which
              EPT, how many points, when flown) from the EPT with the most points there
  4. dates    when each 10 m of each EPT was flown (OUT/flown/<EPT>.tif), and so each footprint
              (flown_first, flown_last); with --before, OUT/flown.gpkg: where before, where after
  5. naip     with --naip: NAIP's green on each footprint's roof (buildings.naip_green)
  6. README   OUT/buildings.gpkg (the footprints with building > 0; with --before, only those
              flown before it), and OUT/README.md: where everything came from and what the
              columns mean (--note adds lines about the inputs, e.g. where the area came from)
  7. share    OUT/share/<OUT's name>.zip: the README, buildings, footprints, windows, flown and
              inputs/ (without the rasters and crowns, which can be big)
"""
import argparse
import csv
import datetime
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import tilestream as ts  # noqa: E402  (sets PROJ_DATA and GDAL_DATA)

RASTERS = ("dtm", "dsm", "ndsm", "segments")  # the rasters combined into OUT/<name>.vrt


def short_name(ept: str) -> str:
    """The project name of an EPT given by name or by its ept.json URL."""
    return ept.rstrip("/").removesuffix("/ept.json").rsplit("/", 1)[-1]


def gps_time(t: float) -> str:
    """Adjusted standard GPS time (3DEP's) as a date and time. GPS time runs 18 s ahead of UTC (2017-)."""
    return (datetime.datetime(1980, 1, 6) + datetime.timedelta(seconds=t + 1e9)).strftime("%Y-%m-%d %H:%M")


GPS_TO_UNIX = 1e9 + 315964800  # adjusted standard GPS time + this = seconds since 1970-01-01 (and 18 leap s)
FLOWN_CELL = 10.0  # m, the cells of the flight-date maps


def day(days: float) -> str:
    """Days since 1970-01-01 as a date, YYYY-MM-DD."""
    return str(np.datetime64(int(np.floor(days)), "D"))


def _flown_square(job):
    """x, y and when flown (days since 1970-01-01) of an EPT's points in one square, thinned to about 5 m."""
    ept, box, srs = job
    pts = ts.fetch_points(ept, box, srs, dims=("X", "Y", "GpsTime"), resolution=5.0, threads=2)
    return pts["X"], pts["Y"], ((pts["GpsTime"] + GPS_TO_UNIX) / 86400).astype(np.float32)


def flown_map(ept, boxes, srs, path, jobs, cell=FLOWN_CELL):
    """When each ``cell`` m of an EPT was flown, from its GpsTime read thinned to about 5 m over the
    squares ``boxes``: a GeoTIFF of 2 bands, the first and the last point in each cell (days since
    1970-01-01; NaN where it has no points). A set can be flown on several dates, in parts (B4 2018
    over the Camp Fire: a strip in July 2018, the rest after the fire)."""
    from multiprocessing import Pool
    from osgeo import gdal, osr
    x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
    x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
    nx, ny = round((x1 - x0) / cell), round((y1 - y0) / cell)
    first = np.full(ny * nx, np.inf, np.float32)
    last = np.full(ny * nx, -np.inf, np.float32)
    with Pool(jobs) as pool:
        for x, y, days in pool.imap_unordered(_flown_square, [(ept, b, srs) for b in boxes]):
            i = (ny - 1 - (y - y0) // cell).astype(int) * nx + ((x - x0) // cell).astype(int)  # row 0 at the top
            np.minimum.at(first, i, days)
            np.maximum.at(last, i, days)
    part = path.with_suffix(".part.tif")
    ds = gdal.GetDriverByName("GTiff").Create(str(part), nx, ny, 2, gdal.GDT_Float32, ["COMPRESS=DEFLATE"])
    ds.SetGeoTransform((x0, cell, 0, y1, 0, -cell))
    sr = osr.SpatialReference()
    sr.SetFromUserInput(srs)
    ds.SetProjection(sr.ExportToWkt())
    for k, (a, what) in enumerate(((first, "first"), (last, "last")), 1):
        a[np.isinf(a)] = np.nan
        ds.GetRasterBand(k).WriteArray(a.reshape(ny, nx))
        ds.GetRasterBand(k).SetNoDataValue(float("nan"))
        ds.GetRasterBand(k).SetDescription(f"{what} point flown, days since 1970-01-01")
    ds = None
    part.rename(path)


def add_flown(footprints, layer, maps):
    """Columns flown_first, flown_last (dates) on each object of ``layer``: the first and the last
    point of its lidar set(s) in the cells of their flown_map (``maps``: {set: GeoTIFF}) under its
    outline's corners and its x, y. NULL where the maps have no points there."""
    from osgeo import gdal, ogr
    grids = {}
    for name, path in maps.items():
        ds = gdal.Open(str(path))
        grids[name] = (ds.GetGeoTransform(), ds.GetRasterBand(1).ReadAsArray(), ds.GetRasterBand(2).ReadAsArray())
    ds = ogr.Open(str(footprints), 1)
    lay = ds.GetLayer(layer)
    for col in ("flown_first", "flown_last"):
        if lay.GetLayerDefn().GetFieldIndex(col) < 0:
            lay.CreateField(ogr.FieldDefn(col, ogr.OFTString))
    ds.StartTransaction()
    for f in lay:
        polygons = ogr.ForceToMultiPolygon(f.GetGeometryRef().Clone())
        x, y = np.array([(f["x"], f["y"])] + [p[:2] for poly in polygons for p in poly.GetGeometryRef(0).GetPoints()]).T
        firsts, lasts = [], []
        for name in f["lidar"].split(" + "):
            (gx, rx, _, gy, _, ry), first, last = grids[name]
            i, j = ((y - gy) // ry).astype(int), ((x - gx) // rx).astype(int)
            ok = (i >= 0) & (i < first.shape[0]) & (j >= 0) & (j < first.shape[1])
            firsts += list(first[i[ok], j[ok]])
            lasts += list(last[i[ok], j[ok]])
        if np.isfinite(firsts).any():
            f["flown_first"], f["flown_last"] = day(np.nanmin(firsts)), day(np.nanmax(lasts))
            lay.SetFeature(f)
    ds.CommitTransaction()
    ds = None


def flown_areas(out, maps, before, srs, area_lonlat):
    """OUT/flown.gpkg: where the lidar sets (together) were flown before ``before`` (a date) and
    where only after, as polygons of the maps' cells; column period: 'before', 'after' or 'both'
    (cells with points of both: mostly along the edge between two flights). Returns the km² of
    each period, of all the maps and of the part inside the area."""
    from osgeo import gdal, ogr, osr
    infos = [gdal.Info(str(p), format="json")["cornerCoordinates"] for p in maps.values()]
    x0, y1 = min(c["upperLeft"][0] for c in infos), max(c["upperLeft"][1] for c in infos)
    x1, y0 = max(c["lowerRight"][0] for c in infos), min(c["lowerRight"][1] for c in infos)
    cut = (np.datetime64(before) - np.datetime64("1970-01-01")).astype(int)
    pre = post = False
    for p in maps.values():
        ds = gdal.Warp("", str(p), format="MEM", outputBounds=(x0, y0, x1, y1), xRes=FLOWN_CELL, yRes=FLOWN_CELL,
                       resampleAlg="near", dstNodata=float("nan"))  # NaN outside this set's map
        first, last = ds.GetRasterBand(1).ReadAsArray(), ds.GetRasterBand(2).ReadAsArray()
        pre, post = pre | (first < cut), post | (last >= cut)  # NaN (no points) is neither
    code = pre * 1 + post * 2  # 1 before, 2 after, 3 both
    sr = osr.SpatialReference()
    sr.SetFromUserInput(srs)
    mem = gdal.GetDriverByName("MEM").Create("", code.shape[1], code.shape[0], 2, gdal.GDT_Byte)
    mem.SetGeoTransform((x0, FLOWN_CELL, 0, y1, 0, -FLOWN_CELL))
    mem.SetProjection(sr.ExportToWkt())
    mem.GetRasterBand(1).WriteArray(code.astype(np.uint8))
    # the area on the same grid, for its share of each period
    lonlat = osr.SpatialReference()
    lonlat.ImportFromEPSG(4326)
    for s in (lonlat, sr):
        s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    area = area_lonlat.Clone()
    area.Transform(osr.CoordinateTransformation(lonlat, sr))
    vds = ogr.GetDriverByName("MEM").CreateDataSource("area")
    vl = vds.CreateLayer("area", sr)
    f = ogr.Feature(vl.GetLayerDefn())
    f.SetGeometry(area)
    vl.CreateFeature(f)
    gdal.RasterizeLayer(mem, [2], vl, burn_values=[1])
    inside = mem.GetRasterBand(2).ReadAsArray() == 1
    km2 = FLOWN_CELL ** 2 / 1e6
    shares = {"area": round(area.GetArea() / 1e6, 2)}
    for k, period in ((1, "before"), (2, "after"), (3, "both")):
        shares[period] = (round(float((code == k).sum() * km2), 2), round(float(((code == k) & inside).sum() * km2), 2))
    (out / "flown.gpkg").unlink(missing_ok=True)
    pds = ogr.GetDriverByName("GPKG").CreateDataSource(str(out / "flown.gpkg"))
    pl = pds.CreateLayer("flown", sr, ogr.wkbPolygon)
    pl.CreateField(ogr.FieldDefn("code", ogr.OFTInteger))
    pl.CreateField(ogr.FieldDefn("period", ogr.OFTString))
    band = mem.GetRasterBand(1)
    gdal.Polygonize(band, band, pl, 0)  # the band is its own mask: 0 (no lidar) left out
    pds.ExecuteSQL("UPDATE flown SET period = CASE code WHEN 1 THEN 'before' WHEN 2 THEN 'after' ELSE 'both' END")
    pds = None
    return shares


def read_area(a):
    """The area as one ogr polygon in lon/lat (traditional x = lon order)."""
    from osgeo import ogr, osr
    ogr.UseExceptions()
    lonlat = osr.SpatialReference()
    lonlat.ImportFromEPSG(4326)
    lonlat.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    if a.bbox:
        x0, y0, x1, y1 = ts.transform_bounds(a.bbox, a.bbox_crs, ts.LONLAT)
        return ogr.CreateGeometryFromWkt(f"POLYGON(({x0} {y0},{x1} {y0},{x1} {y1},{x0} {y1},{x0} {y0}))")
    ds = ogr.Open(a.area)
    lay = ds.GetLayer()
    area = None
    for f in lay:
        g = f.GetGeometryRef().Clone()
        area = g if area is None else area.Union(g)
    sr = lay.GetSpatialRef()
    if sr is not None:
        sr.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        area.Transform(osr.CoordinateTransformation(sr, lonlat))
    return area


def plan_squares(area_lonlat, epts, core):
    """The output CRS, and for each EPT its squares {name: (xmin, ymin, xmax, ymax)} that touch the area."""
    import math
    from osgeo import ogr, osr
    x0, x1, y0, y1 = area_lonlat.GetEnvelope()
    srs = ts.utm_srs((x0 + x1) / 2, (y0 + y1) / 2)
    lonlat, out = osr.SpatialReference(), osr.SpatialReference()
    lonlat.ImportFromEPSG(4326)
    out.SetFromUserInput(srs)
    for s in (lonlat, out):
        s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    area = area_lonlat.Clone()
    area.Transform(osr.CoordinateTransformation(lonlat, out))
    ax0, ax1, ay0, ay1 = area.GetEnvelope()
    touching = set()
    for i in range(math.floor(ax0 / core), math.ceil(ax1 / core)):
        for j in range(math.floor(ay0 / core), math.ceil(ay1 / core)):
            b = (i * core, j * core, (i + 1) * core, (j + 1) * core)
            box = ogr.CreateGeometryFromWkt(f"POLYGON(({b[0]} {b[1]},{b[2]} {b[1]},{b[2]} {b[3]},{b[0]} {b[3]},{b[0]} {b[1]}))")
            if area.Intersects(box):
                touching.add((i, j))
    squares = {}
    for ept in epts:
        has_data = {(i, j) for w in ts.grid_windows(ts.occupied_boxes(ept, srs), core, 0)
                    for i, j in [(round(w.core[0] / core), round(w.core[1] / core))]}
        squares[ept] = {f"{i * core:.0f}_{j * core:.0f}": (i * core, j * core, (i + 1) * core, (j + 1) * core)
                        for i, j in sorted(touching & has_data, key=lambda ij: (ij[1], ij[0]))}
    return srs, squares, len(touching)


def run_all(out, epts, squares, srs, jobs):
    """tilestream run for every EPT, its squares split into chunks, at most ``jobs`` processes at once.
    A chunk that fails (e.g. the network) is tried again, up to 3 times; tilestream resumes it."""
    (out / "plan").mkdir(parents=True, exist_ok=True)
    (out / "logs").mkdir(exist_ok=True)
    tasks = []
    for ept in epts:
        names = list(squares[ept])
        size = max(1, -(-len(names) // jobs))
        for k in range(0, len(names), size):
            plan_csv = out / "plan" / f"{short_name(ept)}_{k // size:02d}.csv"
            with open(plan_csv, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["xmin", "ymin", "xmax", "ymax", "name"])
                w.writerows([*squares[ept][n], n] for n in names[k:k + size])
            tasks.append((ept, plan_csv))
    env = {**os.environ, "PYTHONNOUSERSITE": "1"}
    running, tries = {}, {t: 0 for t in tasks}
    queue = list(tasks)
    while queue or running:
        while queue and len(running) < jobs:
            ept, plan_csv = task = queue.pop(0)
            tries[task] += 1
            log = open(out / "logs" / f"{plan_csv.stem}.log", "a")
            cmd = [sys.executable, str(HERE / "tilestream.py"), "run", str(out / "lidar" / short_name(ept)),
                   "--ept", ept, "--bboxes", str(plan_csv), "--bbox-crs", srs, "--srs", srs,
                   "--analysis", str(HERE / "buildings.py")]
            running[task] = (subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env), log)
        time.sleep(5)
        for task, (p, log) in list(running.items()):
            if p.poll() is None:
                continue
            log.close()
            del running[task]
            if p.returncode != 0:
                print(f"  {task[1].stem}: exit {p.returncode} (try {tries[task]}); see logs/{task[1].stem}.log", flush=True)
                if tries[task] < 3:
                    queue.append(task)
            else:
                print(f"  {task[1].stem}: done", flush=True)
    return [t for t in tasks if tries[t] >= 3]


def keep_both(out, epts, name, sets, layer):
    """For a square that several EPTs have points in (two sets usually meet there, each covering a
    part): {ept: FIDs to keep} of ``layer``. Every object of every set, but where two overlap by
    more than half of the smaller (the same object seen twice), only the bigger outline: at its
    data's edge a set sees a building cut off. Parts that overlap less are joined (join_parts)."""
    from osgeo import ogr
    feats = []
    for ept in sets:
        p = out / "lidar" / short_name(ept) / layer / f"{name}.gpkg"
        if p.exists():
            ds = ogr.Open(str(p))
            feats += [(f.GetGeometryRef().GetArea(), ept, f.GetFID(), f.GetGeometryRef().Clone()) for f in ds.GetLayer()]
    feats.sort(key=lambda x: (-x[0], epts.index(x[1])))
    env = boxes_of([x[3] for x in feats])
    kept = np.zeros(len(feats), bool)
    for k, (area, ept, fid, g) in enumerate(feats):
        kept[k] = not any(feats[i][1] != ept and overlap(g, feats[i][3]) > 0.5 * min(area, feats[i][0])
                          for i in np.flatnonzero(kept & near(env, env[k])))
    return {ept: [x[2] for x, k in zip(feats, kept) if k and x[1] == ept] for ept in sets}


def overlap(g, h):
    """Area shared by two polygons (touching gives a line or point: no area)."""
    i = g.Intersection(h) if g.Intersects(h) else None
    return i.GetArea() if i is not None and i.GetDimension() == 2 else 0.0


def boxes_of(geoms):
    """Each geometry's bounding box as a row (xmin, xmax, ymin, ymax): a quick test before the exact one."""
    return np.array([g.GetEnvelope() for g in geoms]).reshape(-1, 4)


def near(env, box):
    """Which rows of ``env`` have a bounding box that overlaps ``box``."""
    return (env[:, 0] < box[1]) & (env[:, 1] > box[0]) & (env[:, 2] < box[3]) & (env[:, 3] > box[2])


def join_parts(path, layer, boxes):
    """Where two sets meet, each sees a building cut off at its data's edge, and the two parts
    overlap in a strip. Join the parts from different sets that overlap (by > 1 m²) into the
    bigger one: the outline is their union, `area` and `under_canopy` add up (less the overlap),
    `lidar` names both. Only objects within 50 m of the ``boxes`` (squares with several sets)."""
    from osgeo import ogr
    ds = ogr.Open(str(path), 1)
    lay = ds.GetLayer(layer)
    feats = {}
    for x0, y0, x1, y1 in boxes:
        lay.SetSpatialFilterRect(x0 - 50, y0 - 50, x1 + 50, y1 + 50)
        feats.update({f.GetFID(): f for f in lay})
    lay.SetSpatialFilter(None)
    fids = sorted(feats, key=lambda k: -feats[k].GetGeometryRef().GetArea())
    env = boxes_of([feats[k].GetGeometryRef() for k in fids])
    gone, joined = set(), 0
    for fid in fids:
        if fid in gone:
            continue
        f = feats[fid]
        g = f.GetGeometryRef().Clone()
        changed = True
        while changed:  # the joined outline is bigger: look again for parts that touch it
            changed = False
            for k in np.flatnonzero(near(env, g.GetEnvelope())):
                other, h = fids[k], feats[fids[k]]
                if other == fid or other in gone or h["lidar"] in f["lidar"].split(" + "):
                    continue
                shared = overlap(g, h.GetGeometryRef())
                if shared <= 1.0:
                    continue
                for col in ("area", "under_canopy"):
                    if f.GetFieldIndex(col) >= 0 and h[col] is not None:
                        f[col] = (f[col] or 0) + h[col] - (shared if col == "area" else 0)
                g = g.Union(h.GetGeometryRef())
                f["lidar"] = " + ".join(dict.fromkeys(f["lidar"].split(" + ") + h["lidar"].split(" + ")))  # each set once
                gone.add(other)
                joined += 1
                changed = True
        if g.GetArea() != f.GetGeometryRef().GetArea():
            f.SetGeometry(ogr.ForceToMultiPolygon(g) if lay.GetGeomType() == ogr.wkbMultiPolygon else g)
            lay.SetFeature(f)
    for fid in gone:
        lay.DeleteFeature(fid)
    ds = None
    return joined


def combine(out, epts, squares, srs):
    """Each square from the EPT with the most points there (its rasters and its row in windows.gpkg);
    footprints and crowns from it too, or from every EPT with points there (keep_both)."""
    from osgeo import gdal, ogr, osr
    gdal.UseExceptions()
    done = {}  # square -> [(points, ept, record)]
    for ept in epts:
        for name in squares[ept]:
            p = out / "lidar" / short_name(ept) / "_done" / f"{name}.json"
            if p.exists():
                rec = json.loads(p.read_text())
                done.setdefault(name, []).append((rec["points"], -epts.index(ept), ept, rec))
    chosen = {n: max(c)[2:] for n, c in done.items() if max(c)[0] > 0}  # ties: the first EPT given
    # the squares, as polygons
    sr = osr.SpatialReference()
    sr.SetFromUserInput(srs)
    for name in ("windows.gpkg", "footprints.gpkg", "crowns.gpkg"):
        (out / name).unlink(missing_ok=True)
    wds = ogr.GetDriverByName("GPKG").CreateDataSource(str(out / "windows.gpkg"))
    wl = wds.CreateLayer("windows", sr, ogr.wkbPolygon)
    for field, kind in (("name", ogr.OFTString), ("lidar", ogr.OFTString), ("points", ogr.OFTInteger64),
                        ("first_flown", ogr.OFTString), ("last_flown", ogr.OFTString), ("others", ogr.OFTString)):
        wl.CreateField(ogr.FieldDefn(field, kind))
    for name, (ept, rec) in sorted(chosen.items()):
        b = rec["core"]
        f = ogr.Feature(wl.GetLayerDefn())
        f.SetGeometry(ogr.CreateGeometryFromWkt(f"POLYGON(({b[0]} {b[1]},{b[2]} {b[1]},{b[2]} {b[3]},{b[0]} {b[3]},{b[0]} {b[1]}))"))
        flown = rec.get("summaries", {}).get("flown", {})
        f["name"], f["lidar"], f["points"] = name, short_name(ept), rec["points"]
        if flown:
            f["first_flown"], f["last_flown"] = gps_time(flown["gps_first"]), gps_time(flown["gps_last"])
        f["others"] = ", ".join(f"{short_name(e)} {n:,}" for n, _, e, _ in done[name] if e != ept)
        wl.CreateFeature(f)
    wds = None
    # footprints and crowns: append the chosen windows' files (or, where several EPTs have points,
    # the objects keep_both picks from each), then name their lidar
    shared = {n: [e for points, _, e, _rec in c if points > 0] for n, c in done.items()}
    shared = {n: s for n, s in shared.items() if len(s) > 1}
    for layer in ("footprints", "crowns"):
        target = out / f"{layer}.gpkg"
        picks = {n: keep_both(out, epts, n, s, layer) for n, s in shared.items()}
        for ept in epts:
            for n in sorted(chosen):
                p = out / "lidar" / short_name(ept) / layer / f"{n}.gpkg"
                if n in picks and picks[n].get(ept):
                    gdal.VectorTranslate(str(target), str(p), accessMode="append", layerName=layer,
                                         where=f"FID IN ({','.join(map(str, picks[n][ept]))})")
                elif n not in picks and chosen[n][0] == ept and p.exists():
                    gdal.VectorTranslate(str(target), str(p), accessMode="append", layerName=layer)
            if target.exists():
                ds = ogr.Open(str(target), 1)
                lay = ds.GetLayer(layer)
                if lay.GetLayerDefn().GetFieldIndex("lidar") < 0:
                    lay.CreateField(ogr.FieldDefn("lidar", ogr.OFTString))
                ds.ExecuteSQL(f"UPDATE {layer} SET lidar = '{short_name(ept)}' WHERE lidar IS NULL")
                ds = None
        if shared and target.exists():
            joined = join_parts(target, layer, [done[n][0][3]["core"] for n in shared])
            print(f"   {layer}: {joined} parts cut at the edge between two sets joined", flush=True)
    # rasters: one VRT each over the chosen windows' tiles. Where several sets have points, the
    # others' tiles go first, under the chosen one (a VRT's later sources win): they show where
    # it has no data. Not for the dtm, which is filled everywhere (from the nearest ground point).
    for r in RASTERS:
        under = [] if r == "dtm" else [out / "lidar" / short_name(e) / r / f"{n}.tif"
                                       for n, s in sorted(shared.items()) for e in s if e != chosen[n][0]]
        tifs = under + [out / "lidar" / short_name(e) / r / f"{n}.tif" for n, (e, _) in sorted(chosen.items())]
        tifs = [str(t) for t in tifs if t.exists()]
        if tifs:
            gdal.BuildVRT(str(out / f"{r}.vrt"), tifs)
    return chosen, done


def summary(out, epts, chosen, before=None):
    """Counts and flight dates per EPT, for the README and the screen. ``kept``: the buildings in
    buildings.gpkg; with ``before``, also those flown after it, on the edge between two flights
    (part before, part after) and without a date."""
    from osgeo import ogr
    rows = []
    for ept in epts:
        recs = [rec for e, rec in chosen.values() if e == ept]
        flown = [rec["summaries"]["flown"] for rec in recs if "flown" in rec.get("summaries", {})]
        rows.append({"ept": ept, "squares": len(recs), "points": sum(r["points"] for r in recs),
                     "first": gps_time(min(f["gps_first"] for f in flown)) if flown else "?",
                     "last": gps_time(max(f["gps_last"] for f in flown)) if flown else "?",
                     "unknown_dates": len(recs) - len(flown)})
    counts = {}
    for layer in ("footprints", "crowns"):
        ds = ogr.Open(str(out / f"{layer}.gpkg")) if (out / f"{layer}.gpkg").exists() else None
        lay = ds.GetLayer() if ds else None
        counts[layer] = lay.GetFeatureCount() if lay else 0
        if layer == "footprints" and lay:
            lay.SetAttributeFilter("building > 0")
            counts["buildings"] = counts["kept"] = lay.GetFeatureCount()
            lay.SetAttributeFilter("building = 2 AND height > 12" + (f" AND flown_last < '{before}'" if before else ""))
            counts["tall_small"] = lay.GetFeatureCount()
            if lay.GetLayerDefn().GetFieldIndex("naip_flag") >= 0:
                lay.SetAttributeFilter("building > 0 AND naip_flag = 1" + (f" AND flown_last < '{before}'" if before else ""))
                counts["flagged"] = lay.GetFeatureCount()
            if before:
                for what, where in (("kept", f"flown_last < '{before}'"), ("after", f"flown_first >= '{before}'"),
                                    ("edge", f"flown_first < '{before}' AND flown_last >= '{before}'"),
                                    ("undated", "flown_last IS NULL")):
                    lay.SetAttributeFilter(f"building > 0 AND {where}")
                    counts[what] = lay.GetFeatureCount()
    return rows, counts


def git_version():
    """The code's commit, and whether the working tree had changes (then the results come from those)."""
    try:
        git = ["git", "-C", str(HERE)]
        head = subprocess.run([*git, "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run([*git, "status", "--porcelain", "--", ":/src"], capture_output=True, text=True).stdout.strip()
        return f"{head}{' plus uncommitted changes in src/' if dirty else ''}"
    except OSError:
        return "unknown"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", help="output folder (rerun the same command to resume)")
    ap.add_argument("--ept", nargs="+", required=True, help="usgs-lidar-public project names or ept.json URLs; "
                    "where they overlap, each square comes from the one with the most points (ties: the first)")
    where = ap.add_mutually_exclusive_group(required=True)
    where.add_argument("--area", help="polygons (GeoJSON, GeoPackage, shapefile ...): the squares that touch them")
    where.add_argument("--bbox", type=float, nargs=4, metavar=("XMIN", "YMIN", "XMAX", "YMAX"))
    ap.add_argument("--bbox-crs", default=ts.LONLAT, help="CRS of --bbox (default lon/lat)")
    ap.add_argument("--naip", help="NAIP raster (4 bands: R, G, B, NIR; any CRS): flag green on the roofs")
    ap.add_argument("--before", type=datetime.date.fromisoformat, help="a date, YYYY-MM-DD (e.g. a fire's "
                    "start): buildings.gpkg keeps only the buildings flown before it; flown.gpkg shows where")
    ap.add_argument("--core", type=float, default=500.0, help="square size, m (default 500)")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2), help="processes at once")
    ap.add_argument("--note", action="append", default=[], help="a line for the README's inputs (repeatable)")
    a = ap.parse_args()
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    started = datetime.datetime.now()

    print("1. plan", flush=True)
    area = read_area(a)
    srs, squares, n_area = plan_squares(area, a.ept, a.core)
    for ept in a.ept:
        print(f"   {short_name(ept)}: {len(squares[ept])} of the {n_area} squares that touch the area", flush=True)
    print(f"2. run ({a.jobs} processes; progress in {out / 'logs'})", flush=True)
    failed = run_all(out, a.ept, squares, srs, a.jobs)
    print("3. combine", flush=True)
    chosen, done = combine(out, a.ept, squares, srs)
    print("4. dates", flush=True)
    (out / "flown").mkdir(exist_ok=True)
    maps = {short_name(e): out / "flown" / f"{short_name(e)}.tif" for e in a.ept}
    for ept in a.ept:
        if not maps[short_name(ept)].exists():
            flown_map(ept, list(squares[ept].values()), srs, maps[short_name(ept)], a.jobs)
    add_flown(out / "footprints.gpkg", "footprints", maps)
    shares = flown_areas(out, maps, a.before.isoformat(), srs, area) if a.before else None
    if a.naip:
        print("5. naip", flush=True)
        import buildings
        buildings.naip_green(out / "footprints.gpkg", a.naip, out / "segments.vrt")
    print("6. buildings.gpkg, README", flush=True)
    from osgeo import gdal
    (out / "buildings.gpkg").unlink(missing_ok=True)
    keep = "building > 0" + (f" AND flown_last < '{a.before}'" if a.before else "")
    gdal.VectorTranslate(str(out / "buildings.gpkg"), str(out / "footprints.gpkg"), where=keep, layerName="buildings")
    rows, counts = summary(out, a.ept, chosen, a.before)
    write_readme(out, a, srs, n_area, squares, rows, counts, failed, started, shares)
    print("7. share", flush=True)
    share(out)
    print(f"{counts.get('kept', 0):,} buildings in {out / 'buildings.gpkg'}"
          + (f"; {len(failed)} chunks failed 3 times: rerun to resume" if failed else ""), flush=True)


def share(out):
    """OUT/share/<name>.zip: what someone else needs (the rasters and crowns stay in OUT)."""
    import zipfile
    (out / "share").mkdir(exist_ok=True)
    target = out / "share" / f"{out.name}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for name in ("README.md", "buildings.gpkg", "footprints.gpkg", "windows.gpkg", "flown.gpkg"):
            if (out / name).exists():
                z.write(out / name, f"{out.name}/{name}")
        for f in sorted((out / "inputs").glob("*")) if (out / "inputs").exists() else []:
            z.write(f, f"{out.name}/inputs/{f.name}")
    return target


def write_readme(out, a, srs, n_area, squares, rows, counts, failed, started, shares=None):
    from osgeo import gdal
    import buildings
    eptlines = "\n".join(
        f"| `{short_name(r['ept'])}` | {ts.ept_url(r['ept'])} | {r['squares']:,} | {r['points']:,} | {r['first']} | {r['last']} |"
        for r in rows)
    unknown = sum(r["unknown_dates"] for r in rows)
    naip = ""
    if a.naip:
        info = gdal.Info(a.naip, format="json")
        naip = (f"- **NAIP:** `{a.naip}` ({info['size'][0]:,} × {info['size'][1]:,} pixels of "
                f"{abs(info['geoTransform'][1]):.2f} m, {len(info['bands'])} bands). Used only for the "
                f"`naip_green` / `naip_flag` columns.\n")
    notes = "".join(f"- {n}\n" for n in a.note)
    naip_limit = ""
    if a.naip and counts.get("kept"):
        naip_limit = (f"- `naip_flag` is set for {counts.get('flagged', 0):,} of the {counts['kept']:,} buildings "
                      f"({counts.get('flagged', 0) / counts['kept']:.0%}). NAIP is true to the ground only, so tall "
                      f"trees appear leaning by metres, and next to tall trees a roof gets their green: there many "
                      f"roofs are flagged that hold no tree. A hint for a look, not a verdict.\n")
    b = a.before.isoformat() if a.before else None
    dates = keep = after = ""
    if shares:
        def pct(period):
            return f"{shares[period][1]:,.1f} km² ({shares[period][1] / shares['area']:.0%})"
        dates = (f"- **Before {b} (`--before`):** a lidar set can be flown on several dates, in parts. "
                 f"`flown/<set>.tif` maps when each {FLOWN_CELL:g} m was flown (the first and the last point, from "
                 f"the points' GPS times read thinned to about 5 m), and `flown.gpkg` shows where the sets together "
                 f"were flown before {b} (`period` 'before'), only after it ('after') or both (along the edge "
                 f"between two flights). Of the area's {shares['area']:,.1f} km², {pct('before')} were flown "
                 f"before {b}, {pct('after')} only after it and {pct('both')} both; "
                 f"the rest has no lidar. **Where the lidar was flown after {b}, the buildings of before are "
                 f"missing**: it saw only those still standing then, and they are left out of `buildings.gpkg`.\n")
        keep = f" flown before {b}"
        after = (f" Its buildings also include {counts.get('after', 0):,} flown after {b} (still standing then), "
                 f"{counts.get('edge', 0):,} on the edge between two flights (part of their lidar before, part "
                 f"after: check them) and {counts.get('undated', 0):,} without a date.")
    text = f"""# Building footprints: {out.name}

Made {datetime.datetime.now():%Y-%m-%d %H:%M} (started {started:%Y-%m-%d %H:%M}) with `src/detect.py` from
<https://github.com/kulpojke/detection>, code version {git_version()}. This is a first, automatic
result (a test case): nobody has checked these footprints one by one.

    {shlex.join(['python', 'src/detect.py', *sys.argv[1:]])}

## Where the data came from

- **Lidar:** USGS 3D Elevation Program (3DEP) point clouds, read as Entwine Point Tiles (EPT) from
  the public USGS bucket on AWS (`s3://usgs-lidar-public`), in squares of {a.core:g} m. Where two
  sets have points in a square (they usually meet there), its footprints and crowns come from both:
  where both found the same object, the bigger outline (a set sees a building cut off at its data's
  edge), and where each saw part of a building, the parts joined. Each feature's `lidar` column
  says which set(s) it came from. `windows.gpkg` (column `lidar`) and the rasters take each square
  from the set with the most points there (in the rasters, the other set shows where it has none).

| lidar set | EPT | squares | points | first flown | last flown |
| --- | --- | --- | --- | --- | --- |
{eptlines}

  Flight times are the points' GPS times (GPS time, 18 s ahead of UTC), per square in `windows.gpkg`
  (`first_flown`, `last_flown`; they include the {buildings.HALO:g} m border read around each square).
  {f"{unknown} squares have no GPS time." if unknown else ""}
- **Area:** {f"`{a.area}`" if a.area else f"the box {a.bbox} ({a.bbox_crs})"}: the {n_area:,} squares of
  {a.core:g} m that touch it; {sum(r['squares'] for r in rows):,} of them have lidar.
{dates}{naip}{notes}
## What is here

- **`buildings.gpkg`: the {counts.get('kept', 0):,} buildings{keep}** (the footprints with `building` > 0{f" and `flown_last` before {b}" if b else ""}).
- `footprints.gpkg`: all {counts.get('footprints', 0):,} roof-seeded objects, with those the rule turned
  down (`building` = 0, mostly trees), for checking.{after}
- `crowns.gpkg`: {counts.get('crowns', 0):,} objects that aren't buildings (mostly tree crowns), at least
  {buildings.CROWN_MIN_AREA:g} m².
- `windows.gpkg`: the squares, which lidar set each came from, its point count and when it was flown.
{"- `flown.gpkg`: where the lidar was flown before " + b + ", after it, or both." + chr(10) if shares else ""}- `flown/<set>.tif`: when each {FLOWN_CELL:g} m of each lidar set was flown (2 bands: the first and the last
  point, days since 1970-01-01).
- `inputs/`: the area (and other inputs) as used.
- `share/{out.name}.zip`: the README, buildings, footprints, windows{", flown" if shares else ""} and inputs, to pass on. The
  rest (crowns, rasters, `lidar/`) is only in the full folder.
- `dtm.vrt`, `dsm.vrt`, `ndsm.vrt` (ground, top and height above ground, 0.5 m) and `segments.vrt`
  (1 roof, 2 tree, 3 roof found under a tree), over the tiles in `lidar/`.
{"- `code/`: the code the squares were analysed with (a copy, made before the run)." + chr(10) if (out / "code").exists() else ""}- `lidar/<set>/`: the raw per-square output of each lidar set (all squares it ran, also those the
  other set won). `logs/`, `plan/`: the run's progress and squares.

All layers are in **{srs}** (NAD83(2011) / UTM; metres).

## Footprint columns (the main ones)

- `building`: 1 building, 2 shed, container or RV (a building under 40 m²), 0 not a building.
- `area`: m² of roof seen from above (the lidar's 0.5 m pixels). The polygon can be a little
  smaller (walls moved onto the roof's edge) or bigger (`under_canopy`).
- `under_canopy`: m² of roof found under overhanging trees, in the polygon but not in `area`.
- `height`: median height of the roof above the ground, m. `top`, `h_p95`: higher points.
- `outline`: how the polygon was made: 0 straight walls; other codes keep the pixel outline or a
  simplified one (the codes are stored in the file; QGIS shows their labels).
- `review`: 1 = a tree hides part of the roof (a notch): the footprint may be too small there.
{"- `naip_green`: m² of NAIP green (NDVI > " + f"{buildings.NAIP_GREEN_NDVI:g}" + ") on the visible roof, away from its edge; `naip_flag` = 1 when that is at least " + f"{buildings.NAIP_FLAG_M2:g}" + " m²: perhaps a tree taken as roof (NAIP leans tall trees by metres, so check)." + chr(10) if a.naip else ""}- `flown_first`, `flown_last`: when the lidar under the object was flown (dates, UTC): the first and
  the last point in the {FLOWN_CELL:g} m cells of `flown/<set>.tif` under its corners and its x, y.
- `x`, `y`: a point inside the object; `lidar`: the lidar set(s) it came from.
- The rest (`hole_*`, `rectangularity`, `multi_return`, `roughness*`, `n_points`, `h_*`, `cover*`,
  `i_*`, `walls_moved`, `wall_shift`) are the features the building rule and the checks use; see
  the docstring of `src/buildings.py` and `theoretical_basis.md` in the repository.

## Known limits

- Tuned and checked by hand on two sites in Washington (Chewelah, a town; Nine Mile Falls,
  forest with houses), at 10–15 pulses/m². Paradise pre-fire (B4) has ~10 pulses/m².
- Roof hidden under dense crowns is missing from the footprint; `review` marks the notches it
  leaves. Very small sheds without a covered hole in the ground points can be missed.
- Where two lidar sets meet, an object both saw keeps the bigger outline, and the parts of a
  building cut by the edge of one set's data are joined (`lidar` names both): check these.
- A building is decided by the square its point lies in, read with a {buildings.HALO:g} m border: one
  that reaches further out of its square than that is cut.
- Bridges, and power-line wires over canyons and water, can come out as buildings: smooth, with no
  ground under them. {counts.get('tall_small', 0):,} code 2 buildings (sheds, containers, RVs) stand more than 12 m
  above the ground, which those don't: most are pieces of wire. Drop them with
  `NOT (building = 2 AND height > 12)`. Bridges are big code 1 objects high above a river or road.
{naip_limit}"""
    if failed:
        text += "\n**Unfinished:** " + ", ".join(t[1].stem for t in failed) + " failed 3 times; rerun to resume.\n"
    (out / "README.md").write_text(text)


if __name__ == "__main__":
    sys.modules.setdefault("detect", sys.modules[__name__])
    main()
