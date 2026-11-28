# Architectural Decisions

1. **Spatial Database (PostGIS, SRID 4326)**:
   - All geometries (`DisasterArea.area`, `EvacuationPoint.geom`, `Road.geom`, `end1`, `end2`) use WGS84 (`SRID=4326`) with PostGIS spatial functions (`ST_Contains`, `ST_Intersects`, `ST_Distance`, `ST_Length`).
2. **Road Network as Self-Referential Many-to-Many Graph**:
   - `Road` segments connect via `end1_roads` and `end2_roads` association tables, enabling in-memory Dijkstra search without external routing engines.
3. **Starting Road Exception in Flood Zones**:
   - A user inside a `DisasterArea` may start on their nearest `Road` segment even if flooded, while all subsequent flooded segments are forbidden.
4. **Zero-Dependency Auth (Stdlib Crypto + JWT)**:
   - Uses Python `hashlib.pbkdf2_hmac`, `hmac`, and `base64` instead of external JWT/passlib packages to keep `requirements.txt` minimal.
5. **Centralized Frontend Palette**:
   - All colors and fonts live in `frontend/src/style.css` (`:root` variables); components consume variables only.
