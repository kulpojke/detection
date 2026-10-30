"""Shared fixtures. Network tests need an EPT and pick their own test site inside it:

    pytest --ept WA_NorthEast_2_B22        # any usgs-lidar-public project name or ept.json URL
    pytest                                 # offline tests only; network tests are skipped
"""
from types import SimpleNamespace

import numpy as np
import pytest

import tilestream as ts


def pytest_addoption(parser):
    parser.addoption("--ept", help="EPT for the network tests (project name or ept.json URL)")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--ept"):
        return
    skip = pytest.mark.skip(reason="network test: pass --ept NAME")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def ept(request):
    return request.config.getoption("--ept")


@pytest.fixture(scope="session")
def site(ept):
    """A 500 m square test area with data in all four 250 m quarters, as rugged as
    possible (tall objects or steep ground give the seam tests something to break),
    found from thinned reads of the octree cells nearest the middle of the footprint.

    Fields: srs, area (UTM, aligned to the 250 m grid), lonlat (enclosing lon/lat box).
    """
    srs = ts.utm_srs(*ts._centre(ts.footprint(ept)))
    cx, cy = ts._centre(ts.footprint(ept, srs))
    cells = sorted(ts.occupied_boxes(ept, srs), key=lambda b: np.hypot(ts._centre(b)[0] - cx, ts._centre(b)[1] - cy))
    for cell in cells[:10]:
        area, score = _best_area(ept, srs, cell)
        if area:
            print(f"\ntest site in {ept}: {area} ({srs}), relief score {score:.0f} m")
            return SimpleNamespace(srs=srs, area=area, lonlat=ts.transform_bounds(area, srs, ts.LONLAT))
    pytest.fail(f"no 500 m area with data found in the 10 octree cells nearest the middle of {ept}")


def _best_area(ept, srs, cell, q=250.0):
    """The 2q x 2q square (on the q grid) in ``cell`` whose four quarters are all well
    filled, with the most relief; (None, 0) if there is none."""
    mx, my = ts._centre(cell)
    half = min(1500.0, (cell[2] - cell[0]) / 2, (cell[3] - cell[1]) / 2)
    x0, y0 = np.floor((mx - half) / q) * q, np.floor((my - half) / q) * q
    n = int(np.ceil(2 * half / q))
    pts = ts.fetch_points(ept, (x0, y0, x0 + n * q, y0 + n * q), srs, dims=("X", "Y", "Z"), resolution=16)
    if len(pts) == 0:
        return None, 0
    i, j = ((pts["X"] - x0) // q).astype(int), ((pts["Y"] - y0) // q).astype(int)
    count = np.zeros((n, n), int)
    np.add.at(count, (i, j), 1)
    # compare with well-filled quarters, not all of them: a cell at the data edge is mostly empty
    full = count >= max(20, 0.6 * np.percentile(count[count > 0], 75))
    relief = np.zeros((n, n))
    for a, b in zip(*np.nonzero(full)):
        z = pts["Z"][(i == a) & (j == b)]
        relief[a, b] = np.percentile(z, 99) - np.percentile(z, 1)
    best = max(((relief[a:a + 2, b:b + 2].sum(), a, b) for a in range(n - 1) for b in range(n - 1)
                if full[a:a + 2, b:b + 2].all()), default=None)
    if best is None:
        return None, 0
    score, a, b = best
    return (x0 + a * q, y0 + b * q, x0 + (a + 2) * q, y0 + (b + 2) * q), score
