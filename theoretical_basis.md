# Theoretical basis

How `src/tilestream.py` and `src/buildings.py` work, and why each step is justified. Constants
are given with their values. The numbers in brackets come from the two test sites in
WA_NorthEast_2_B22, at 12–15 pulses/m²: Chewelah (a town) and Nine Mile Falls (forest with
houses). `HANDOFF.md` has the history and `test/README.md` lists what each test checks.

State on 2026-11-10: everything described here is in the code. Since run 28 the walls of
straightened buildings move to where the points put them (section 7), and since run 29 a small
roof seed piece whose flood is a crown makes a tree (section 3). `detect.py` runs a whole area,
joins lidar sets where they meet, maps when each part was flown and looks at NAIP's green on the
roofs (section 8).

---

## 1. Reading lidar in windows (tilestream)

**The problem.** A 3DEP project is terabytes. An analysis has to run over areas far bigger
than memory, and its result must not depend on how the area was cut up.

```mermaid
flowchart LR
  A["Area: bbox, download list or whole EPT"] --> B["Core windows on a grid, in UTM metres"]
  B --> C["Fetch core + halo from the EPT octree (PDAL)"]
  C --> D["analyze(points, window)"]
  D --> E["Crop every output to the core"]
  E --> F["Files per window"]
  F --> G["merge: raster mosaics (VRT), one table, one GeoPackage"]
```

An EPT is an octree of LAZ tiles. A box query reads only the nodes that touch the box, so a
window streams straight from S3 and nothing big lands on disk. The next window is fetched in a
background thread while the current one is analysed.

**Why it has no seams.** Suppose the analysis has a *reach* r: its output at a location p
depends only on points within r of p. A window fetches its core plus a halo h around it. For
every p in the core, all points within r of p are fetched if h ≥ r. Then the window's output at
p is exactly what one run over the whole area would give. Cores tile the plane with half-open
bounds (x0 ≤ x < x1), so each location is produced once. Raster grids are snapped to global
multiples of the pixel size, so the crop falls on pixel edges. Table rows and polygons are kept
by their point (x, y), so a polygon that reaches into the next window is written whole, by one
window only.

```
+-------------------------------+
| halo: fetched, not kept       |
|   +-----------------------+   |
|   | core: kept            |   |
|   |   * an object's point |   |
|   +-----------------------+   |
+-------------------------------+
```

For objects, the reach is the object's own size: how a building is cut out depends on all of
it. So `buildings.py` uses h = 50 m (objects up to ~100 m across), and gives each object one
point inside it: for a roof, its pixel furthest from any ground point; for a tree, its top. The
window that owns that point owns the object.

The seam tests taught two more requirements:

- **Results must not depend on where in a window something lies.** Ties are broken by pixel
  position (raster order), which is the same in every window. They are never broken by label
  numbers, which depend on what else is in the window.
- **Results must not depend on the order of the points**, which differs between windows. A
  float sum depends on order. Counts, minima, sorted sums and integer sums don't, so those are
  what the code uses.

The check: four 250 m windows against one or two 500 m windows give identical outputs.

---

## 2. Rasters: what the points say about surfaces

The points go onto a 0.5 m grid (`RES`): about 3–4 pulses per pixel at 15 pulses/m², which is
fine enough for a 2 × 3 m shed.

| raster | definition | what it shows |
|---|---|---|
| `dtm` | mean height of the ground points (class 2); empty pixels take the nearest ground pixel's | the ground, also under buildings |
| `dsm` | highest point (noise dropped); gaps up to 1 m filled | the top surface |
| `ndsm` | dsm − dtm | height above ground; objects are > 2 m (`MIN_HEIGHT`) |
| `ground_gap` | distance to the nearest pixel with a ground point | covered holes |
| `multi_return` | share of the pulses with more than one return, over 3 × 3 pixels | hard surface or foliage |
| `roughness` | \|Laplacian of the dsm\| | planar or bumpy |

Why these three clues tell roofs from trees:

- **A roof is opaque.** No pulse reaches the ground under it, so a building leaves a hole in
  the ground points with straight edges and a large `ground_gap` in its middle. A crown is
  porous: some pulses reach the ground, so its gaps stay small and scattered.
- **A roof stops a pulse at once.** A laser pulse lights a spot tens of centimetres across. On
  a hard surface all of it comes back at the same time: one return. In foliage, parts of the
  spot hit leaves at different depths, and the echo splits into several returns. So the share
  of split pulses is near 0 on roofs and high in trees.
- **A roof is made of planes.** For any plane z = a + bx + cy the Laplacian
  ∂²z/∂x² + ∂²z/∂y² is 0, so flat and evenly sloping roofs both have roughness 0. Only ridges
  and edges show up, as lines. A crown bends everywhere.

---

## 3. Objects: a seeded watershed (`segment`, `reclaim_roof`)

```mermaid
flowchart TD
  H["High pixels: ndsm above 2 m"] --> CH["Covered holes: no ground point within 0.5 m"]
  CH --> Q{"Hole roof-like as a whole? split below 0.15, roughness below 0.5"}
  Q -- yes --> S1["Roof seed: its roof-like pieces of 1 m² or more"]
  Q -- no --> S2["Roof seed: its roof-like pieces of 20 m² or more, if any"]
  H --> TT["Tree tops: local maxima within 2.5 m of the smoothed ndsm, not within 1 m of a roof seed"]
  S1 --> W["Watershed on −ndsm from all seeds, each seed piece with its own label"]
  S2 --> W
  TT --> W
  W --> MP["merge_parts: each piece's flood back to its building, unless a small piece flooded a crown (split 0.24 or more): then a tree"]
  MP --> RR["reclaim_roof: tree pixels that are really roof go to a roof"]
```

**The watershed.** Turn the ndsm upside down (−ndsm), so that tops become basins, and flood it
from the seeds. Each pixel goes to the seed whose flood reaches it first. Floods meet along the
valleys of the real surface: the saddle between two crowns, the drop from a roof to a crown. If
every local top were a seed, a roof would fall apart at each ridge and dormer. So there is one
seed per roof, made from its covered hole.

**Roof seeds.** A covered hole is a connected group of high pixels with no ground point in the
pixel or its four neighbours (`HOLE_GAP` = 0.5 m). It seeds a roof if, as a whole, it has few
split pulses (< 0.15) and is smooth (median roughness < 0.5 m). Only the hole's own roof-like
pixels become the seed, in pieces of at least 1 m², because a dense crown next to a building
also hides the ground. If no such piece exists, the whole hole is the seed: a small shed is
nearly all edge. A hole that runs on under trees can fail as a whole. Then only its roof-like
pieces of at least 20 m² (`ROOF_PIECE`) seed it, which are bigger than a crown's smooth bits.
In Chewelah 8 of 2,297 such holes have one, and 5 of them are buildings.

**Tree tops.** Local maxima of the ndsm, smoothed with a 0.5 m Gaussian, within 2.5 m: at 1.5 m,
big crowns split into 2–3 pieces. Tops within 1 m of a roof seed are dropped, since eaves and
parapets aren't trees. A high patch with neither kind of seed is seeded at its highest pixel.

**Small seed pieces in a crown (`merge_parts`).** A dense crown over a roof's edge hides the
ground too, so it belongs to the roof's covered hole, and a smooth bit of it (a few pixels with
no split pulse) can be one of the hole's roof-like pieces. That bit is then a roof seed, the
1 m rule drops the tree's top next to it, and its flood takes the whole crown into the roof.
Michael found this in Chewelah: a 47 m² tree in a corner of a building, seeded by a 1 m² bit.

So each seed piece floods with a label of its own. The flood itself doesn't change: the
watershed takes pixels in order of height, and which label a seed carries doesn't change that
order, so the pieces of a building together get exactly the pixels they got before (checked:
with the rule off, the labels equal the old ones on both test sites). Afterwards each flood goes
back to its building, except the flood of a small piece (not the building's largest, and under
`ROOF_PIECE`) that has 0.24 or more split pulses (`SEED_TREE_MULTI`): that flood is a crown, and
becomes a tree object. Then `reclaim_roof` treats it like any other tree, so its thin edges along
the roof still go back to the roof.

Why the split pulses of the flood, not the size of the piece: Michael labelled the 84 floods of
such pieces in Chewelah. Trees grew from pieces of 1–4.5 m², but so did many roof parts (porches,
lower sections). The floods' split shares separate them: trees 0.26–0.33, roof parts at most
0.22. One flood at 0.26 is a roof edge with shrubs below it; `reclaim_roof` and the roof under the
canopy give two thirds of it back. Three small trees stay in their roofs (0.12–0.22). In run 29
11 of 189 footprints changed, by −344 m² in all, and no building appeared or disappeared.

**`reclaim_roof`.** The flood can call roof "tree" in three ways:

- a tree top lands on a roof section, so the section becomes a tree object;
- a crown floods along the eaves or a ridge, which look like trees (split pulses, sharp bends);
- a rooftop unit becomes a patch of "tree" in the middle of the roof.

The fixes, in the same order:

- A whole tree object next to a roof becomes roof if it has few split pulses (< 0.15, or < 0.2
  from 25 m² up). It goes in one piece to the roof it shares the longest border with.
- Thin pieces next to a roof become roof. An opening (an erosion, then a dilation) with a 1 m
  disk keeps only what the disk fits into, so it finds every piece narrower than ~2.5 m.
- A tree patch with roof all around it becomes roof. A real tree reaches past the roof's edge
  somewhere, down to lower ground.

Each tree object is made one piece again afterwards.

Each object then gets a row of features (`objects_table`): area, height, hole depth and share,
rectangularity, split share, roughness (also without the edge pixels: `roughness_inside`) and
metrics from its points.

---

## 4. The rule (`is_building`) and the vehicle clue (`cover_min`)

```mermaid
flowchart LR
  O["Object"] --> A{"Roof seed?"}
  A -- no --> N["0: not a building"]
  A -- yes --> B{"split below 0.35, roughness_inside below 0.5 (0.3 under 25 m²), area 4 m² or more, cover_min not below 0.2"}
  B -- no --> N
  B -- yes --> C{"Under 40 m²?"}
  C -- yes --> S["2: shed, container or RV"]
  C -- no --> Y["1: building"]
```

Each condition comes from objects labelled in QGIS:

- **Only roof-seeded objects.** Applying the rule to every object was tried. In Chewelah it
  added 26 footprints, many of them trees.
- **Split pulses below 0.35, over the whole object.** A dense crown can hide the ground and get
  a seed, but over all of it, its pulses split.
- **`roughness_inside` below 0.5 m, or below 0.3 under 25 m².** Roughness is taken without the
  edge pixels, where the Laplacian sees the drop to the ground: on a shed half the pixels are
  edge. Small objects must be smoother because a shed has a simple roof. Examples:

  | object | roughness_inside | result |
  |---|---|---|
  | a shed | 0.05 | passes |
  | a 16 m² bush | 0.36 | fails |
  | a rock (a dome) | 0.61 | fails |
  | a 180 m² house with a complex roof | 0.40 | passes |
- **At least 4 m².** The smallest checked shed is 7.5 m².
- **`cover_min` not below 0.2.** This is the vehicle guard, described next.

**`cover_min`.** Each flight line is one pass at one time. Chewelah has four lines: three
within 24 minutes and one three days later. For one line, `cover` is the share of its first
returns in the object's pixels that lie above 2 m.

- A building is there in every pass, so its cover is high in every line.
- A vehicle that came or went between passes is missing from one line, and its cover there is
  about 0.
- Only lines that reach at least half of the object's pixels count. A line that only grazes
  the edge sees mostly the ground beside it.

The data show a clear gap. The vehicles that moved score 0.00, while every roof-seeded object
scores at least 0.33. The guard is insurance: a moving vehicle rarely gets a roof seed anyway,
because another line saw the ground under it. A vehicle parked through all passes looks like a
shed, and nothing here can tell them apart.

---

## 5. Roof under the canopy (`roof_under_canopy`)

**Physics.** A crown lets a share of the pulses through: its gap probability. In canopy optics
this falls exponentially with the leaf area along the path. Over the tree notches in Chewelah,
20–55% of the pulses got through. A pulse that gets through ends on whatever lies below: the
roof if the roof continues under the tree, otherwise the ground.

```mermaid
flowchart TD
  A["Pixel of another object within 2 m of a building"] --> B{"Plane through the nearest visible roof fits? (sd up to 0.1 m)"}
  B -- no --> X["stays tree"]
  B -- yes --> C{"Pixel's top under the plane?"}
  C -- yes --> X
  C -- no --> D{"A last return more than 0.3 m under the plane?"}
  D -- yes --> X
  D -- no --> E{"A last return within 0.15 m of the plane?"}
  E -- yes --> F["evidence"]
  E -- no --> G{"5 of 8 neighbours roof, one of them evidence?"}
  G -- yes --> F
  G -- no --> X
  F --> H{"Patch: 3 times as many returns on the plane as in the slab just above, and connected to the building?"}
  H -- yes --> Y["roof, for the footprint only"]
  H -- no --> X
```

Why each step:

- **The surface.** A roof facet is locally a plane. It is fitted to the dsm of the building's
  pixels within 1.5 m of the nearest visible roof pixel, in four rounds that each drop the
  pixels more than 0.3 m off. If it still doesn't fit within 0.1 m (a ridge, a step), there is
  no surface to continue. The plane is evaluated at each point's own position, so sloping
  roofs work too.
- **"On" is evidence; "under" is a veto.** A last return within 0.15 m of the surface means
  something stopped the pulse there. A last return more than 0.3 m under it means the pulse
  went through where the roof would be. Since a roof is opaque, one such return rules the pixel
  out. That is how the edge of a hidden roof is found: beyond it, pulses reach the ground.
- **The top must not be under the surface.** A tree lower than the roof isn't over it.
- **Filling gaps.** With n ≈ 3.5 pulses per pixel and a gap probability P ≈ 0.3, the chance
  that no pulse gets through a pixel is (1 − P)ⁿ ≈ 0.29. So about a third of the pixels have
  no evidence either way. Such a pixel joins if most of its neighbours are roof and one of them
  has evidence, so there is never roof without evidence nearby.
- **The thin layer.** The pixels were *chosen* because they have a return on the surface, so
  that alone proves little: in a dense crown, by a rough estimate, a quarter of the pixels have
  one by chance.
  What a real surface does is concentrate returns into a thin slab, while a crown spreads them
  through its depth. So each patch is compared with the 0.3 m slab just above the surface (the
  same thickness, and not selected for), and it must have at least 3 times as many returns on
  the surface. In Chewelah the roof found has 3,101 returns on the surface against 103 in the
  slab above (a median of 26 to 1 per building).
- **For the footprint only.** The rule has already judged the visible roof, so no building code
  can change. The crown keeps its whole shape, so crown and footprint overlap where the tree
  hangs over the roof, which is what is really there.

Result in Chewelah: 49 of 167 buildings gained 306 m² in all, and the tree-notch flags went from
32 to 13.

---

## 6. Polygons (`trace`, `straighten`, `tree_notch`)

Outlines run along the pixel edges (`gdal.Polygonize`), with holes under 1 m² filled (single
low pixels in a roof). Objects that aren't buildings are simplified with a 0.5 m tolerance.
Buildings get straight walls:

1. **Walls.** Douglas–Peucker cuts each ring into walls: keep the point furthest from the
   chord if it is more than 0.75 m off, and repeat on both halves. It starts at the point
   furthest from the centre, which is a real corner.
2. **The main direction.** A wall's direction θ is the same as θ + 180°, and in a rectilinear
   building as θ + 90°. Multiplying the angle by 4 makes walls at right angles point the same
   way. The direction is the mean of the vectors $w\,e^{4i\theta}$, divided by 4:

   $$z = \sum_k w_k e^{4i\theta_k}, \qquad \text{main} = \tfrac{1}{4}\arg z, \qquad \text{agree} = \frac{|z|}{\sum_k w_k} \in [0, 1].$$

   `agree` is the mean resultant length of circular statistics: 1 when all walls are at 0° or
   90° to each other. The weight w = length². Long walls are measured from many pixels and are
   rarely tree edges. In a second pass only walls within 15° of the first answer vote, so
   diagonal tree edges can't turn the building.
3. **A line through each wall.** Each wall is fitted by total least squares to its pixel
   edges, treated as line pieces. A piece of length ℓ adds its own spread ℓ²/12 along itself,
   so a staircase gives the line through its middle, and a single edge keeps its own direction.
4. **Snapping and corners.** A wall within 15° of the main direction (or square to it) is
   turned onto it. Diagonal walls of 4 m or more stay; shorter unsnapped bits are dropped.
   Corners are where neighbouring lines cross; parallel neighbours get a jog.
5. **Safety net.** Straightening is refused, with an `outline` code, if:
   - the building is under 25 m²;
   - the walls don't agree (agree < 0.7, mostly tree-bitten);
   - the result isn't a valid shape;
   - or the area changed more than 10%.

**Tree notch.** A diagonal wall of 4 m or more with mostly tree pixels just in front of it is
usually a tree over the roof's corner, not a wall. Such buildings get `review` = 1.

**A known bias.** A pixel counts as roof if any of its pulses hits the roof, so the pixel outline
lies outside the true roof edge. Section 7 measures how far and moves the walls back.

---

## 7. Walls from the points (`wall_edge`, `move_wall`, `refine_walls`)

A wall placed on pixel edges is good to about ±0.25 m. The points can do better.

**The estimate.** Cut the wall into slices of width s = 0.5 m along its length. In each slice,
take the roof point furthest out, d_max. It always lies a little inside the true edge: the roof
stops at the edge, and between its last point and the edge there is a gap. Suppose the roof
points form a random (Poisson) pattern with ρ points per m². A strip of the slice x deep along
the edge then holds ρsx points on average, and the chance that it holds none (that the gap is
bigger than x) is $e^{-\rho s x}$. So the gap is exponential, with mean $1/(\rho s)$ and median
$\ln 2/(\rho s)$ (where $e^{-\rho s x} = \tfrac12$): about 0.10 m at ρ = 14. The edge is then

$$\hat e = \operatorname{median}_k\, d_{\max,k} + \frac{\ln 2}{\rho s}.$$

The median ignores the odd slice with a gutter, an antenna or a gap. Because it is the median of
the outermost points, the gap to add is the median gap too: the mean would put the wall about
4 cm too far out. Over the ~20 slices of a 10 m wall the error is about ±3 cm.

The gap grows as the density falls: 0.28 m at 5 points per m², 0.69 m at QL2's 2 per m², as big
as the bias being corrected, and as uncertain. Real points lie along scan lines, more regularly
than at random, and then the gap depends on how the pattern lines up with the wall: on a regular
grid at 14 per m² it is between about 0.08 m and half the point spacing, 0.13 m. So the term is
good to 2–4 cm either way. (On the tests' perfect 0.25 m lattice, with the walls along it, they
land 4 cm too far in.)

**Roof side only.** Two effects make the ground side unreliable:

- behind a building (seen from the scanner) the ground near the wall is in shadow;
- slanted pulses reach the ground *under* the eaves. In Chewelah the innermost ground point lies
  a median 0.16 m inside the roof's edge.

**Roof points.** These are first returns within 0.3 m of the roof surface fitted next to the
wall. Each slice gets its own height and the slices share one slope across the wall. A single
plane fails at gable ends, where the roof rises along the wall to the ridge.

**Moving the walls.** Each wall of a straightened building lies on a line
$\mathbf n_i \cdot \mathbf x = c_i$, where $\mathbf n_i$ is its outward normal (for a courtyard,
outward is into the courtyard). Moving the wall by its shift $s_i$ (negative is inward) only
changes $c_i$ to $c_i + s_i$. Each corner is where its two moved lines cross:

$$\begin{pmatrix} \mathbf n_{i-1}^\top \\ \mathbf n_i^\top \end{pmatrix} \mathbf x
= \begin{pmatrix} c_{i-1} + s_{i-1} \\ c_i + s_i \end{pmatrix}.$$

So the walls keep their directions and the building stays square. A wall that keeps its place
has s = 0. If two neighbouring walls are parallel, the corner between them becomes a short step.
If the result isn't a valid shape (a narrow part pinched shut), the building keeps its
straightened walls.

**Safety rules.** A wall keeps its place:

- if it isn't square to the building's main direction (within 1°): diagonal walls are often a
  tree's edge;
- if `wall_edge` can't measure it: too few slices with roof points, a poor fit, or high points
  in front of it;
- if tree pixels lie in front of it (more than 20% of the places 0.75 and 1.25 m out);
- if it would move more than 0.5 m (7 of 858 walls in Chewelah, mostly next to a building of
  the same height);
- or if moving it out would push it into another roof-seeded object. That last rule is
  Michael's. The neighbours are taken as their pixel outlines, which are the same in every
  window. Their final footprints would depend on which building is done first, and that order
  changes from window to window.

**Result.** Measured on its own (Chewelah run 24, 559 straight walls), the walls move in by a
median 0.15 m (middle half 0.10–0.20 m). That is the pixel bias, about 6% of the area of a
10 × 10 m house. Built in (run 28), in Chewelah 115 of the 117 straightened buildings had walls
moved, 592 walls in all. The median shift per building is 0.16 m inward (90% between 0.09 and
0.24 m), and almost all of it is inward: 1128 m² of outline moved in, 17 m² out. The footprints
lost a median 3% of their area. That is less than 6% because most buildings are bigger than
10 × 10 m and not every wall moves. In Nine Mile Falls 3 buildings moved by 0.12 m. A test house
at random points shows the same: its pixel outline is over 124 m² for a true 120 m², and the
moved walls give 120 ± 2 m². The footprints' `area` column stays the pixel count. The columns
`walls_moved` (how many) and `wall_shift` (their median shift) record what moved.

---

## 8. A whole area (`detect.py`): several lidar sets, and a look at NAIP

**Squares.** `detect.py` lays 500 m squares on a grid in the output CRS (NAD83(2011) UTM at the
area's centre) and keeps those that touch the area (a fire perimeter, say) and where an EPT's
octree has data. tilestream reads each with its 50 m border (section 1), several processes at
once. Each window also reports its earliest and latest GPS time, so every square says when it
was flown.

**Where two lidar sets meet.** Each set runs on its own squares, so the squares they share run
twice. In a square only one set has points in, its objects are taken. In a square both have
points in, the objects of both are taken, except:

- two outlines that overlap by more than half of the smaller one are the same object, seen
  twice. The bigger one stays: at the edge of its data a set sees a building cut off, so the
  bigger outline is the more complete one;
- two outlines that overlap less are the two parts of one object that the data's edge cut:
  each set sees its own side, and the parts overlap in the strip where both have points. They
  are joined: the union, with the areas added up less the overlap.

Each object's `lidar` column names its set (or both). Why not take each square from one set:
in the Camp Fire test, in three squares where the sets meet, the set with the most points would
have lost 96 buildings that only the other set saw.

**When it was flown (`flown_map`, `add_flown`, `--before`).** A lidar set can be flown on
several dates, in parts, and its name doesn't say so. B4 2018 over the Camp Fire was flown in a
strip through Paradise in July 2018, and everything west of it in December 2018, February 2019
and August 2019, after the fire. The squares' GPS times show this, but one square can hold both.
So `detect.py` maps each set's dates at 10 m: it reads each square again, thinned to about 5 m
(the EPT octree's upper levels, a small share of the points), and keeps the first and the last
GPS time in each cell. A footprint's dates (`flown_first`, `flown_last`) are those of the cells
under its corners and its x, y, from its own set(s). With `--before DATE` (a fire's start),
`buildings.gpkg` keeps only the buildings with `flown_last` before it, and `flown.gpkg` shows
where the sets together were flown before, after, or both. Why it matters: lidar flown after a
fire sees only the buildings that still stand, so the others aren't absent, they are missing.
The flights meet along lines: only 0.2 km² of B4's 170 km² have points of both.

**NAIP's green on the roof (`naip_green`).** The lidar's clues miss some trees: in Chewelah three
small ones stayed in their roofs (section 3). Colour sees them. For each building, `naip_green`
is the m² of its visible roof (`segments` = 1, inside its polygon) where NAIP's pixel is green:
NDVI = (near infrared − red) / (near infrared + red) above 0.25, and the green band at least as
bright as the blue one. Only roof pixels at least 1 m from anything that isn't roof count.
`naip_flag` = 1 from 4 m². Why each part:

- **NDVI above 0.25.** On Michael's labels (2023 NAIP), the trees' median NDVI was 0.29 or more,
  the roof parts' 0.21 or less.
- **Green at least blue.** A blue metal roof in Chewelah had NDVI above 0.25 over 1,100 m²:
  metal reflects near infrared. Plants are greener than they are blue. The labelled trees keep
  most of their green pixels (shares of 0.38–0.83, from 0.38–0.98).
- **1 m from the roof's edge.** NAIP is made true to the ground, so tall things appear shifted
  away from the camera. Over all Chewelah crowns: in 2021 east, 1 m for crowns of 2–6 m, 2 m for
  6–12 m, 3 m above; in 2023 west, 1.5–2.5 m. A crown beside a roof spills its green onto the
  roof's edge. A tree taken as roof is mostly inside it.
- **4 m².** fid 61 in Chewelah keeps two small trees in its roof; after the edge rule 5.75 m² of
  them are green.

Results: in Chewelah run 29 (NAIP 2023) 16 of 167 buildings are flagged. Of the four greenest,
one is a tree or hedge taken as a building and one has tree parts in it; one is a green-painted
roof and one a big white roof that street trees lean onto in NAIP. In the Paradise test (NAIP
2018, flown 9–14 days after the lidar) 16 of 319 (5%) are flagged; the three greenest all hold
trees. So it is a flag for a look, not a verdict. It needs NAIP close to the lidar's date: in
Chewelah, 2021's NAIP sees 3 of the 12 labelled trees, 2023's 10.

Where it breaks: tall trees next to roofs. Over the whole Camp Fire run (NAIP 2018) 21% of the
buildings are flagged: 2–8% in central Paradise, up to 67% among the tall pines of east Paradise.
There, shifting NAIP 3 m west takes two-thirds of the green off the roofs (4,568 → 1,513 m² on
424 buildings; 3 m east doubles it). That is the pines' lean in NAIP, not trees in the roofs:
`NAIP_EDGE` (1 m) is enough for Chewelah's trees, not for 30–40 m pines. A next step: align
NAIP to the lidar's crowns block by block before counting.

---

## 9. How we know it works

- **Seam tests:** different window layouts give identical outputs (section 1), the moved walls
  included.
- **Synthetic scenes with known answers:** a house, a tree, a rock, a bush; a tree over a
  house's corner; a sloping roof under a crown; a house measured at random points.
- **Every rule has a test that fails without it.** This is checked by running the tests against
  deliberately broken copies of the code.
- **Real objects labelled in QGIS** by Michael: the rock, the sheds, the vehicles, a hedge, a
  log pile, the 24 seedless candidates, the 97 seed floods and patches of the reclaim survey, and
  the footprints of each change.

---

## 10. Main assumptions, and where they break

| assumption | why it mostly holds | where it breaks |
|---|---|---|
| Roofs are opaque | solid materials | glass roofs; skylights (holes under 1 m² are filled) |
| Roofs are made of smooth planes | most roofs | gravel roofs with equipment, parapets (walls keep their pixel place) |
| The ground classification is right | the USGS ground filter | a terrace on a steep slope can look like a roof |
| Smooth, high, with no ground under it, is a roof | buildings are | bridges and power-line wires over canyons and water (Camp Fire: the Pulga railway bridge; 105 code 2 objects more than 12 m up, most of them wire) |
| Buildings are rectilinear | most buildings | round or curved buildings (no straightening, `outline` 2) |
| Things stay put between passes | buildings do | parked vehicles look like sheds |
| Objects are under ~100 m | halo of 50 m | big warehouses would need a larger halo |
| A lidar set's name gives its date | most sets are flown in weeks | B4 2018 (Camp Fire): July 2018 and three flights after the fire; `detect.py` maps the dates (section 8) |
| ~12–15 pulses/m² | thresholds tuned there | much sparser data: retune and re-check (Paradise pre-fire, QL2 on paper, has 9.8/m²) |
| A crown's smooth bit doesn't seed a roof | its flood has split pulses (`merge_parts`) | a crown whose flood has few split pulses |
| A hedge isn't a roof | lidar: few split pulses, smooth | a dense trimmed hedge passes (`naip_flag` may catch it) |
| NAIP's green is a plant | NDVI above 0.25 and green at least blue | green-painted roofs, roof gardens; tall trees shifted onto a roof by more than 1 m (east Paradise: 3 m, up to 67% of houses flagged) |
