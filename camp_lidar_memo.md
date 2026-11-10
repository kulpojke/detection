# Camp Fire lidar: pre-fire, post-fire and recovery collections

**Date:** 2026-11-06
**Area:** 2018 Camp Fire perimeter, Butte County, CA (NIFC perimeter, CABTU, ~153,000 acres / 621 km²). The fire started Nov 8, 2018.

## Summary

All three time periods are covered by public USGS 3DEP lidar, and **every dataset below has an EPT** on the USGS public bucket on AWS.

- **Pre-fire:** flown Jul 7–8, 2018, covering about 87% of the footprint (all of Paradise, Magalia and Concow). Nothing before 2018 overlaps the fire.
- **Post-fire:** flown Feb–Sep 2019, covering 100%.
- **Recovery:** flown Nov 2021, covering 100% across two work units.

## Datasets

| Period | 3DEP work unit | QL | Flown | Share of fire | Main areas |
| --- | --- | --- | --- | --- | --- |
| Pre-fire | `CA_NoCAL_Wildfires_B4_2018` | QL2 | Jul 7, 2018 (verified over Paradise). The official window is Jul 2018 – Aug 2019. | ~31% | Paradise, Butte Creek canyon side |
| Pre-fire | `CA_NoCAL_Wildfires_PlumasNF_B2_2018` | QL1 | Jul 7–8, 2018 (verified). The official window is Jul 7 – Sep 20, 2018. | ~61% | Magalia, Concow, Pulga, Stirling City |
| Post-fire | `CA_NoCal_Wildfires_CampFire_2018` | QL1 | Feb 10 – Sep 6, 2019 | 100% | Whole footprint |
| Post-fire (SW edge) | `CA_NoCAL_Wildfires_B2_2018` | QL2 | Feb 21, 2019 (verified in the SW). The official window is Jul 2018 – Feb 2019. | ~18% (~13% not covered by any pre-fire set) | Toward Pentz / Oroville |
| Recovery | `CA_SierraNevada_1_2022` | QL1 | Nov 7–29, 2021 | ~95% | Most of footprint |
| Recovery | `CA_SierraNevada_4_2022` | QL1 | Nov 3 – Dec 5, 2021 | ~8% | Fills the rest with SN_1 (together 100%) |

Pre-fire coverage combined (B4 + PlumasNF B2) is **~87%**. The ~13% gap on the southwest edge only has the Feb 2019 B2 flight, which is post-fire.

## EPT links

All the EPTs are in EPSG:3857 and store points as LAZ.

| Work unit | EPT | Points |
| --- | --- | --- |
| B4 2018 (pre) | <https://s3-us-west-2.amazonaws.com/usgs-lidar-public/USGS_LPC_CA_NoCAL_Wildfires_B4_2018/ept.json> | 61.8 B |
| PlumasNF B2 2018 (pre) | <https://s3-us-west-2.amazonaws.com/usgs-lidar-public/USGS_LPC_CA_NoCAL_Wildfires_PlumasNF_B2_2018/ept.json> | 121.0 B |
| CampFire 2018 (post) | <https://s3-us-west-2.amazonaws.com/usgs-lidar-public/USGS_LPC_CA_NoCal_Wildfires_CampFire_2018/ept.json> | 25.2 B |
| B2 2018 (post, SW edge) | <https://s3-us-west-2.amazonaws.com/usgs-lidar-public/USGS_LPC_CA_NoCAL_Wildfires_B2_2018/ept.json> | 49.3 B |
| SierraNevada 1 2022 (recovery) | <https://s3-us-west-2.amazonaws.com/usgs-lidar-public/CA_SierraNevada_1_2022/ept.json> | 85.7 B |
| SierraNevada 4 2022 (recovery) | <https://s3-us-west-2.amazonaws.com/usgs-lidar-public/CA_SierraNevada_4_2022/ept.json> | 240.6 B |

PlumasNF B2 also has an identical copy (same point count) at `.../usgs-lidar-public/CA_NoCAL_Wildfires_PlumasNF_B2_2018/ept.json`.

Example PDAL read, for a 300 m box over Paradise:

```json
{
  "pipeline": [
    {
      "type": "readers.ept",
      "filename": "https://s3-us-west-2.amazonaws.com/usgs-lidar-public/USGS_LPC_CA_NoCAL_Wildfires_B4_2018/ept.json",
      "bounds": "([-13538937.8, -13538637.8], [4830977.2, 4831277.2])"
    },
    "paradise_prefire.laz"
  ]
}
```

## Tiled LAZ downloads (rockyweb)

- B4: <https://rockyweb.usgs.gov/vdelivery/Datasets/Staged/Elevation/LPC/Projects/CA_NoCAL_3DEP_Supp_Funding_2018_D18/CA_NoCAL_Wildfires_B4_2018>
- PlumasNF B2: <https://rockyweb.usgs.gov/vdelivery/Datasets/Staged/Elevation/LPC/Projects/CA_NoCAL_3DEP_Supp_Funding_2018_D18/CA_NoCAL_Wildfires_PlumasNF_B2_2018>
- CampFire: <https://rockyweb.usgs.gov/vdelivery/Datasets/Staged/Elevation/LPC/Projects/CA_NoCAL_3DEP_Supp_Funding_2018_D18/CA_NoCal_Wildfires_CampFire_2018>
- B2: <https://rockyweb.usgs.gov/vdelivery/Datasets/Staged/Elevation/LPC/Projects/CA_NoCAL_3DEP_Supp_Funding_2018_D18/CA_NoCAL_Wildfires_B2_2018>
- SierraNevada 1: <https://rockyweb.usgs.gov/vdelivery/Datasets/Staged/Elevation/LPC/Projects/CA_SierraNevada_B22/CA_SierraNevada_1_2022>
- SierraNevada 4: <https://rockyweb.usgs.gov/vdelivery/Datasets/Staged/Elevation/LPC/Projects/CA_SierraNevada_B22/CA_SierraNevada_4_2022>

## Caveats

- **Pre-fire dates need checking tile by tile.** The official windows for B4 and B2 run past Nov 8, 2018. I only checked the GPS timestamps at four sample spots (two in Paradise, one in Magalia, one in Concow), and all of them read Jul 7–8, 2018. Other parts of B4 inside the fire could have been flown after the fire, so check `GpsTime` for the tiles you use. Times are adjusted GPS seconds: add 1e9 and count forward from 1980-01-06.
- **PlumasNF B2 is mislocated in the 3DEP index.** The index footprint covers only ~1% of the fire, but the point cloud itself covers ~61%. This is probably why USGS Lidar Explorer doesn't show it for Paradise. OpenTopography's coverage polygon matches the data.
- **Coverage figures are approximate.** They come from intersecting the OpenTopography coverage polygons with the NIFC perimeter.
- **Quality levels differ.** B4 and B2 are QL2 (lower density), while the others are QL1. Account for the density difference when you compare B4 against the post-fire and recovery data, for example for canopy height change.
- **No older lidar overlaps.** The 2008 NCALM Middle Fork Feather River (Oroville) collection doesn't touch the fire.

## Sources

- OpenTopography catalog API: <https://portal.opentopography.org/API/otCatalog>
- USGS 3DEP Elevation Index: <https://index.nationalmap.gov/arcgis/rest/services/3DEPElevationIndex/MapServer>
- USGS 3DEP public lidar on AWS: <https://s3-us-west-2.amazonaws.com/usgs-lidar-public/>
- NIFC InterAgency Fire Perimeter History: <https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/InterAgencyFirePerimeterHistory_All_Years_View/FeatureServer/0>
