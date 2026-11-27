# PROPOSAL: extend `disaster_areas` (for Ivan, not applied)

Status: **proposal only**. `models.py` is unchanged; `service/ingestion_flood.py` works with the current schema and keeps the extra fields in memory / GeoJSON until this is decided.

## Why

The EMSR444 AOI02 package (Chernivtsi, June 2020) has two kinds of area that the current table cannot tell apart:

- **observed flood**: 346 features ("Flooded area" 147, "Flood trace" 199), 75.4 km², imaged by SPOT-6/7 on 2020-06-26 09:12:48–09:13:00 UTC.
- **Not Analysed**: 92 polygons, 126.4 km² of the 1160 km² AOI. EMS did not map these, so there may or may not be water there.

If both go into `disaster_areas` as plain polygons, a dispatcher sees "unknown" as either "flooded" or, worse, as nothing at all, which reads as "safe". A route through an unknown area must never be marked verified. The dispatcher also needs to know how old a polygon is: a 3-day-old satellite image is not current water.

## Proposed columns

| column | type | why |
|---|---|---|
| `kind` | `String(32)`, not null, default `'observed_flood'` | `observed_flood` / `flood_trace` (optional split) / `not_analysed` / `modelled` / `reported`. Without it the Not Analysed mask cannot be stored at all. |
| `observed_at` | `DateTime(timezone=True)`, nullable | Acquisition time of the image (EMSR `src_date` + `source_tm`). Lets the UI show data age and lets routing treat old data as stale. |
| `valid_until` | `DateTime(timezone=True)`, nullable | When the polygon should stop being trusted (set by us or by a newer product). Null = no expiry set. |
| `source` | `String(255)`, nullable | e.g. `SPOT-6-7 (6 m, Post-event)`. Provenance for the dispatcher and for the pitch (Copernicus attribution). |
| `external_id` | `String(128)`, nullable, indexed | e.g. `EMSR444/AOI02/observedEventA:12#0`. Makes re-import idempotent (upsert instead of delete-by-name). |

## Geometry: POLYGON vs MULTIPOLYGON

EMSR features are mostly Polygon but some are MultiPolygon (2 of 346 here).

- **Keep `POLYGON` + explode** (what the ingester does now): simple, every row is one shape, works with the existing column. Cost: one EMSR feature becomes several rows (349 rows from 346 features), so we need `external_id` + part index to group them back.
- **Switch to `MULTIPOLYGON`**: one row per EMSR feature, no part bookkeeping, also fits dissolved masks. Cost: column type change (table re-create, fine at this stage), and every writer must wrap a Polygon into a MultiPolygon (`ST_Multi`).

Recommendation: switch to `MULTIPOLYGON` now while the table is empty. Either way is fine for the ingester; it already produces both.

## Exact diff (models.py)

```diff
+from datetime import datetime
+
 class DisasterArea(Base):
     __tablename__ = 'disaster_areas'
     id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
     name: Mapped[str] = mapped_column(String(255))
     disaster_id: Mapped[int] = mapped_column(Integer, ForeignKey('disasters.id'))
-    area: Mapped[object] = mapped_column(Geometry(geometry_type='POLYGON', srid=SRID))
+    area: Mapped[object] = mapped_column(Geometry(geometry_type='MULTIPOLYGON', srid=SRID))
+    # observed_flood | flood_trace | not_analysed | modelled | reported
+    kind: Mapped[str] = mapped_column(String(32), nullable=False, default='observed_flood',
+                                      server_default='observed_flood')
+    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
+    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
+    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
+    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
```

There are no migrations (tables come from `create_all`), so the change needs a fresh DB volume (`docker compose down -v`).

## Ingester changes once accepted

- write `kind`, `source`, `observed_at`, `external_id` from `FloodRecord` (fields already exist);
- write Not Analysed records too (`kind='not_analysed'`);
- with MULTIPOLYGON: one row per EMSR feature instead of per part.
