"""Ingest the Copernicus EMS flood delineation EMSR444 AOI02 (Chernivtsi, June 2020).

What the vector package contains (checked on the real zip, all layers EPSG:4326):

* ``observedEventA``  346 polygons (344 Polygon + 2 MultiPolygon). Fields:
  ``event_type`` (always "5-Flood"), ``obj_desc``, ``det_method``,
  ``notation`` ("Flooded area" | "Flood trace"), ``dmg_src_id`` (-> source),
  ``area`` (hectares). There is no per-object id, so we use the row index.
* ``imageFootprintA`` 95 polygons. ``obj_type`` is "Image Footprint" (3 rows)
  or "Not Analysed" (92 rows); ``or_src_id`` points to the source table.
  The "Not Analysed" rows are the areas EMS did not map: unknown, not dry.
* ``source`` (dbf only, no geometry): ``src_id``, ``source_nam``,
  ``src_date`` ("DD/MM/YYYY" or "Not Applicable" or a bare year),
  ``source_tm`` ("THH:MM:SSZ" or "Not Applicable"), ``sensor_gsd``,
  ``eventphase``.

Each feature becomes a ``FloodRecord`` (one per polygon part). Only observed
flood polygons fit the current ``disaster_areas`` table; the "Not Analysed"
mask has no column yet (see SCHEMA_PROPOSAL_disaster_areas.md) and can be
exported to GeoJSON instead.

CLI, run from ``backend/``::

    python -m service.ingestion_flood --dry-run
    python -m service.ingestion_flood --export-geojson D:/cassini-review/out/
    python -m service.ingestion_flood            # writes to the database
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio
from shapely import make_valid
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry

DEFAULT_ZIP = Path("D:/cassini-2026/data/raw/EMSR444_AOI02_DEL_PRODUCT_r1_RTP01_v1_vector.zip")
DEFAULT_OUT_DIR = Path("D:/cassini-review/out/")
# Repo root (backend/..): exports must never land inside it.
REPO_ROOT = Path(__file__).resolve().parents[2]

LAYER_PREFIX = "EMSR444_AOI02_DEL_PRODUCT_"
EMSR_ID = "EMSR444"
AOI_ID = "AOI02"
DISASTER_NAME = "EMSR444 Chernivtsi flood 2020-06"
DISASTER_TYPE_NAME = "disaster"  # seeded by seed.py

KIND_OBSERVED_FLOOD = "observed_flood"
KIND_NOT_ANALYSED = "not_analysed"

WGS84 = "EPSG:4326"


@dataclass
class SourceInfo:
    """One row of the EMSR ``source`` table."""

    src_id: int
    name: str
    observed_at: datetime | None  # UTC
    has_time: bool  # False when only the date was given
    sensor_gsd: str | None
    event_phase: str | None

    def label(self) -> str:
        details = ", ".join(x for x in (self.sensor_gsd, self.event_phase) if x)
        return f"{self.name} ({details})" if details else self.name


@dataclass
class FloodRecord:
    """Everything we want to keep about one polygon, whatever the DB can hold today."""

    name: str
    geometry: Polygon
    kind: str  # KIND_OBSERVED_FLOOD | KIND_NOT_ANALYSED
    source: str | None
    observed_at: datetime | None  # UTC
    emsr_obj_id: str  # "<layer>:<row index>"
    part: int  # index of the polygon part inside the original (multi)polygon
    notation: str | None = None  # "Flooded area" / "Flood trace" for floods
    valid_until: datetime | None = None
    observed_has_time: bool = field(default=True)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

_NOT_APPLICABLE = {"", "not applicable", "n/a", "none"}
_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")  # DD/MM/YYYY
_TIME_RE = re.compile(r"^T?(\d{1,2}):(\d{2})(?::(\d{2}))?Z?$")  # THH:MM[:SS]Z


def _clean(value) -> str | None:
    """Turn EMSR placeholders ("Not Applicable", NaN) into None."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return None if text.lower() in _NOT_APPLICABLE else text


def parse_observed_at(date_str, time_str) -> tuple[datetime | None, bool]:
    """Combine EMSR ``src_date`` + ``source_tm`` into a UTC datetime.

    Returns ``(datetime, has_time)``. Missing time -> midnight UTC of that
    date with ``has_time=False``. Missing or unparseable date (e.g. a bare
    year such as "2010") -> ``(None, False)``.
    """
    date_text = _clean(date_str)
    if date_text is None:
        return None, False
    m = _DATE_RE.match(date_text)
    if not m:
        return None, False
    day, month, year = (int(g) for g in m.groups())

    hour = minute = second = 0
    has_time = False
    time_text = _clean(time_str)
    if time_text is not None:
        t = _TIME_RE.match(time_text)
        if t:
            hour, minute = int(t.group(1)), int(t.group(2))
            second = int(t.group(3) or 0)
            has_time = True
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc), has_time


def load_sources(source_df: pd.DataFrame) -> dict[int, SourceInfo]:
    """Index the EMSR source table by ``src_id``."""
    sources: dict[int, SourceInfo] = {}
    for row in source_df.itertuples(index=False):
        observed_at, has_time = parse_observed_at(row.src_date, row.source_tm)
        sources[int(row.src_id)] = SourceInfo(
            src_id=int(row.src_id),
            name=_clean(row.source_nam) or f"source {row.src_id}",
            observed_at=observed_at,
            has_time=has_time,
            sensor_gsd=_clean(getattr(row, "sensor_gsd", None)),
            event_phase=_clean(getattr(row, "eventphase", None)),
        )
    return sources


def polygon_parts(geom: BaseGeometry | None) -> list[Polygon]:
    """Make a geometry valid and return its non-empty Polygon parts.

    make_valid can turn a self-intersecting polygon into a MultiPolygon or a
    GeometryCollection with stray lines/points; only areas are kept.
    """
    if geom is None or geom.is_empty:
        return []
    if not geom.is_valid:
        geom = make_valid(geom)
    if isinstance(geom, Polygon):
        return [geom] if not geom.is_empty else []
    if isinstance(geom, MultiPolygon) or hasattr(geom, "geoms"):
        parts: list[Polygon] = []
        for sub in geom.geoms:
            parts.extend(polygon_parts(sub))
        return parts
    return []  # lines, points


def _to_wgs84(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if gdf.crs is None:
        raise ValueError("layer has no CRS; refusing to guess")
    return gdf if gdf.crs.to_string() == WGS84 else gdf.to_crs(WGS84)


def _stamp(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%dT%H:%MZ") if dt else "time-unknown"


# ---------------------------------------------------------------------------
# Layer -> records
# ---------------------------------------------------------------------------

def build_flood_records(events: gpd.GeoDataFrame, sources: dict[int, SourceInfo]) -> list[FloodRecord]:
    """observedEventA -> one FloodRecord per valid polygon part."""
    events = _to_wgs84(events)
    records: list[FloodRecord] = []
    for idx, row in events.iterrows():
        src = sources.get(int(row["dmg_src_id"])) if pd.notna(row.get("dmg_src_id")) else None
        notation = _clean(row.get("notation"))
        observed_at = src.observed_at if src else None
        for part_no, poly in enumerate(polygon_parts(row.geometry)):
            label = f" ({notation})" if notation else ""
            records.append(FloodRecord(
                name=f"{EMSR_ID} {AOI_ID} observed flood{label} {_stamp(observed_at)} #{idx}.{part_no}",
                geometry=poly,
                kind=KIND_OBSERVED_FLOOD,
                source=src.label() if src else None,
                observed_at=observed_at,
                observed_has_time=src.has_time if src else False,
                emsr_obj_id=f"observedEventA:{idx}",
                part=part_no,
                notation=notation,
            ))
    return records


def build_not_analysed_records(footprints: gpd.GeoDataFrame, sources: dict[int, SourceInfo]) -> list[FloodRecord]:
    """imageFootprintA rows with obj_type == "Not Analysed" -> unknown-mask records.

    "Image Footprint" rows are only the satellite scene outlines and are skipped.
    """
    footprints = _to_wgs84(footprints)
    mask = footprints["obj_type"].astype(str).str.strip().str.lower() == "not analysed"
    records: list[FloodRecord] = []
    for idx, row in footprints[mask].iterrows():
        src = sources.get(int(row["or_src_id"])) if pd.notna(row.get("or_src_id")) else None
        observed_at = src.observed_at if src else None
        for part_no, poly in enumerate(polygon_parts(row.geometry)):
            records.append(FloodRecord(
                name=f"{EMSR_ID} {AOI_ID} not analysed {_stamp(observed_at)} #{idx}.{part_no}",
                geometry=poly,
                kind=KIND_NOT_ANALYSED,
                source=src.label() if src else None,
                observed_at=observed_at,
                observed_has_time=src.has_time if src else False,
                emsr_obj_id=f"imageFootprintA:{idx}",
                part=part_no,
            ))
    return records


def read_package(zip_path: Path = DEFAULT_ZIP) -> list[FloodRecord]:
    """Read the EMSR zip directly (no extraction) and return all records."""
    def member(layer: str, ext: str = "shp") -> str:
        return f"zip://{Path(zip_path).as_posix()}!{LAYER_PREFIX}{layer}_r1_v1.{ext}"

    sources = load_sources(pyogrio.read_dataframe(member("source", "dbf"), read_geometry=False))
    events = gpd.read_file(member("observedEventA"))
    footprints = gpd.read_file(member("imageFootprintA"))
    return build_flood_records(events, sources) + build_not_analysed_records(footprints, sources)


def not_analysed_mask(records: list[FloodRecord]) -> BaseGeometry | None:
    """Dissolved "unknown" area. Never to be rendered as safe."""
    parts = [r.geometry for r in records if r.kind == KIND_NOT_ANALYSED]
    if not parts:
        return None
    return gpd.GeoSeries(parts, crs=WGS84).union_all()


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

def records_to_gdf(records: list[FloodRecord]) -> gpd.GeoDataFrame:
    rows = [{
        "name": r.name,
        "kind": r.kind,
        "source": r.source,
        "observed_at": r.observed_at.isoformat() if r.observed_at else None,
        "observed_has_time": r.observed_has_time,
        "valid_until": r.valid_until.isoformat() if r.valid_until else None,
        "emsr_obj_id": r.emsr_obj_id,
        "part": r.part,
        "notation": r.notation,
    } for r in records]
    return gpd.GeoDataFrame(rows, geometry=[r.geometry for r in records], crs=WGS84)


def summarize(records: list[FloodRecord]) -> dict:
    """Counts, km² (in a metric UTM CRS), observed_at range, mask area."""
    gdf = records_to_gdf(records)
    if gdf.empty:
        return {"records": 0}
    metric = gdf.to_crs(gdf.estimate_utm_crs())
    flood = metric[metric["kind"] == KIND_OBSERVED_FLOOD]
    na = metric[metric["kind"] == KIND_NOT_ANALYSED]
    flood_union = flood.union_all() if not flood.empty else None
    na_union = na.union_all() if not na.empty else None
    times = [r.observed_at for r in records if r.kind == KIND_OBSERVED_FLOOD and r.observed_at]
    return {
        "metric_crs": metric.crs.to_string(),
        "flood_records": len(flood),
        "flood_source_objects": flood["emsr_obj_id"].nunique(),
        "flood_by_notation": flood["notation"].value_counts(dropna=False).to_dict(),
        "flood_area_km2": flood.area.sum() / 1e6,
        "flood_area_dissolved_km2": flood_union.area / 1e6 if flood_union is not None else 0.0,
        "observed_at_min": min(times) if times else None,
        "observed_at_max": max(times) if times else None,
        "flood_without_time": sum(1 for r in records if r.kind == KIND_OBSERVED_FLOOD and not r.observed_has_time),
        "not_analysed_records": len(na),
        "not_analysed_mask_km2": na_union.area / 1e6 if na_union is not None else 0.0,
        "flood_inside_mask_km2": (flood_union.intersection(na_union).area / 1e6
                                  if flood_union is not None and na_union is not None else 0.0),
    }


def export_geojson(records: list[FloodRecord], out_dir: Path = DEFAULT_OUT_DIR) -> list[Path]:
    """Write all records and the dissolved Not Analysed mask as GeoJSON."""
    out_dir = Path(out_dir).resolve()
    if out_dir.is_relative_to(REPO_ROOT):
        raise ValueError(f"refusing to export data inside the repo: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)

    all_path = out_dir / "emsr444_aoi02_records.geojson"
    records_to_gdf(records).to_file(all_path, driver="GeoJSON")
    written = [all_path]

    mask = not_analysed_mask(records)
    if mask is not None:
        mask_path = out_dir / "emsr444_aoi02_not_analysed_mask.geojson"
        gpd.GeoDataFrame(
            [{"kind": KIND_NOT_ANALYSED, "meaning": "unknown: not analysed by Copernicus EMS, NOT safe"}],
            geometry=[mask], crs=WGS84,
        ).to_file(mask_path, driver="GeoJSON")
        written.append(mask_path)
    return written


def write_to_db(session, records: list[FloodRecord], replace: bool = True) -> int:
    """Insert observed flood polygons into ``disaster_areas``.

    Get-or-creates the disaster type and the ``Disaster``. Not Analysed
    records are deliberately NOT written: the table has no ``kind`` column, so
    they would be indistinguishable from flood. With ``replace`` the earlier
    rows of this package (same disaster, same name prefix) are deleted first,
    so the import can be re-run. Commits and returns the number inserted.
    """
    from geoalchemy2.shape import from_shape
    from sqlalchemy import delete, select

    from models import Disaster, DisasterArea, DisasterType

    flood = [r for r in records if r.kind == KIND_OBSERVED_FLOOD]
    times = [r.observed_at for r in flood if r.observed_at]
    # DateTime column is timezone-naive: store UTC without tzinfo.
    creation_time = min(times).replace(tzinfo=None) if times else None

    dtype = session.scalar(select(DisasterType).where(DisasterType.name == DISASTER_TYPE_NAME))
    if dtype is None:
        dtype = DisasterType(name=DISASTER_TYPE_NAME)
        session.add(dtype)
        session.flush()

    disaster = session.scalar(select(Disaster).where(Disaster.name == DISASTER_NAME))
    if disaster is None:
        disaster = Disaster(name=DISASTER_NAME, creation_time=creation_time, disaster_type=dtype.id)
        session.add(disaster)
        session.flush()

    if replace:
        session.execute(delete(DisasterArea).where(
            DisasterArea.disaster_id == disaster.id,
            DisasterArea.name.like(f"{EMSR_ID} {AOI_ID} %"),
        ))

    for r in flood:
        session.add(DisasterArea(
            name=r.name[:255],
            disaster_id=disaster.id,
            area=from_shape(r.geometry, srid=4326),
        ))
    session.commit()
    return len(flood)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_summary(summary: dict) -> None:
    for key, value in summary.items():
        if isinstance(value, float):
            value = f"{value:.3f}"
        elif isinstance(value, datetime):
            value = value.isoformat()
        print(f"  {key}: {value}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest EMSR444 AOI02 flood polygons into disaster_areas.")
    parser.add_argument("--zip", type=Path, default=DEFAULT_ZIP, help="EMSR vector package zip")
    parser.add_argument("--dry-run", action="store_true", help="only read and print a summary")
    parser.add_argument("--export-geojson", type=Path, nargs="?", const=DEFAULT_OUT_DIR, default=None,
                        metavar="DIR", help=f"export records + Not Analysed mask (default dir {DEFAULT_OUT_DIR})")
    args = parser.parse_args(argv)

    if not args.zip.exists():
        print(f"EMSR package not found: {args.zip}", file=sys.stderr)
        return 1

    records = read_package(args.zip)
    print(f"Read {args.zip.name}")
    _print_summary(summarize(records))

    if args.export_geojson is not None:
        for path in export_geojson(records, args.export_geojson):
            print(f"  exported: {path}")

    if args.dry_run:
        print("Dry run: database not touched.")
        return 0

    from database import SessionLocal  # imported late: needs a reachable PostGIS

    with SessionLocal() as session:
        inserted = write_to_db(session, records)
    print(f"Inserted {inserted} observed flood polygons into disaster_areas "
          f"(Not Analysed mask not stored: no column for it yet).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
