# Evacuation Routing

## Goal
Provide a reliable evacuation route for a user at `(lat, lon)` when a disaster (`DisasterArea`) is active, guiding them along safe `Road` segments to an `EvacuationPoint` or the nearest safe fallback.

## Decision Flow
1. **Safety Check**: Check if the user's point `(lon, lat)` is inside any `DisasterArea` polygon (`ST_Contains`).
   - If **not** inside any `DisasterArea` -> return `status = "safe"`.
2. **Road Graph Search (Dijkstra)**:
   - Snap the user's location to the nearest `Road` segment (`start_road`).
   - Identify all flooded roads intersecting any `DisasterArea` (`ST_Intersects`).
   - **Starting Road Rule**: Because a user inside a `DisasterArea` will likely snap to a flooded road, `start_road` is *allowed* as the first step so the user can exit their immediate block. All *subsequent* flooded roads are forbidden.
   - Search for the shortest path (by segment length in meters) using `end1_roads` and `end2_roads` adjacency to the nearest `Road` of any `EvacuationPoint`.
   - If a path is found -> return `status = "route_found"` with ordered `road_ids` and `distance_meters`.
3. **Fallback Strategy** (when no safe road path exists):
   - **Dispatcher Evacuation Point**: If at least one `EvacuationPoint` exists, return the nearest one (`status = "evacuation_point_fallback"`).
   - **Candidate Evacuation Point**: Otherwise, return the nearest `Road` endpoint (`end1` or `end2`) that lies outside all `DisasterArea` polygons (`status = "candidate_fallback"`).
