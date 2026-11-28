# RESQNET historical flood demo

From `backend/` in the Windows checkout, use the backend `.venv` and the running
`resqnet-db-1` PostGIS container on localhost port 5432. The container needs the
`template_postgis` database. The builder expects these two pinned inputs.

- `D:\cassini-review\out\resqnet_aoi02_seed.dump`
- `D:\cassini-2026\data\raw\EMSR444_AOI02_DEL_PRODUCT_r1_RTP01_v1_vector.zip`

Stop any API connected to `resqnet_demo` before rebuilding. The command verifies
both SHA-256 hashes, then drops and recreates `resqnet_demo`.

```powershell
$env:DATABASE_URL = 'postgresql+psycopg2://resqnet:resqnet@localhost:5432/resqnet_demo'
.\.venv\Scripts\python.exe -B -m demo.build_demo_db
```

The command writes JSON to stdout and saves the same fixture to
`D:\cassini-2026\analysis\21-demo-fixture.json`. The fixture records the input
hashes, Git revision, replay time, viewport, selected click, point and route
expectations.

The builder checks the database name and `jit=off`, 26,793 roads and the road
sequence value of 26,793, 52,009 end1 links, 48,005 end2 links, 349 flood rows,
93 coverage rows, zero evidence rows with missing timestamps, and zero
settlements. It checks these whole-graph road counts with the backend classifier.

| Replay time (UTC) | Closed | Mapped option | Unknown |
| --- | ---: | ---: | ---: |
| 2020-06-27 00:00:00 | 228 | 8,348 | 18,217 |
| 2020-06-27 09:13:00 | 0 | 0 | 26,793 (10,629 expired, 16,164 gap) |

The route check requires one to five downstream limiting segments and both road
snaps within 30 m. The destination is `Synthetic evacuation point · demo only`.
If the fixture selects the flooded-start fallback, show the start marker and
omit closed road 48 from the drawn route.

## Run the API

From `backend/`, start the API after the final rebuild.

```powershell
$env:DATABASE_URL = 'postgresql+psycopg2://resqnet:resqnet@localhost:5432/resqnet_demo'
.\.venv\Scripts\python.exe -B -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Fetch `GET /api/map/live` with the fixture's `at` and `bbox`. Send the same `at`
and the fixture's click `lat` and `lon` to `POST /evacuation-route`.

For Compose, run this from the repository root to override the backend URL.

```powershell
@'
services:
  backend:
    environment:
      DATABASE_URL: postgresql+psycopg2://resqnet:resqnet@db:5432/resqnet_demo
'@ | docker compose -f docker-compose.yml -f - up -d backend
```

## Export and restore

Export the validated demo DB and copy the binary archive out of the container.

```powershell
docker exec resqnet-db-1 pg_dump -U resqnet -d resqnet_demo -Fc -f /tmp/resqnet_demo.dump
docker cp resqnet-db-1:/tmp/resqnet_demo.dump .\resqnet_demo.dump
```

On the recipient machine, use the same PostGIS image and a fresh `resqnet_demo`.
The full snapshot includes the schema and extensions.

```powershell
docker exec resqnet-db-1 createdb -U resqnet -T template0 resqnet_demo
docker cp .\resqnet_demo.dump resqnet-db-1:/tmp/resqnet_demo.dump
docker exec resqnet-db-1 pg_restore -U resqnet -d resqnet_demo --no-owner --no-privileges --single-transaction --exit-on-error /tmp/resqnet_demo.dump
docker exec resqnet-db-1 psql -X -U resqnet -d resqnet_demo -v ON_ERROR_STOP=1 -c 'ALTER DATABASE resqnet_demo SET jit = off'
```
