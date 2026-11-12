# Utility lines in the lidar: notes for later

Notes, 2026-11-11, from the Camp Fire run (`runs/camp_fire_prefire`, PlumasNF B2 QL1 and B4 QL2).
Nothing here is built yet. Someday we may want to find power lines themselves; probably not
from QL2.

Why it matters here: CAL FIRE found (May 2019) that the Camp Fire was started by PG&E's
transmission line near Pulga, in the Feather River canyon, inside our area.

## How they show up

- **Class 1.** The USGS lidar specification doesn't ask for the wire classes (LAS 1.4: 13
  wire guard, 14 wire conductor, 15 transmission tower, 16 insulator), and B2 and B4 don't use
  them. The wire points over the reservoir below are class 1 (unclassified), plus some class 7
  (noise). Bridge decks are classified (17), so bridges can be dropped, but wires can't.
- **Wires:** dotted lines of single returns. A conductor is a few cm thick, so only a few
  pulses hit it: in a 30 x 30 m box over the reservoir at (628860, 4393196) there were 61
  points, 52 of them on the wires. Seen from above they run straight between towers. Seen from
  the side they sag as a catenary.
- **Towers:** steel lattice, so pulses split. They look like a thin, sparse tree with a square
  base. **Wooden poles** (distribution lines, ~12 m) give a few points: a "tree" with no crown.
- **Over ground:** ground points lie under the wires, so there is no covered hole and no roof
  seed. At most a few small tree objects are left, mostly under `MIN_AREA` or `CROWN_MIN_AREA`.
  But the wires do put spikes in the dsm and ndsm.
- **Over water or a deep canyon,** where nothing under the wire returns: no ground nearby, so
  the wire pixels form a "covered hole". Single returns, smooth, so the rule takes them as roofs.
  In this run, **49 "sheds" (code 2) are pieces of wire 71–83 m over the water** near
  (628865, 4393197): 4–31 m² each, `multi_return` 0. Their ground hole reaches tens of metres
  beyond them (median 47 m), much further than a roof's own hole. One more stands 33 m up at
  (636197, 4401062).

What catches them now:
- `SMALL_MAX_HEIGHT` (2026-11-11): a building under 40 m² must be lower than 12 m. This
  catches all of these.
- An idea, not built: a hole that reaches much further than the object itself ("hole excess").
  See HANDOFF.

## If we want to find the lines someday

- **Candidate points:** high above the ground, nothing under them within a few metres, few
  neighbours (isolated, not a crown). Single returns, or the first of a split pulse.
- **Lines:** link the candidates in plan view with RANSAC or a Hough transform. Wires are
  straight between towers, so even sparse points line up. Then fit a catenary (or a parabola)
  in the vertical plane along each line, which sorts wires from bird- or tree-noise.
- **Towers:** look at the ends of the straight runs for vertical, multi-return clusters.
  Line direction changes at towers.
- **Density:** QL2 (~2 pulses/m², B4) gives a point every few metres of wire at best. QL1 (B2)
  is better. Expect broken lines from QL2: link the pieces along the same direction.
- **Data to check against:** public transmission-line maps (HIFLD's electric transmission
  lines for the US, OpenStreetMap `power=line` / `power=tower`). Check what is available when
  we start; distribution lines are mostly not mapped.
- **Output:** a line layer (wire runs with height above ground and sag) and a point layer
  (towers and poles). The wire points could also be dropped from the dsm, like bridge decks,
  so they stop making false objects.
