# RESQNET historical flood demo

From `backend/` in the Windows checkout, use the backend `.venv` and the running
`resqnet-db-1` PostGIS container on localhost port 5432. The container needs the
`template_postgis` database. The builder takes two pinned inputs. Pass each one
as an option or set its environment variable. The option wins.

| Input | Option | Environment variable | SHA-256 |
| --- | --- | --- | --- |
| `resqnet_aoi02_seed.dump` (OSM road graph) | `--dump` | `RESQNET_DEMO_DUMP` | `0fe6447cb152cda3d5e43131cffdbb693e38fb7b654a64ea3ba456bc3d6cfbc4` |
| `EMSR444_AOI02_DEL_PRODUCT_r1_RTP01_v1_vector.zip` (CEMS) | `--zip` | `RESQNET_DEMO_ZIP` | `ef4db492680aeadb8d49cbdb88927e1bda74672142a931cd872c3652cb807759` |

The builder stops before it touches the database if an input is missing or its
SHA-256 differs, or if tracked files have uncommitted changes. With `--allow-dirty`
it builds anyway and records `"git_dirty": true` and `git_diff_sha256`, the
SHA-256 of `git diff HEAD`, in the fixture. A clean build records
`"git_dirty": false`. It then drops `resqnet_demo` with `dropdb --force`, which
also ends open connections such as a running API, and recreates it.

Example with the current input paths on the Windows build machine:

```powershell
$env:DATABASE_URL = 'postgresql+psycopg2://resqnet:resqnet@localhost:5432/resqnet_demo'
.\.venv\Scripts\python.exe -B -m demo.build_demo_db `
  --dump D:\cassini-review\out\resqnet_aoi02_seed.dump `
  --zip D:\cassini-2026\data\raw\EMSR444_AOI02_DEL_PRODUCT_r1_RTP01_v1_vector.zip `
  --fixture-out D:\cassini-2026\analysis\21-demo-fixture.json
```

The fixture JSON always goes to stdout. The builder also writes it to a file
only with `--fixture-out` or `RESQNET_DEMO_FIXTURE_OUT`. The fixture records
the input hashes, Git revision and dirty flag, replay time, viewport, selected
click, point and route expectations.

Restart any running API, local or Compose, after a rebuild. Its road-graph
cache lives in the API process, and a rebuild does not refresh it.

The expected numbers below were measured on the `postgis/postgis:16-3.4` image
from `docker-compose.yml` (PostgreSQL 16.4, PostGIS 3.4.3). Counts and road IDs
must match exactly. The fallback route distance of 3,610.40 m may differ by
less than 0.5 m.

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

From `backend/`, start the API after the final rebuild. If it was already
running, restart it.

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
