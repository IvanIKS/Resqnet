# Live Map and People Estimate

Author: Yevhen (None_UN1)

## Live map: `GET /api/map/live`

One GeoJSON `FeatureCollection` for the dispatcher map, so the frontend makes one request per refresh. Every feature has `properties.kind`:

| kind | geometry | properties |
| --- | --- | --- |
| `disaster_area` | Polygon | `name`, `disaster_id`, `disaster_name`, `since` (image time), `untimed` (hand-drawn, no image time) |
| `road` | LineString | `route_status` (`closed` / `mapped_option` / `unknown`), `reason`, `flooded` = closed |
| `evacuation_point` | Point | `name`, `in_danger`: the point is inside a current flood polygon |
| `settlement` | Polygon | `name`, `population`, `affected`: the settlement touches a current flood polygon |

`summary` carries the dashboard numbers: roads by status, evacuation points in danger, settlements affected, `people_affected`, and `observations` (source, image time, valid until) for the "data as of" line.

Parameters: `at` replays the situation at a moment (ISO 8601, default now), `disaster_id` limits to one disaster, `bbox=minLon,minLat,maxLon,maxLat` returns only what is on screen (PostGIS `&&` on the bounding box, so it uses the spatial index).

Road status comes from `services/road_status.py`, shared with the evacuation route; see [`evacuation-routing.md`](evacuation-routing.md).

## People estimate: `GET /api/disasters/{id}/people-estimate`

Answers "how many people are in each flood zone" for the dispatcher.

Method (`settlement_population_by_area`): a settlement's population is assumed to be spread evenly over its polygon. A flood zone gets

```
people = population × area(settlement ∩ zone) / area(settlement)
```

Areas are computed on the geography (`ST_Area(geom::geography)`, square metres), not in degrees, so the share does not depend on latitude.

- Each zone is counted on its own, with a per-settlement breakdown (`zones[].settlements[]`: population, flooded share, people).
- `total_people` uses the union of all current zones per settlement, so a village under two overlapping zones is counted once.
- A satellite zone counts while `observed_at <= at < valid_until` (same time model as ingestion). A hand-drawn zone without `observed_at` counts from the disaster's `creation_time`.
- 404 when the disaster does not exist.

Difference from `summary.people_affected` on the live map: the map counts the whole population of every settlement that touches water (an upper bound, cheap enough for every refresh). The estimate weights by flooded area.

Limits: even spread is a simplification (dense centre, empty fields). A building layer with storeys (Lina's buildings work) would give a better weight than area; the endpoint can switch weights without changing its response. The Diia idea from the Trello card (app installs in the zone × 1.5) is not implemented: there is no data source for it.

Code: `backend/api/people_estimate.py`, `backend/services/people_estimate.py`, tests in `backend/tests/test_people_estimate.py` (half-covered village, overlapping zones, other disaster, empty zone, validity window, hand-drawn zone, 404).
