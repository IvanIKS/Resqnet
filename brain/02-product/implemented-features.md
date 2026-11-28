# Implemented Features

## Backend (FastAPI + PostGIS)
- **Authentication & Authorization** (`backend/api/auth.py`, `backend/services/auth.py`):
  - `POST /auth/register`, `POST /auth/login`, `GET /auth/me`.
  - PBKDF2-HMAC-SHA256 password hashing and HS256 JWT tokens.
  - `get_current_user` and `require_role` dependencies (`dispatcher`, `mobile_user`).
- **Evacuation Routing** (`backend/api/evacuation_route.py`, `backend/services/evacuation.py`):
  - `POST /evacuation-route` accepting `{ lat, lon, at? }`.
  - Two-pass Dijkstra over the cached `Road` graph: `mapped_option` first, then `unknown`, never `closed`; the route is rated by its weakest segment. See [`evacuation-routing.md`](../03-architecture/evacuation-routing.md).
- **Road status at a moment** (`backend/services/road_status.py`): `closed` / `mapped_option` / `unknown` with a reason, shared by the live map and the route.
- **Field reports with Galileo OSNMA** (`backend/api/field_report.py`, `backend/services/osnma.py`): `POST/GET /api/reports`; OSNMAlib v3.2.5 checks a recorded Galileo sample (`ublox_authentic` / `forged`). Shown on the live map as `field_report`; only an authenticated `blocked`/`flooded` report closes its road (`reason = field_report`).
- **People in flood zones** (`backend/api/people_estimate.py`, `backend/services/people_estimate.py`):
  - `GET /api/disasters/{id}/people-estimate?at=`: people per flood zone and in total, weighted by the flooded share of each settlement. See [`live-map-and-people-estimate.md`](../03-architecture/live-map-and-people-estimate.md).
- **Ingestion, Alarm, Notification & Live Map**:
  - Flood & road ingestion (`backend/service/ingestion_flood.py`, `ingestion_roads.py`), live map API (`backend/api/live_map.py`), disaster & evacuation point CRUD.

## Frontend (React 18 + Vite + Leaflet)
- **Authentication UI** (`frontend/src/components/AuthForm.jsx`, `AuthContext.jsx`):
  - Liquid Glass card with `Orbitron` display wordmark and `JetBrains Mono` typography.
  - Animated ASCII flood-wave canvas background (`TerminalBackground.jsx`).
  - Role toggle (`Mobile User` / `Dispatcher`) on registration and `localStorage` token persistence.
- **Map View** (`frontend/src/components/MapView.jsx`):
  - Full-screen OpenStreetMap view via `leaflet` and `react-leaflet@4` with click-to-place marker.
- **Design System** (`frontend/src/style.css`):
  - Centralized light-theme CSS custom properties (`--color-*`, `--font-mono`, `--font-display`).
