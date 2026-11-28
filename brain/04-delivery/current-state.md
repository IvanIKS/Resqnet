# Current State & Delivery

## Status
All core backend and frontend features are merged into `master`:
- **PR #3**: Full-screen Leaflet map view (`feat/frontend-map`)
- **PR #4**: JWT authentication & role-based authorization (`feat/backend-auth`)
- **PR #5**: Liquid Glass auth UI + ASCII flood background + map integration (`feat/frontend-auth`)

## Verification
- Backend auth unit tests (`backend/tests/test_auth.py`): 3/3 passing.
- Backend evacuation unit tests (`backend/tests/test_evacuation.py`): 5/5 passing.
- Frontend production build (`cd frontend && npm run build`): passing.

## Running Locally
- **With Docker**: `just run` (`docker compose up -d --build`)
- **Without Docker**: `just dev` (starts `backend/.venv/bin/uvicorn` on `:8000` and Vite on `:5173`, requires local PostgreSQL + PostGIS).
