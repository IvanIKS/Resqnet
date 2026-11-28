# Flood and Road Ingestion

Author: Artem (grsrse)

## Purpose

ResQNet loads the Copernicus EMS EMSR444 AOI02 flood delineation for Chernivtsi, June 2020, into PostGIS. The package provides observed flood polygons, an analysis extent, and areas marked **Not Analysed**. OSMnx brings in the OpenStreetMap drivable road network around the same area.

## Data Flow

The flood importer reads the vector ZIP directly and stores geometry in WGS84 (EPSG:4326). The `source.dbf` table supplies image provenance and acquisition time. The product ID comes from the ZIP name.

| ZIP layer | PostGIS result for AOI02 |
| --- | --- |
| `observedEventA` | 346 source features become 349 polygon rows in `disaster_areas` (`observed_flood`) after geometry repair and splitting into polygon parts. |
| `areaOfInterestA` | One extent row in `analysis_coverage` (`analysis_extent`). |
| `imageFootprintA` | 92 **Not Analysed** rows in `analysis_coverage` (`not_analysed`). The three image footprint outlines are skipped. |

The flood writer refuses a package without any flood polygons. The OSMnx graph populates `roads`, `road_end1_connections`, and `road_end2_connections`.

## Time Model

- `observed_at` is the image acquisition time in UTC. A usable date without a usable time gets midnight UTC. An unknown date gives NULL. The extent takes the earliest post-event image time when all post-event dates are known.
- `valid_until` defaults to image time plus 24 hours. The window can be shortened. The extent expires no later than any flood or Not Analysed record from its package. An unknown observation time in those layers leaves the extent without a validity window.
- `ingested_at` records when the row was loaded. Flood and coverage rows are append-only. Re-importing the same observations is a no-op because the writer skips stored `(source, product, observed_at)` combinations.
- `valid_at` supplies the SQL filter `observed_at <= at < valid_until`. `observations_valid_at` requires a timezone-aware `at` and returns matching flood, extent, and Not Analysed rows. NULL in either time bound never matches.

## Roads

OSMnx downloads the simplified drive graph for the AOI02 bounding box with a 0.05° buffer by default and keeps disconnected components. The importer merges matching reverse edges for two-way streets and keeps distinct parallel segments. Each road stores a line, two endpoints, and OSM node and way IDs where available. The two connection tables record which segments share the OSM node at each end.

`oneway=True` means travel from `end1` to `end2`. `False` means both directions. NULL means the direction is unknown. Overpass responses are cached in `OSM_CACHE_DIR`, which defaults to `~/.cache/resqnet/osm`.

## How to Run

From `backend/`:

```sh
python -m service.ingestion_flood --dry-run
python -m service.ingestion_flood --dry-run --export-geojson ../out/
python -m service.ingestion_flood
python -m service.ingestion_flood --zip path/to/EMSR444_AOI02_DEL_PRODUCT_r1_RTP01_v1_vector.zip --max-age-hours 12
python -m service.ingestion_roads --dry-run --buffer 0.05
python -m service.ingestion_roads --replace
python -m service.ingestion_roads --append
python -m service.ingestion_roads --replace --force
```

Set `EMSR_ZIP` to select the ZIP by default. `--export-geojson` without a directory uses `EMSR_OUT_DIR` or the sibling `out/` directory. The export writes the records and, when present, a dissolved Not Analysed mask. Use `--dry-run` to skip PostGIS writes.

Road imports replace the stored graph by default. `--append` requires a network with no OSM nodes shared with stored roads. `--force` allows replacement with fewer than half the stored road count. Empty networks are always refused.

## Tests

- `test_ingestion_flood.py` checks parsing, geometry, provenance, coverage, validity, and write logic, with a real ZIP check when the file is available.
- `test_flood_db.py` checks PostGIS schema upgrades, history, duplicate imports, validity boundaries, refusal and rollback, and replay of the real package when available.
- `test_ingestion_roads.py` checks segment and junction construction, direction, cache configuration, and CLI modes. Its PostGIS test checks replacement on rerun and rollback after a failed write.

## Limitations

The package represents one flood snapshot from 26 June 2020. It supplies image acquisition times and has no delivery timestamp. Within valid coverage, outside flood and Not Analysed polygons, an empty flood layer at a location means “no water mapped on the image.” Road passability requires separate evidence. Outside valid analysed coverage, the flood state is unknown. Evacuation routing uses junction adjacency and does not enforce `oneway`.
