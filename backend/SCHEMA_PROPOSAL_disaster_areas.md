# Flood schema: `disaster_areas` and `analysis_coverage`

Status: **implemented** (this file started as a proposal). `database.init_db()` creates the schema and upgrades an older database in place, so no fresh volume is needed.

## Why

The EMSR444 AOI02 package (Chernivtsi, June 2020) contains three kinds of area:

- **observed flood**: 346 features ("Flooded area" / "Flood trace"), 75.5 km², imaged by SPOT-6/7 on 2020-06-26 09:12:48–09:13:00 UTC;
- **Not Analysed**: 92 polygons, 126.5 km², all inside the AOI. EMS did not map them, so there may or may not be water;
- **area of interest (AOI)**: one 1,161 km² polygon, the only area EMS analysed.

A missing flood polygon means "observed dry" only inside the analysed area, outside the Not Analysed areas, and only while the observation is current. Everywhere else nobody looked, or the look is too old, and nothing there may look safe.

## Append-only storage

An import never deletes or updates stored rows. Every row says what was seen and by whom (`source`, `product`), when the image was taken (`observed_at`), how long it counts (`valid_until`) and when it was loaded (`ingested_at`). So the history can be rebuilt: the rows with `ingested_at <= W` are what the system knew at wall-clock time W, and the rows with `observed_at <= T < valid_until` are what was current at moment T.

- **Same observation, stored once.** A row whose `(source, product, observed_at)` is already stored is skipped. Importing the same product again writes nothing; the CLI prints `Already stored, nothing written`.
- **A newer product is added next to the older one.** A new version or a monitoring product has a new product id and is appended. Until the older product expires, both count. A newer product can add flood or gaps, but it cannot hide older ones.
- **No `superseded_by` yet.** Data contract v2 does not define it (it was planned for v3, which was never written). Without it, nothing retracts an older observation before it expires, and that keeps the map on the safe side.
- Imports run under an advisory lock, so two at once cannot store the same product twice.

## Validity: `valid_until`

- `valid_until = observed_at + max age`. The max age is 24 h, both by default and at most. Data contract v2 §2 gives satellite data 24 h, measured from the observation, not from the load. Set it with `SATELLITE_MAX_AGE_HOURS` or `--max-age-hours`. Values outside 0 < h ≤ 24 are refused, never clamped.
- Unknown `observed_at` gives `valid_until = NULL`, which is never current.
- The analysis extent claims "observed dry" wherever its package has no flood and no Not Analysed polygon. So it ends at the earliest `valid_until` of those rows, and it gets NULL if one of them has no time.
- At `T = valid_until` a row is already stale.
- EMSR444 AOI02: flood and Not Analysed rows are valid until 2020-06-27 09:12:48 or 09:13:00 UTC (it depends on their image); the extent is valid until 2020-06-27 09:12:48 UTC.

## Reading: observations valid at T

```python
from service.ingestion_flood import observations_valid_at

found = observations_valid_at(session, datetime(2020, 6, 26, 12, 0, tzinfo=timezone.utc), disaster_id=1)
found.flood, found.analysis_extent, found.not_analysed  # ORM rows
```

It returns the rows with `observed_at <= at < valid_until`. `at` must be timezone-aware. `valid_at(model, at)` is the same condition as a SQLAlchemy clause, for joins. In SQL:

```sql
SELECT * FROM disaster_areas WHERE observed_at <= :t AND :t < valid_until;
SELECT * FROM analysis_coverage WHERE kind = 'analysis_extent' AND observed_at <= :t AND :t < valid_until;
```

On the local database, `at = 2020-06-26T12:00Z` returns 349 flood rows, 1 extent and 92 Not Analysed areas. `at = now` returns nothing, because all of them are stale.

Rules for consumers (map, routing, road status):

- Ground is "observed dry" at T only inside a returned `analysis_extent` and outside every returned flood and `not_analysed` area. Even then it is "dry as of `observed_at`", not "open now".
- Everything else is unknown, never safe. That includes ground under rows that are stale or have no time.
- `POST /disasterArea` sets no times yet, so its rows are never returned. Do not read their area as dry.

## Tables

### `disaster_areas` (existing table, new nullable columns)

Every row is a flooded or affected area. The geometry stays `POLYGON`: a MultiPolygon feature becomes several rows (349 rows from 346 features).

| column | type | meaning |
|---|---|---|
| `kind` | `varchar(32)` | `observed_flood` (EMS `observedEventA`). NULL on rows from `POST /disasterArea` and on rows from before the column existed |
| `observed_at` | `timestamptz` | acquisition time of the image (EMSR `source.dbf`: `src_date` + `source_tm`). NULL = unknown |
| `source` | `varchar(255)` | provider and image, e.g. `CEMS EMSR444 AOI02: SPOT-6-7 (6 m, Post-event)` |
| `product` | `varchar(128)` | product id from the zip name, e.g. `EMSR444_AOI02_DEL_PRODUCT_r1_RTP01_v1` |
| `valid_until` | `timestamptz` | see Validity. NULL = never current |
| `ingested_at` | `timestamptz` | when the row was written |

### `analysis_coverage` (new)

Where the analysis looked and where it could not. These rows are never flood and never go into `disaster_areas`.

| column | type | meaning |
|---|---|---|
| `id`, `name` | | `name` starts with `EMSR444 AOI02 ` like the flood rows |
| `disaster_id` | `int`, FK `disasters` | |
| `kind` | `varchar(32)`, CHECK in (`analysis_extent`, `not_analysed`) | a misspelt kind would silently make ground look observed, so the database rejects it |
| `area` | `geometry(MultiPolygon, 4326)` | one row per EMS feature for the extent, one per polygon part for Not Analysed |
| `observed_at`, `source`, `product`, `valid_until`, `ingested_at` | as above | |

- `analysis_extent`: EMS `areaOfInterestA`, 1 row. The layer has no source field, so `observed_at` is the earliest post-event image time (2020-06-26 09:12:48 UTC). It is NULL if any post-event image has no usable date.
- `not_analysed`: EMS `imageFootprintA` rows with `obj_type = "Not Analysed"`, 92 rows.

### `disasters` (unchanged)

The importer get-or-creates `EMSR444 Chernivtsi flood 2020-06`. Its `creation_time` is the earliest observation of the first import (2020-06-26 09:12:48), or the import time when no observation time is known, so it is never NULL. Later imports leave it alone.

## Import rules (`service/ingestion_flood.py`)

- A package with no flood polygon is refused (`ImportRefused`) and nothing is written. Otherwise its analysed area would be stored as "observed: no water".
- An `imageFootprintA.obj_type` other than "Image Footprint" or "Not Analysed" is refused. An unrecognised Not Analysed area would otherwise look observed.
- The old guard against shrinking the stored layer is gone. In append-only storage nothing is removed, and a newer product with less flood is a real observation. The older product still counts until it expires.
- One transaction, rolled back on any error.
- An unparsable or impossible date (`31/02/2020`) gives `observed_at = NULL` and a warning, never a guess. An unusable time keeps the date at 00:00 UTC, which is earlier than the real time, so the row expires early rather than late.
- Environment: `EMSR_ZIP`, `EMSR_OUT_DIR`, `SATELLITE_MAX_AGE_HOURS`.

## Upgrading older databases

`init_db()` takes an advisory lock and runs `create_all` (new tables). It adds the columns listed in `database.ADDED_COLUMNS` with `ADD COLUMN IF NOT EXISTS` (nullable only, skipped when present), and the `connected_road_id` indexes. All of it is idempotent and in one transaction. To add a column to an existing table, add it to the model as nullable and add its name to `ADDED_COLUMNS`.

Rows written before `valid_until` existed keep it NULL, so they never count as current. Import the product again to get current rows; delete the old rows by hand first if they should not stay in the history.

## Not done

- `superseded_by` (see above).
- The product delivery time (contract v2 `source_available_at`) is not in the vector package. With it, a replay could hide a product until it was published: AOI02 came about 9.5–12.5 h after the image.
- `disaster_areas` stays `POLYGON`, without a switch to `MULTIPOLYGON`.
- No road status and no API fields for the new columns. Those belong to the road-status and API work, which reads the tables through `observations_valid_at`.
