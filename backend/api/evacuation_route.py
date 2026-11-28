"""Routes for evacuation route calculation."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from api.schemas import EvacuationPointOut, EvacuationRouteIn, EvacuationRouteOut
from database import get_session
from services.evacuation import get_evacuation_route

router = APIRouter(tags=["evacuation-route"])


@router.post("/evacuation-route", response_model=EvacuationRouteOut)
def compute_evacuation_route(
    payload: EvacuationRouteIn, session: Session = Depends(get_session)
):
    result = get_evacuation_route(session, lat=payload.lat, lon=payload.lon, at=payload.at)
    target_point = (
        EvacuationPointOut(id=result.target_point_id, name=result.target_point_name)
        if result.target_point_id is not None
        else None
    )
    return EvacuationRouteOut(
        status=result.status,
        at=result.at,
        route_status=result.route_status,
        reason=result.reason,
        limiting_segment_ids=result.limiting_segment_ids,
        start_in_flood=result.start_in_flood,
        route=result.route,
        detail=result.detail,
        in_disaster_area=result.in_disaster_area,
        road_ids=result.road_ids,
        distance_meters=result.distance_meters,
        target_point=target_point,
        candidate_lat=result.candidate_lat,
        candidate_lon=result.candidate_lon,
    )
