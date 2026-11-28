# Evacuation Routing

## Goal
Give the dispatcher a route from `(lat, lon)` to an `EvacuationPoint` at a moment `at`, with an honest status: which segments are flooded, which have no current data, and where the current image shows no water. Nothing is ever reported as "safe": satellite data cannot prove a road is passable.

## Road status at `at` (`backend/services/road_status.py`)
One SQL query classifies every road; the live map and the route use the same function.

| `route_status` | `reason` | Meaning |
|---|---|---|
| `closed` | `null` | The whole segment geometry intersects water valid at `at` (`observed_at <= at < valid_until`). |
| `closed` | `untimed_report` | Intersects a polygon without an observation time (manual report). |
| `mapped_option` | `null` | Fully inside a current analysis extent, outside every current Not Analysed mask, no water. UI label: "Not checked" (grey), not "open". |
| `unknown` | `gap` | Outside current coverage, partly outside, or under a current Not Analysed mask. |
| `unknown` | `expired` | Only covered by analysis that has expired at `at`. |
| `unknown` | `error` | Status could not be computed. |

## Decision Flow (`backend/services/evacuation.py`)
1. **Start**: snap `(lon, lat)` to the nearest `Road`. `start_in_flood` is true if the point is in current or untimed water, or the start road is `closed`. The route is computed for any click, inside water or not (there is no `"safe"` short-cut anymore).
2. **Targets**: evacuation points that are not in current or untimed water (the same test as `in_danger` on the map), snapped to their nearest road; closed roads are never targets.
3. **Two-pass Dijkstra** by segment length (metres), undirected:
   - pass 1 uses `mapped_option` segments only (an `unknown` start may also be the goal);
   - if that fails, pass 2 also allows `unknown`;
   - `closed` segments are never entered. A closed start may be left (the user has to exit their block).
4. **Rating**: the route gets the status of its weakest segment (`closed` < `unknown` < `mapped_option`). A closed start is excluded from the rating and reported via `start_in_flood`; any other start counts. `limiting_segment_ids` are the segments with the weakest status; `reason` prefers `expired` over `gap`.
5. **Fallback** (no path, or an error, always `route_status = unknown`, `reason = error`, `route = null`):
   - nearest dry evacuation point → `evacuation_point_fallback`;
   - otherwise the nearest road end outside water → `candidate_fallback` (both coordinates may be `null`).

## Response (`POST /evacuation-route`, body `{lat, lon, at?}`; `at` must carry `Z` or an offset)
`status`, `at`, `route_status`, `reason`, `limiting_segment_ids`, `start_in_flood`, `road_ids`, `distance_meters`, `target_point`, `candidate_lat/lon`, `detail`, and `route`: a GeoJSON FeatureCollection with one feature per segment, `properties = {id, route_status, reason}`. Colour the line per segment and use the top-level `route_status` for the badge. `in_disaster_area` is kept as an alias of `start_in_flood`.

## Performance
The road graph (adjacency + lengths) is cached in memory and rebuilt when the stored roads change (road count, max id, link count). On AOI02 (26 793 roads): first request ~1.4 s, then ~30 ms for the graph; the per-request cost is now the status query (~1–3 s).
