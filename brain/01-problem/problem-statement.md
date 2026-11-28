# Problem Statement & Goals

## Problem
During sudden flood disasters:
- Affected areas (`DisasterArea`) change dynamically from satellite/ingestion data, cutting off standard road networks.
- Citizens need immediate, safe evacuation routes that avoid flooded road segments, or a reliable fallback when all road paths are blocked.
- Dispatchers and mobile users need role-separated access (`dispatcher` vs `mobile_user`) and a live map to coordinate evacuation points and disaster zones.

## Goals
1. **Automated Flood & Road Graph Modeling**: Store `DisasterArea` polygons, `EvacuationPoint` coordinates, and `Road` segments (`end1_roads` / `end2_roads`) in PostGIS (SRID 4326).
2. **Safe Evacuation Routing**: Compute shortest non-flooded road paths with deterministic fallbacks.
3. **Role-Based Access & Live Map UI**: Provide JWT authentication and a responsive, clean map interface for coordination.
