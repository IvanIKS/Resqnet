"""Evacuation route calculation service using PostGIS and Road graph search."""

import heapq
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from geoalchemy2 import Geography
from sqlalchemy import cast, exists, func, or_, select
from sqlalchemy.orm import Session, selectinload

from models import SRID, DisasterArea, EvacuationPoint, Road
from service.ingestion_flood import _require_aware, valid_at
from services.road_status import RouteStatus, SegmentStatus, StatusReason, road_statuses

logger = logging.getLogger(__name__)


@dataclass
class EvacuationResult:
    status: str
    at: datetime
    start_in_flood: bool
    route_status: RouteStatus = "unknown"
    reason: StatusReason | None = "error"
    road_ids: List[int] = field(default_factory=list)
    limiting_segment_ids: List[int] = field(default_factory=list)
    route: dict | None = None
    detail: str | None = None
    distance_meters: Optional[float] = None
    target_point_id: Optional[int] = None
    target_point_name: Optional[str] = None
    candidate_lat: Optional[float] = None
    candidate_lon: Optional[float] = None

    @property
    def in_disaster_area(self) -> bool:
        return self.start_in_flood


def find_shortest_road_path(
    start_road_id: int,
    target_road_ids: set[int],
    adjacency: Dict[int, List[int]],
    lengths: Dict[int, float],
    allowed_road_ids: set[int],
) -> Optional[Tuple[List[int], float]]:
    """Run Dijkstra over Road segments from start_road_id to any road in target_road_ids.

    The start may be outside the allowed set. Every subsequent road and the
    destination must be allowed. Adjacency ignores driving direction.
    """
    target_road_ids = target_road_ids & allowed_road_ids
    if not target_road_ids:
        return None

    start_cost = lengths.get(start_road_id, 0.0)
    heap: List[Tuple[float, int]] = [(start_cost, start_road_id)]
    best_cost: Dict[int, float] = {start_road_id: start_cost}
    parent: Dict[int, Optional[int]] = {start_road_id: None}

    while heap:
        cost, current = heapq.heappop(heap)
        if cost > best_cost.get(current, float("inf")):
            continue

        if current in target_road_ids:
            path: List[int] = []
            node: Optional[int] = current
            while node is not None:
                path.append(node)
                node = parent[node]
            path.reverse()
            return path, cost

        for neighbor in adjacency.get(current, []):
            if neighbor not in allowed_road_ids:
                continue
            new_cost = cost + lengths.get(neighbor, 0.0)
            if new_cost < best_cost.get(neighbor, float("inf")):
                best_cost[neighbor] = new_cost
                parent[neighbor] = current
                heapq.heappush(heap, (new_cost, neighbor))

    return None


def _in_hazard(geom, at: datetime):
    """Same water test as the live map's in_danger: current or untimed, boundary included."""
    return exists().where(
        or_(valid_at(DisasterArea, at), DisasterArea.observed_at.is_(None)),
        func.ST_Intersects(DisasterArea.area, geom),
    )


def _is_in_disaster_area(session: Session, point_wkt: str, at: datetime) -> bool:
    return bool(session.scalar(select(_in_hazard(func.ST_GeomFromText(point_wkt, SRID), at))))


def _find_nearest_road_id(session: Session, point_geom) -> Optional[int]:
    stmt = select(Road.id).order_by(func.ST_Distance(Road.geom, point_geom), Road.id).limit(1)
    return session.scalar(stmt)


def _find_nearest_evacuation_point(
    session: Session, point_wkt: str, at: datetime
) -> Optional[EvacuationPoint]:
    point_geom = func.ST_GeomFromText(point_wkt, SRID)
    stmt = (
        select(EvacuationPoint).where(~_in_hazard(EvacuationPoint.geom, at))
        .order_by(func.ST_Distance(EvacuationPoint.geom, point_geom), EvacuationPoint.id).limit(1)
    )
    return session.scalar(stmt)


def _find_candidate_point(session: Session, point_wkt: str, at: datetime) -> Optional[Tuple[float, float]]:
    point_geom = func.ST_GeomFromText(point_wkt, SRID)

    best_dist = float("inf")
    best_coords: Optional[Tuple[float, float]] = None

    for end_col in (Road.end1, Road.end2):
        not_in_disaster = ~_in_hazard(end_col, at)
        stmt = (
            select(
                func.ST_Y(end_col).label("lat"),
                func.ST_X(end_col).label("lon"),
                func.ST_Distance(end_col, point_geom).label("dist"),
            )
            .where(not_in_disaster)
            .order_by("dist")
            .limit(1)
        )
        row = session.execute(stmt).first()
        if row is not None and row.dist < best_dist:
            best_dist = row.dist
            best_coords = (float(row.lat), float(row.lon))

    return best_coords


def _load_road_graph(session: Session) -> Tuple[Dict[int, List[int]], Dict[int, float]]:
    roads = session.scalars(
        select(Road).options(selectinload(Road.end1_roads), selectinload(Road.end2_roads))
    ).all()

    length_rows = session.execute(
        select(Road.id, func.ST_Length(cast(Road.geom, Geography)).label("len_m"))
    ).all()
    lengths: Dict[int, float] = {row.id: float(row.len_m or 0.0) for row in length_rows}

    adjacency: Dict[int, List[int]] = {}
    for road in roads:
        neighbors = [r.id for r in road.end1_roads] + [r.id for r in road.end2_roads]
        adjacency[road.id] = neighbors

    return adjacency, lengths


def _find_route(
    start: int, targets: set[int], adjacency: dict[int, list[int]],
    lengths: dict[int, float], statuses: dict[int, SegmentStatus],
) -> tuple[list[int], float] | None:
    # Pass 1: mapped_option only; pass 2: + unknown; closed never (the start is always allowed).
    # An unknown start is already paid for, so pass 1 may also end on it.
    mapped = {road_id for road_id, status in statuses.items() if status.route_status == "mapped_option"}
    first = mapped | {start} if statuses[start].route_status == "unknown" else mapped
    result = find_shortest_road_path(start, targets, adjacency, lengths, first)
    if result is not None:
        return result

    unknown = {road_id for road_id, status in statuses.items() if status.route_status == "unknown"}
    return find_shortest_road_path(start, targets, adjacency, lengths, mapped | unknown)


def _route_evidence(path: list[int], statuses: dict[int, SegmentStatus]):
    # A closed start is reported via start_in_flood and left out of the rating;
    # any other start counts like every other segment. Closed roads are never targets.
    evaluated = path[1:] if statuses[path[0]].route_status == "closed" else path
    rank = {"closed": 0, "unknown": 1, "mapped_option": 2}
    weakest = min((statuses[road_id].route_status for road_id in evaluated), key=rank.__getitem__)
    limiting = [road_id for road_id in evaluated if statuses[road_id].route_status == weakest]
    reasons = [statuses[road_id].reason for road_id in limiting]
    # Report stale evidence first if the route also crosses gaps.
    return weakest, "expired" if "expired" in reasons else reasons[0], limiting


def _route_geometry(session: Session, path: list[int], statuses: dict[int, SegmentStatus]) -> dict:
    geometries = dict(session.execute(
        select(Road.id, func.ST_AsGeoJSON(Road.geom)).where(Road.id.in_(path))
    ).all())
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": json.loads(geometries[road_id]), "properties": {
            "id": road_id, "route_status": statuses[road_id].route_status,
            "reason": statuses[road_id].reason,
        }}
        for road_id in path
    ]}


def get_evacuation_route(
    session: Session, lat: float, lon: float, at: datetime | None = None,
) -> EvacuationResult:
    at = _require_aware(at or datetime.now(timezone.utc), "at").astimezone(timezone.utc)
    point_wkt = f"POINT({lon} {lat})"
    start_in_flood = False
    detail = "No route to an evacuation point."

    try:
        start_in_flood = _is_in_disaster_area(session, point_wkt, at)
        start = _find_nearest_road_id(session, func.ST_GeomFromText(point_wkt, SRID))
        if start is not None:
            statuses = road_statuses(session, at)
            start_in_flood |= statuses[start].route_status == "closed"
            targets: dict[int, EvacuationPoint] = {}
            points = select(EvacuationPoint).where(~_in_hazard(EvacuationPoint.geom, at))
            for point in session.scalars(points.order_by(EvacuationPoint.id)):
                road_id = _find_nearest_road_id(session, point.geom)
                if road_id is not None and statuses[road_id].route_status != "closed":
                    targets.setdefault(road_id, point)

            if targets:
                adjacency, lengths = _load_road_graph(session)
                result = _find_route(start, set(targets), adjacency, lengths, statuses)
                if result is not None:
                    path, distance_m = result
                    target = targets[path[-1]]
                    route_status, reason, limiting = _route_evidence(path, statuses)
                    return EvacuationResult(
                        status="route_found", at=at, start_in_flood=start_in_flood,
                        road_ids=path, route_status=route_status, reason=reason,
                        limiting_segment_ids=limiting, route=_route_geometry(session, path, statuses),
                        distance_meters=round(distance_m, 2),
                        target_point_id=target.id, target_point_name=target.name,
                    )
    except Exception:
        logger.exception("Evacuation route computation failed")
        session.rollback()
        detail = "Route computation failed."

    fallback = EvacuationResult(
        status="candidate_fallback", at=at, start_in_flood=start_in_flood, detail=detail,
    )
    try:
        nearest = _find_nearest_evacuation_point(session, point_wkt, at)
        if nearest is not None:
            fallback.status = "evacuation_point_fallback"
            fallback.target_point_id, fallback.target_point_name = nearest.id, nearest.name
        else:
            candidate = _find_candidate_point(session, point_wkt, at)
            if candidate is not None:
                fallback.candidate_lat, fallback.candidate_lon = candidate
    except Exception:
        logger.exception("Evacuation fallback lookup failed")
        fallback.detail = "Route and fallback point unavailable."

    return fallback
