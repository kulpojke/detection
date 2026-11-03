#!/usr/bin/env python3
"""Example Python analysis for tilestream: a 1 m DSM plus local-maximum detection.

Swap ``analyze`` for your own detector. The only contract:
  * take ``(points, window)``; points cover window.halo, a structured array with
    PDAL dimension names (X, Y, Z, Classification, ReturnNumber, ...)
  * return {name: Raster | table | summary}; build rasters on window.grid(res),
    give tables "x"/"y" columns, make summaries a dict of scalars (one row per
    window, handy for bbox site checks). Rasters and tables are cropped to window.core.
  * HALO must cover the analysis' spatial reach (here the 11 px filters -> 5.5 m).

    python src/tilestream.py run out/ --ept WA_NorthEast_2_B22 --bbox -117.42 47.40 -117.38 47.42 \\
        --analysis src/example_peaks.py
    python src/tilestream.py run out_sites/ --ept WA_NorthEast_2_B22 --bboxes sites.csv --analysis src/example_peaks.py
    python src/tilestream.py merge out/
"""
import numpy as np
from scipy import ndimage

from tilestream import Raster

RES = 1.0           # m
FOOT = 11           # px, local-maximum / prominence neighbourhood
MIN_PROMINENCE = 3  # m above the neighbourhood minimum
HALO = 20.0         # m, >= FOOT * RES / 2 with margin


def analyze(pts, w):
    x0, ytop, nx, ny = w.grid(RES)
    keep = (pts["ReturnNumber"] == 1) & ~np.isin(pts["Classification"], (7, 18))  # first returns, no noise
    col = ((pts["X"][keep] - x0) / RES).astype(np.int64)
    # a point exactly on the grid's bottom edge would get row ny, one past the last row
    row = np.minimum(((ytop - pts["Y"][keep]) / RES).astype(np.int64), ny - 1)
    dsm = np.full((ny, nx), -np.inf, np.float32)
    np.maximum.at(dsm, (row, col), pts["Z"][keep].astype(np.float32))
    empty = ~np.isfinite(dsm)

    filled = np.where(empty, np.nanmin(np.where(empty, np.nan, dsm)) if (~empty).any() else 0, dsm)
    prominence = filled - ndimage.minimum_filter(filled, FOOT)
    peak = (filled == ndimage.maximum_filter(filled, FOOT)) & (prominence >= MIN_PROMINENCE) & ~empty
    r, c = np.nonzero(peak)

    dsm[empty] = np.nan
    return {
        "dsm": Raster(dsm, x0, ytop, RES, nodata=np.nan),
        "peaks": {"x": x0 + (c + 0.5) * RES, "y": ytop - (r + 0.5) * RES,
                  "z": dsm[r, c], "prominence": prominence[r, c]},
        "stats": {"n_peaks_in_core": int(w.in_core(x0 + (c + 0.5) * RES, ytop - (r + 0.5) * RES).sum()),
                  "max_z": float(np.nanmax(dsm)) if (~empty).any() else float("nan")},
    }

