"""Live map for the dispatcher UI.

One GeoJSON FeatureCollection with everything the map needs: disaster areas,
roads with their status, evacuation points, settlements and field reports
with their Galileo OSNMA verdict, plus a summary block for the dashboard. ``at`` replays the situation at a
given moment, which is how the demo scrubs through a flood in time.
"""

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, exists, func, or_, select, true, union
from sqlalchemy.orm import Session

from database import get_session
from models import (
    SRID, AnalysisCoverage, Disaster, DisasterArea, EvacuationPoint, FieldReport, Road, Settlement,
)
from service.ingestion_flood import _require_aware, valid_at
from services.road_status import BBox, SegmentStatus, road_statuses

router = APIRouter(prefix="/api/map", tags=["map"])
logger = logging.getLogger(__name__)


def _parse_bbox(bbox: str | None) -> BBox | None:
    if bbox is None:
        return None
    try:
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox.split(","))
    except ValueError:
        raise HTTPException(status_code=400, detail="bbox must be minLon,minLat,maxLon,maxLat")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise HTTPException(status_code=400, detail="bbox min must be less than max")
    return min_lon, min_lat, max_lon, max_lat


def _feature(geojson: str, properties: dict) -> dict:
    return {"type": "Feature", "geometry": json.loads(geojson), "properties": properties}


@router.get("/live")
def live_map(
    at: datetime | None = Query(None, description="Replay time (ISO 8601 with Z or offset). Defaults to now."),
    disaster_id: int | None = Query(None, description="Only this disaster."),
    bbox: str | None = Query(None, description="minLon,minLat,maxLon,maxLat - only what is visible on the map."),
    session: Session = Depends(get_session),
):
    try:
        moment = _require_aware(at or datetime.now(timezone.utc), "at").astimezone(timezone.utc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="at must include Z or a UTC offset") from exc
    bounds = _parse_bbox(bbox)
    envelope = func.ST_MakeEnvelope(*bounds, SRID) if bounds is not None else None

    active = or_(valid_at(DisasterArea, moment), DisasterArea.observed_at.is_(None))
    if disaster_id is not None:
        active = and_(active, DisasterArea.disaster_id == disaster_id)

    def in_view(geom):
        return geom.op("&&")(envelope) if envelope is not None else true()

    def touches_water(geom):
        return exists().where(active, func.ST_Intersects(DisasterArea.area, geom))

    features = []

    areas = session.execute(
        select(
            DisasterArea.id, DisasterArea.name, Disaster.id, Disaster.name,
            DisasterArea.observed_at, func.ST_AsGeoJSON(DisasterArea.area),
        )
        .join(Disaster, DisasterArea.disaster_id == Disaster.id)
        .where(active, in_view(DisasterArea.area))
        .order_by(DisasterArea.id)
    ).all()
    for area_id, area_name, dis_id, dis_name, observed_at, geom in areas:
        features.append(_feature(geom, {
            "kind": "disaster_area", "id": area_id, "name": area_name,
            "disaster_id": dis_id, "disaster_name": dis_name,
            "since": observed_at.isoformat() if observed_at is not None else None,
            "untimed": observed_at is None,
        }))

    roads = session.execute(
        select(Road.id, func.ST_AsGeoJSON(Road.geom))
        .where(in_view(Road.geom))
        .order_by(Road.id)
    ).all()
    counts = {"closed": 0, "mapped_option": 0, "unknown": 0}
    try:
        # Known hazards still apply if the displayed disaster layer is filtered.
        statuses = road_statuses(session, moment, bbox=bounds)
    except Exception:
        logger.exception("Live map road status computation failed")
        session.rollback()
        statuses = {}
    unavailable = SegmentStatus("unknown", "error")
    for road_id, geom in roads:
        status = statuses.get(road_id, unavailable)
        counts[status.route_status] += 1
        features.append(_feature(geom, {
            "kind": "road", "id": road_id, "flooded": status.route_status == "closed",
            "route_status": status.route_status, "reason": status.reason,
        }))

    points = session.execute(
        select(EvacuationPoint.id, EvacuationPoint.name, func.ST_AsGeoJSON(EvacuationPoint.geom),
               touches_water(EvacuationPoint.geom))
        .where(in_view(EvacuationPoint.geom))
        .order_by(EvacuationPoint.id)
    ).all()
    for point_id, name, geom, in_danger in points:
        features.append(_feature(geom, {
            "kind": "evacuation_point", "id": point_id, "name": name, "in_danger": bool(in_danger),
        }))

    affected_people = 0
    settlements = session.execute(
        select(Settlement.id, Settlement.name, Settlement.population,
               func.ST_AsGeoJSON(Settlement.area), touches_water(Settlement.area))
        .where(in_view(Settlement.area))
        .order_by(Settlement.id)
    ).all()
    for settlement_id, name, population, geom, affected in settlements:
        if affected:
            affected_people += population or 0
        features.append(_feature(geom, {
            "kind": "settlement", "id": settlement_id, "name": name,
            "population": population, "affected": bool(affected),
        }))

    reports = session.execute(
        select(FieldReport.id, FieldReport.road_id, FieldReport.status, FieldReport.observed_at,
               FieldReport.osnma_sample, FieldReport.osnma_verdict, func.ST_AsGeoJSON(FieldReport.point))
        .where(FieldReport.observed_at <= moment, in_view(FieldReport.point))
        .order_by(FieldReport.id)
    ).all()
    for report_id, road_id, status, observed_at, sample, verdict, geom in reports:
        features.append(_feature(geom, {
            "kind": "field_report", "id": report_id, "road_id": road_id, "status": status,
            "observed_at": observed_at.isoformat(), "osnma_sample": sample, "osnma_verdict": verdict,
        }))

    observations = union(*(
        select(model.source, model.observed_at, model.valid_until).where(valid_at(model, moment))
        for model in (DisasterArea, AnalysisCoverage)
    )).order_by("observed_at", "valid_until", "source")
    return {
        "type": "FeatureCollection",
        "features": features,
        "summary": {
            "at": moment.isoformat().replace("+00:00", "Z"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "disaster_areas": len(areas),
            "roads_flooded": counts["closed"],
            "roads_total": len(roads),
            "roads_by_status": counts,
            "observations": [
                {"source": source, "observed_at": start.isoformat(), "valid_until": end.isoformat()}
                for source, start, end in session.execute(observations)
            ],
            "evacuation_points_in_danger": sum(1 for p in points if p[3]),
            "settlements_affected": sum(1 for s in settlements if s[4]),
            "people_affected": affected_people,
            "reports_total": len(reports),
            "reports_authenticated": sum(1 for r in reports if r[5] == "authenticated"),
            "reports_rejected": sum(1 for r in reports if r[5] == "rejected"),
        },
    }
