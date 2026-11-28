"""Ingest the Copernicus EMS flood delineation EMSR444 AOI02 (Chernivtsi, June 2020).

What the vector package contains (checked on the real zip, all layers EPSG:4326):

* ``observedEventA``  346 polygons (344 Polygon + 2 MultiPolygon). Fields:
  ``event_type`` (always "5-Flood"), ``obj_desc``, ``det_method``,
  ``notation`` ("Flooded area" | "Flood trace"), ``dmg_src_id`` (-> source),
  ``area`` (hectares). There is no per-object id, so we use the row index.
* ``imageFootprintA`` 95 polygons. ``obj_type`` is "Image Footprint" (3 rows)
  or "Not Analysed" (92 rows); ``or_src_id`` points to the source table.
  The "Not Analysed" rows are the areas EMS did not map: unknown, not dry.
* ``areaOfInterestA`` 1 polygon, 1,161 km²: the area EMS analysed. Outside it
  nothing is known. It has no source field.
* ``source`` (dbf only, no geometry): ``src_id``, ``source_nam``,
  ``src_date`` ("DD/MM/YYYY" or "Not Applicable" or a bare year),
  ``source_tm`` ("THH:MM:SSZ" or "Not Applicable"), ``sensor_gsd``,
  ``eventphase``.

Each feature becomes a ``FloodRecord`` (one per polygon part; the AOI stays
whole). ``write_to_db`` appends observed flood to ``disaster_areas`` and the
AOI ("analysis_extent") plus the "Not Analysed" areas to
``analysis_coverage``, each row with ``source``, ``product``, ``observed_at``
(image time), ``valid_until`` and ``ingested_at``.

Storage is append-only: an import never deletes or updates stored rows, so
what was observed, and when it was loaded, stays in the database. A product
that is already stored (same source, product and observed_at) is not stored
again. ``observations_valid_at(session, at)`` returns the rows that count at
moment ``at`` (observed_at <= at < valid_until): a past moment replays the
flood, now gives the live state.

``valid_until`` = observed_at + SATELLITE_MAX_AGE_HOURS (default and maximum
24 h, measured from the image, not from the load: RESQNET data contract v2
§2, "satellite 24 h"). Unknown observed_at gives NULL: never current.

The package path and the export directory come from ``EMSR_ZIP`` and
``EMSR_OUT_DIR``; the defaults sit next to the checkout, never inside it.

CLI, run from ``backend/``::

    python -m service.ingestion_flood --dry-run
    python -m service.ingestion_flood --export-geojson D:/cassini-review/out/
    python -m service.ingestion_flood                     # appends to the database
    python -m service.ingestion_flood --max-age-hours 12  # shorter validity
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio
from shapely import make_valid, union_all
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry

logger = logging.getLogger("resqnet.ingestion_flood")


def checkout_root(backend_dir: Path) -> Path:
    """The checkout (backend/..), or backend/ itself when it sits directly
    under the filesystem root (Docker: /app). Exports must never land inside it."""
    return backend_dir if backend_dir.parent == type(backend_dir)(backend_dir.anchor) else backend_dir.parent


ZIP_NAME = "EMSR444_AOI02_DEL_PRODUCT_r1_RTP01_v1_vector.zip"
BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = checkout_root(BACKEND_DIR)
DEFAULT_ZIP = Path(os.getenv("EMSR_ZIP") or REPO_ROOT.parent / "data" / ZIP_NAME)
DEFAULT_OUT_DIR = Path(os.getenv("EMSR_OUT_DIR") or REPO_ROOT.parent / "out")

LAYER_PREFIX = "EMSR444_AOI02_DEL_PRODUCT_"
EMSR_ID = "EMSR444"
AOI_ID = "AOI02"
DISASTER_NAME = "EMSR444 Chernivtsi flood 2020-06"
DISASTER_TYPE_NAME = "disaster"  # seeded by seed.py
PROVIDER = "CEMS"  # Copernicus EMS rapid mapping; first word of every source string

# Same strings as models.AREA_KIND_* / models.COVERAGE_KIND_*.
KIND_OBSERVED_FLOOD = "observed_flood"
KIND_NOT_ANALYSED = "not_analysed"
KIND_ANALYSIS_EXTENT = "analysis_extent"
COVERAGE_KINDS = (KIND_ANALYSIS_EXTENT, KIND_NOT_ANALYSED)

# imageFootprintA.obj_type values we can classify (lower case). Any other
# value is refused: it could be a Not Analysed area under another name.
FOOTPRINT_TYPES = {"image footprint", "not analysed"}

# How long satellite data count after the image was taken: contract v2 §2
# gives 24 h for satellite data, and requirements T-03 makes it the maximum.
MAX_AGE_ENV = "SATELLITE_MAX_AGE_HOURS"
MAX_AGE_LIMIT = timedelta(hours=24)

WGS84 = "EPSG:4326"


class ImportRefused(RuntimeError):
    """The import would make the map look safer than the data allows."""


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
    geometry: Polygon | MultiPolygon  # MultiPolygon only for a multi-part analysis extent
    kind: str  # KIND_OBSERVED_FLOOD | KIND_NOT_ANALYSED | KIND_ANALYSIS_EXTENT
    source: str | None
    observed_at: datetime | None  # UTC
    emsr_obj_id: str  # "<layer>:<row index>"
    part: int  # index of the polygon part inside the original (multi)polygon
    notation: str | None = None  # "Flooded area" / "Flood trace" for floods
    valid_until: datetime | None = None  # set by assign_validity
    observed_has_time: bool = field(default=True)
    product: str | None = None  # e.g. "EMSR444_AOI02_DEL_PRODUCT_r1_RTP01_v1", set by read_package


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

    Returns ``(datetime, has_time)``. Missing or unusable time -> midnight UTC
    of that date with ``has_time=False`` (earlier than the real time, so the
    data never looks fresher than it is). Missing or unparseable date (e.g. a
    bare year such as "2010") or an impossible one ("31/02/2020", logged) ->
    ``(None, False)``: unknown, never a guess.
    """
    date_text = _clean(date_str)
    if date_text is None:
        return None, False
    m = _DATE_RE.match(date_text)
    if not m:
        return None, False
    day, month, year = (int(g) for g in m.groups())
    try:
        date = datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        logger.warning("impossible source date %r: observation time unknown", date_text)
        return None, False

    time_text = _clean(time_str)
    if time_text is None:
        return date, False
    t = _TIME_RE.match(time_text)
    if t:
        try:
            return date.replace(hour=int(t.group(1)), minute=int(t.group(2)), second=int(t.group(3) or 0)), True
        except ValueError:
            pass
    logger.warning("unusable source time %r on %s: using the start of that day", time_text, date_text)
    return date, False


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


def source_label(*sources: SourceInfo | None) -> str:
    """"CEMS EMSR444 AOI02: <image(s)>"; without the images when none is known."""
    images = "; ".join(sorted({s.label() for s in sources if s is not None}))
    base = f"{PROVIDER} {EMSR_ID} {AOI_ID}"
    return f"{base}: {images}" if images else base


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
                source=source_label(src),
                observed_at=observed_at,
                observed_has_time=src.has_time if src else False,
                emsr_obj_id=f"observedEventA:{idx}",
                part=part_no,
                notation=notation,
            ))
    return records


def build_not_analysed_records(footprints: gpd.GeoDataFrame, sources: dict[int, SourceInfo]) -> list[FloodRecord]:
    """imageFootprintA rows with obj_type == "Not Analysed" -> unknown-mask records.

    "Image Footprint" rows are only the satellite scene outlines and are
    skipped. Any other obj_type raises ``ImportRefused``: if it meant "not
    analysed", skipping it would make that ground look observed.
    """
    footprints = _to_wgs84(footprints)
    obj_type = footprints["obj_type"].astype(str).str.strip().str.lower()
    unknown = sorted(set(obj_type) - FOOTPRINT_TYPES)
    if unknown:
        raise ImportRefused(f"imageFootprintA has obj_type {unknown}: cannot tell whether that ground was "
                            "analysed; refusing (a skipped Not Analysed area would look observed)")
    records: list[FloodRecord] = []
    for idx, row in footprints[obj_type == "not analysed"].iterrows():
        src = sources.get(int(row["or_src_id"])) if pd.notna(row.get("or_src_id")) else None
        observed_at = src.observed_at if src else None
        for part_no, poly in enumerate(polygon_parts(row.geometry)):
            records.append(FloodRecord(
                name=f"{EMSR_ID} {AOI_ID} not analysed {_stamp(observed_at)} #{idx}.{part_no}",
                geometry=poly,
                kind=KIND_NOT_ANALYSED,
                source=source_label(src),
                observed_at=observed_at,
                observed_has_time=src.has_time if src else False,
                emsr_obj_id=f"imageFootprintA:{idx}",
                part=part_no,
            ))
    return records


def build_extent_records(aoi: gpd.GeoDataFrame, sources: dict[int, SourceInfo]) -> list[FloodRecord]:
    """areaOfInterestA -> one analysis-extent record per AOI feature, kept whole.

    The layer has no source field. The extent was analysed on the post-event
    images, so it gets their earliest acquisition time, or none at all if any
    of them has no usable date: never a guessed, fresher time.
    """
    aoi = _to_wgs84(aoi)
    post = [s for s in sources.values() if (s.event_phase or "").lower().startswith("post")]
    first = (min(post, key=lambda s: s.observed_at)
             if post and all(s.observed_at is not None for s in post) else None)
    observed_at = first.observed_at if first else None
    records: list[FloodRecord] = []
    for idx, row in aoi.iterrows():
        parts = polygon_parts(row.geometry)
        if not parts:
            continue
        records.append(FloodRecord(
            name=f"{EMSR_ID} {AOI_ID} analysis extent {_stamp(observed_at)} #{idx}",
            geometry=union_all(parts),
            kind=KIND_ANALYSIS_EXTENT,
            source=source_label(*post),
            observed_at=observed_at,
            observed_has_time=first.has_time if first else False,
            emsr_obj_id=f"areaOfInterestA:{idx}",
            part=0,
        ))
    return records


def product_id(zip_path: Path) -> str:
    """EMSR444_AOI02_DEL_PRODUCT_r1_RTP01_v1_vector.zip -> EMSR444_AOI02_DEL_PRODUCT_r1_RTP01_v1."""
    name = Path(zip_path).name
    stem = name[:-4] if name.lower().endswith(".zip") else name
    return stem.removesuffix("_vector")


def read_package(zip_path: Path = DEFAULT_ZIP) -> list[FloodRecord]:
    """Read the EMSR zip directly (no extraction) and return all records."""
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise FileNotFoundError(f"EMSR package not found: {zip_path} (set EMSR_ZIP to its path)")

    def member(layer: str, ext: str = "shp") -> str:
        return f"zip://{zip_path.as_posix()}!{LAYER_PREFIX}{layer}_r1_v1.{ext}"

    sources = load_sources(pyogrio.read_dataframe(member("source", "dbf"), read_geometry=False))
    events = gpd.read_file(member("observedEventA"))
    footprints = gpd.read_file(member("imageFootprintA"))
    aoi = gpd.read_file(member("areaOfInterestA"))
    records = (build_flood_records(events, sources)
               + build_not_analysed_records(footprints, sources)
               + build_extent_records(aoi, sources))
    product = product_id(zip_path)
    for r in records:
        r.product = product
    return records


def earliest_observation(records: list[FloodRecord]) -> datetime | None:
    """Earliest ``observed_at`` of the records; None if any of them is unknown."""
    times = [r.observed_at for r in records]
    if not times or any(t is None for t in times):
        return None
    return min(times)


def not_analysed_mask(records: list[FloodRecord]) -> BaseGeometry | None:
    """Dissolved "unknown" area. Never to be rendered as safe."""
    parts = [r.geometry for r in records if r.kind == KIND_NOT_ANALYSED]
    if not parts:
        return None
    return gpd.GeoSeries(parts, crs=WGS84).union_all()


# ---------------------------------------------------------------------------
# Validity
# ---------------------------------------------------------------------------

def check_max_age(max_age: timedelta) -> timedelta:
    """Refuse a validity window that is empty or longer than MAX_AGE_LIMIT."""
    if not timedelta(0) < max_age <= MAX_AGE_LIMIT:
        raise ValueError(f"max age must be above 0 and at most {MAX_AGE_LIMIT} "
                         f"(contract v2: satellite data count for 24 h), got {max_age}")
    return max_age


def max_age_from_env() -> timedelta:
    """$SATELLITE_MAX_AGE_HOURS as a timedelta; 24 h when unset."""
    value = os.getenv(MAX_AGE_ENV)
    if not value:
        return MAX_AGE_LIMIT
    try:
        hours = float(value)
    except ValueError:
        raise ValueError(f"{MAX_AGE_ENV} must be a number of hours, got {value!r}") from None
    return check_max_age(timedelta(hours=hours))


def assign_validity(records: list[FloodRecord], max_age: timedelta) -> None:
    """Set ``valid_until`` on every record: observed_at + max_age, or None
    (never current) when observed_at is unknown.

    An analysis extent says "observed dry" wherever its package has no flood
    and no Not Analysed polygon, so it must not outlive any of them: it ends
    at the earliest of their valid_until, and gets None if one of them has no
    observation time.
    """
    check_max_age(max_age)
    for r in records:
        r.valid_until = r.observed_at + max_age if r.observed_at is not None else None
    layers = [r.valid_until for r in records if r.kind != KIND_ANALYSIS_EXTENT]
    for r in records:
        if r.kind != KIND_ANALYSIS_EXTENT or r.valid_until is None or not layers:
            continue
        end = None if any(v is None for v in layers) else min(r.valid_until, *layers)
        r.valid_until = end if end is not None and end > r.observed_at else None


def _require_aware(moment: datetime, name: str) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware (UTC), got {moment!r}")
    return moment


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
        "product": r.product,
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
    extent = metric[metric["kind"] == KIND_ANALYSIS_EXTENT]
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
        "analysis_extent_records": len(extent),
        "analysis_extent_km2": extent.union_all().area / 1e6 if not extent.empty else 0.0,
        "analysis_extent_observed_at": next((r.observed_at for r in records if r.kind == KIND_ANALYSIS_EXTENT), None),
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


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def count_by_kind(records: list[FloodRecord]) -> dict[str, int]:
    kinds = (KIND_OBSERVED_FLOOD, KIND_ANALYSIS_EXTENT, KIND_NOT_ANALYSED)
    return {kind: sum(1 for r in records if r.kind == kind) for kind in kinds}


@dataclass
class WriteResult:
    """What one ``write_to_db`` call stored."""

    disaster_id: int | None  # None when nothing was written
    inserted: dict[str, int]  # rows appended, per kind
    already_stored: dict[str, int]  # rows skipped: same source, product and observed_at stored before

    @property
    def nothing_new(self) -> bool:
        return not any(self.inserted.values())


def _as_multipolygon(geom: Polygon | MultiPolygon) -> MultiPolygon:
    return MultiPolygon([geom]) if isinstance(geom, Polygon) else geom


def _cut(text: str | None, length: int) -> str | None:
    return text[:length] if text else None


def _stored_observations(session, products: set[str]) -> set[tuple]:
    """(source, product, observed_at) of the stored rows of these products."""
    from sqlalchemy import select, union_all

    from models import AnalysisCoverage, DisasterArea

    query = union_all(*(
        select(model.source, model.product, model.observed_at).where(model.product.in_(sorted(products))).distinct()
        for model in (DisasterArea, AnalysisCoverage)))
    return {tuple(row) for row in session.execute(query)}


def write_to_db(session, records: list[FloodRecord], max_age: timedelta | None = None,
                now: datetime | None = None) -> WriteResult:
    """Append one EMSR package: flood polygons to ``disaster_areas``, the
    analysis extent and the Not Analysed areas to ``analysis_coverage``.

    Append-only: stored rows are never deleted or updated. A record whose
    (source, product, observed_at) is already stored is skipped, so importing
    the same product again writes nothing (``nothing_new``). Each row gets
    ``valid_until`` from ``assign_validity`` (``max_age`` defaults to
    $SATELLITE_MAX_AGE_HOURS, else 24 h) and ``ingested_at = now``.

    Raises ``ImportRefused`` before touching the session if the package has
    no flood polygon: its analysed area would be recorded as "observed: no
    water". Get-or-creates the disaster type and the ``Disaster`` (its
    creation_time is the earliest observation, else ``now``). One
    transaction under an advisory lock: committed at the end, rolled back on
    any error.
    """
    from geoalchemy2.shape import from_shape
    from sqlalchemy import select, text

    from models import SRID, AnalysisCoverage, Disaster, DisasterArea, DisasterType

    if not any(r.kind == KIND_OBSERVED_FLOOD for r in records):
        raise ImportRefused("no observed flood polygon parsed: refusing to record the analysed area as observed dry")
    if not all(r.product for r in records):
        raise ValueError("every record needs a product id: source, product and observed_at identify an observation")
    assign_validity(records, max_age_from_env() if max_age is None else max_age)
    now = _require_aware(now or datetime.now(timezone.utc), "now")
    values = [dict(name=r.name[:255], kind=r.kind, source=_cut(r.source, 255), product=_cut(r.product, 128),
                   observed_at=r.observed_at, valid_until=r.valid_until, ingested_at=now) for r in records]
    # The DateTime column is timezone-naive: store UTC without tzinfo. With no
    # observation time the disaster gets the import time; the rows keep
    # observed_at NULL, so nothing presents the unknown data as fresh.
    creation_time = (earliest_observation(records) or now).astimezone(timezone.utc).replace(tzinfo=None)

    try:
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext('resqnet.ingestion_flood'))"))
        stored = _stored_observations(session, {v["product"] for v in values})
        is_new = [(v["source"], v["product"], v["observed_at"]) not in stored for v in values]
        result = WriteResult(
            disaster_id=None,
            inserted=count_by_kind([r for r, new in zip(records, is_new) if new]),
            already_stored=count_by_kind([r for r, new in zip(records, is_new) if not new]),
        )
        if result.nothing_new:
            session.rollback()  # nothing to write; ends the transaction and the lock
            return result

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
        elif disaster.creation_time is None:
            disaster.creation_time = creation_time

        result.disaster_id = disaster.id
        for r, row, new in zip(records, values, is_new):
            if not new:
                continue
            if r.kind == KIND_OBSERVED_FLOOD:
                session.add(DisasterArea(disaster_id=disaster.id, area=from_shape(r.geometry, srid=SRID), **row))
            else:
                session.add(AnalysisCoverage(disaster_id=disaster.id,
                                             area=from_shape(_as_multipolygon(r.geometry), srid=SRID), **row))
        session.commit()
    except Exception:
        session.rollback()
        raise
    return result


@dataclass
class ValidObservations:
    """Stored rows that count at one moment; see ``observations_valid_at``."""

    at: datetime
    flood: list  # models.DisasterArea
    analysis_extent: list  # models.AnalysisCoverage, kind analysis_extent
    not_analysed: list  # models.AnalysisCoverage, kind not_analysed


def valid_at(model, at: datetime):
    """SQL condition on ``model`` (DisasterArea or AnalysisCoverage): the row
    counts at ``at``, i.e. observed_at <= at < valid_until. A NULL in either
    column never matches, so a row without an observation time never counts."""
    from sqlalchemy import and_

    return and_(model.observed_at <= at, model.valid_until > at)


def observations_valid_at(session, at: datetime, disaster_id: int | None = None) -> ValidObservations:
    """The stored observations that count at moment ``at`` (timezone-aware):
    observed_at <= at < valid_until, optionally for one disaster.

    A past ``at`` replays the flood (EMSR444 AOI02: 2020-06-26T12:00Z); now
    gives the live state. Whatever is not returned is unknown at ``at``: not
    observed yet, expired, or without an observation time (such as areas from
    POST /disasterArea, which sets no times), and never dry. Ground may be
    read as "observed dry" only inside a returned analysis_extent and outside
    every returned flood and not_analysed area, and only as of that extent's
    observed_at, never as "open now".
    """
    from sqlalchemy import select

    from models import AnalysisCoverage, DisasterArea

    _require_aware(at, "at")

    def rows(model, *conditions):
        query = select(model).where(valid_at(model, at), *conditions)
        if disaster_id is not None:
            query = query.where(model.disaster_id == disaster_id)
        return list(session.scalars(query.order_by(model.id)))

    return ValidObservations(
        at=at,
        flood=rows(DisasterArea),
        analysis_extent=rows(AnalysisCoverage, AnalysisCoverage.kind == KIND_ANALYSIS_EXTENT),
        not_analysed=rows(AnalysisCoverage, AnalysisCoverage.kind == KIND_NOT_ANALYSED),
    )


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
    parser = argparse.ArgumentParser(
        description="Append EMSR444 AOI02 flood polygons and analysis coverage to PostGIS.")
    parser.add_argument("--zip", type=Path, default=DEFAULT_ZIP,
                        help=f"EMSR vector package zip (env EMSR_ZIP, default {DEFAULT_ZIP})")
    parser.add_argument("--dry-run", action="store_true", help="only read and print a summary")
    parser.add_argument("--export-geojson", type=Path, nargs="?", const=DEFAULT_OUT_DIR, default=None,
                        metavar="DIR",
                        help=f"export records + Not Analysed mask (env EMSR_OUT_DIR, default {DEFAULT_OUT_DIR})")
    parser.add_argument("--max-age-hours", type=float, default=None,
                        help=f"hours the data count after the image time, at most 24 (env {MAX_AGE_ENV}, default 24)")
    args = parser.parse_args(argv)

    try:
        max_age = (max_age_from_env() if args.max_age_hours is None
                   else check_max_age(timedelta(hours=args.max_age_hours)))
    except ValueError as exc:
        parser.error(str(exc))
    if not args.zip.is_file():
        print(f"EMSR package not found: {args.zip}. Set EMSR_ZIP or pass --zip.", file=sys.stderr)
        return 1

    try:
        records = read_package(args.zip)
    except ImportRefused as exc:
        print(f"Import refused, database unchanged: {exc}", file=sys.stderr)
        return 2
    assign_validity(records, max_age)
    print(f"Read {args.zip.name}")
    _print_summary(summarize(records))
    ends = sorted(r.valid_until for r in records if r.valid_until)
    print(f"  valid_until: {ends[0].isoformat() if ends else None} .. {ends[-1].isoformat() if ends else None} "
          f"({max_age.total_seconds() / 3600:g} h after the image; "
          f"never current: {len(records) - len(ends)} records)")

    if args.export_geojson is not None:
        for path in export_geojson(records, args.export_geojson):
            print(f"  exported: {path}")

    if args.dry_run:
        print("Dry run: database not touched.")
        return 0

    from database import SessionLocal, init_db  # imported late: needs a reachable PostGIS

    init_db()
    try:
        with SessionLocal() as session:
            result = write_to_db(session, records, max_age=max_age)
    except ImportRefused as exc:
        print(f"Import refused, database unchanged: {exc}", file=sys.stderr)
        return 2
    skipped = sum(result.already_stored.values())
    if result.nothing_new:
        print(f"Already stored, nothing written: all {skipped} records of {product_id(args.zip)} "
              "(same source, product and observed_at).")
        return 0
    inserted = result.inserted
    print(f"Appended to disaster {result.disaster_id}: {inserted[KIND_OBSERVED_FLOOD]} observed flood polygons "
          f"(disaster_areas), {inserted[KIND_ANALYSIS_EXTENT]} analysis extent(s) and "
          f"{inserted[KIND_NOT_ANALYSED]} Not Analysed areas (analysis_coverage)."
          + (f" Skipped {skipped} records already stored." if skipped else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
