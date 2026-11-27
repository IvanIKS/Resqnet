"""Live map for the dispatcher UI.

One GeoJSON FeatureCollection with everything the map needs: disaster areas,
roads (flagged when they cross water), evacuation points and settlements,
plus a summary block for the dashboard. ``at`` replays the situation at a
given moment, which is how the demo scrubs through a flood in time.
"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, exists, func, select, true
from sqlalchemy.orm import Session

from database import get_session
from models import SRID, Disaster, DisasterArea, EvacuationPoint, Road, Settlement

router = APIRouter(prefix="/api/map", tags=["map"])


def _parse_bbox(bbox: str | None):
    if bbox is None:
        return None
    try:
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox.split(","))
    except ValueError:
        raise HTTPException(status_code=400, detail="bbox must be minLon,minLat,maxLon,maxLat")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise HTTPException(status_code=400, detail="bbox min must be less than max")
    return func.ST_MakeEnvelope(min_lon, min_lat, max_lon, max_lat, SRID)


def _feature(geojson: str, properties: dict) -> dict:
    return {"type": "Feature", "geometry": json.loads(geojson), "properties": properties}


@router.get("/live")
def live_map(
    at: datetime | None = Query(None, description="Show the situation at this moment (ISO 8601). Defaults to now."),
    disaster_id: int | None = Query(None, description="Only this disaster."),
    bbox: str | None = Query(None, description="minLon,minLat,maxLon,maxLat - only what is visible on the map."),
    session: Session = Depends(get_session),
):
    moment = at or datetime.now(timezone.utc)
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    envelope = _parse_bbox(bbox)

    active = and_(
        DisasterArea.disaster_id == Disaster.id,
        Disaster.creation_time <= moment,
    )
    if disaster_id is not None:
        active = and_(active, Disaster.id == disaster_id)

    def in_view(geom):
        return geom.op("&&")(envelope) if envelope is not None else true()

    def touches_water(geom):
        return exists().where(active, func.ST_Intersects(DisasterArea.area, geom))

    features = []

    areas = session.execute(
        select(
            DisasterArea.id, DisasterArea.name, Disaster.id, Disaster.name,
            Disaster.creation_time, func.ST_AsGeoJSON(DisasterArea.area),
        )
        .join(Disaster, DisasterArea.disaster_id == Disaster.id)
        .where(active, in_view(DisasterArea.area))
        .order_by(DisasterArea.id)
    ).all()
    for area_id, area_name, dis_id, dis_name, created, geom in areas:
        features.append(_feature(geom, {
            "kind": "disaster_area", "id": area_id, "name": area_name,
            "disaster_id": dis_id, "disaster_name": dis_name,
            "since": created.isoformat() if created else None,
        }))

    flooded_roads = 0
    roads = session.execute(
        select(Road.id, func.ST_AsGeoJSON(Road.geom), touches_water(Road.geom))
        .where(in_view(Road.geom))
        .order_by(Road.id)
    ).all()
    for road_id, geom, flooded in roads:
        flooded_roads += bool(flooded)
        features.append(_feature(geom, {"kind": "road", "id": road_id, "flooded": bool(flooded)}))

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

    return {
        "type": "FeatureCollection",
        "features": features,
        "summary": {
            "at": moment.isoformat() + "Z",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "disaster_areas": len(areas),
            "roads_flooded": flooded_roads,
            "roads_total": len(roads),
            "evacuation_points_in_danger": sum(1 for p in points if p[3]),
            "settlements_affected": sum(1 for s in settlements if s[4]),
            "people_affected": affected_people,
        },
    }
