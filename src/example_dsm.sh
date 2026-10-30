#!/usr/bin/env bash
# Example --exec analysis for tilestream: a 1 m max-Z DSM (first returns, noise dropped)
# made with the pdal command-line tool.
#
#   python src/tilestream.py run out/ --ept WA_NorthEast_2_B22 --bbox -117.42 47.40 -117.38 47.42 \
#       --exec src/example_dsm.sh --halo 5
#
# Contract (see tilestream.exec_analysis):
#   $1  this window's points (LAZ, core + halo, in $TS_SRS)
#   $2  output dir: each file <name>.<ext> written here becomes out/<name>/<window>.<ext>;
#       GeoTIFFs are cropped to the core, CSVs with x,y columns lose rows outside it,
#       <name>.json (an object of scalars) becomes a row of out/<name>.csv after merge
#   TS_NAME, TS_SRS, TS_CORE and TS_HALO ("xmin ymin xmax ymax", metres)
# Put rasters on a grid aligned to multiples of the resolution, so the core crop is exact
# and windows mosaic seamlessly. --halo must cover the reach of the analysis: here the
# gdal writer's search radius, RES * sqrt(2).
set -euo pipefail
laz=$1 out=$2
RES=${RES:-1}

# halo snapped outward to multiples of RES (UTM coordinates are positive)
read -r x0 y0 x1 y1 < <(awk -v r="$RES" -v b="$TS_HALO" 'BEGIN {
    split(b, h, " ")
    printf "%.10g %.10g %.10g %.10g\n", int(h[1] / r) * r, int(h[2] / r) * r, -int(-h[3] / r) * r, -int(-h[4] / r) * r }')

pdal translate "$laz" "$out/dsm.tif" expression \
    --filters.expression.expression="ReturnNumber == 1 && Classification != 7 && Classification != 18" \
    --writers.gdal.resolution="$RES" --writers.gdal.output_type=max --writers.gdal.data_type=float32 \
    --writers.gdal.bounds="([$x0, $x1], [$y0, $y1])"

n=$(pdal info --summary "$laz" | python -c "import json, sys; print(json.load(sys.stdin)['summary']['num_points'])")
echo "{\"points_with_halo\": $n}" > "$out/info.json"
