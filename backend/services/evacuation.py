"""Evacuation route calculation service using PostGIS and Road graph search."""

from dataclasses import dataclass
import heapq
from typing import Dict, List, Optional, Set, Tuple

from geoalchemy2 import Geography
from sqlalchemy import cast, func, or_, select
from sqlalchemy.orm import Session, selectinload

from models import SRID, DisasterArea, EvacuationPoint, Road


@dataclass
class EvacuationResult:
    status: str
    in_disaster_area: bool
    road_ids: List[int]
    distance_meters: Optional[float] = None
    target_point_id: Optional[int] = None
    target_point_name: Optional[str] = None
    candidate_lat: Optional[float] = None
    candidate_lon: Optional[float] = None


def find_shortest_road_path(
    start_road_id: int,
    target_road_ids: Set[int],
    adjacency: Dict[int, List[int]],
    lengths: Dict[int, float],
    flooded_road_ids: Set[int],
) -> Optional[Tuple[List[int], float]]:
    """Run Dijkstra over Road segments from start_road_id to any road in target_road_ids.

    Starting on start_road_id is allowed even if it is in flooded_road_ids so a user
    inside a disaster area can exit their immediate block; subsequent flooded roads
    are forbidden.
    """
    if not target_road_ids:
        return None

    if start_road_id in target_road_ids:
        return [start_road_id], lengths.get(start_road_id, 0.0)

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
            if neighbor in flooded_road_ids:
                continue
            new_cost = cost + lengths.get(neighbor, 0.0)
            if new_cost < best_cost.get(neighbor, float("inf")):
                best_cost[neighbor] = new_cost
                parent[neighbor] = current
                heapq.heappush(heap, (new_cost, neighbor))

    return None


def _is_in_disaster_area(session: Session, point_wkt: str) -> bool:
    point_geom = func.ST_GeomFromText(point_wkt, SRID)
    stmt = select(DisasterArea.id).where(func.ST_Contains(DisasterArea.area, point_geom)).limit(1)
    return session.scalar(stmt) is not None


def _get_flooded_road_ids(session: Session) -> Set[int]:
    stmt = select(Road.id).join(
        DisasterArea, func.ST_Intersects(Road.geom, DisasterArea.area)
    ).distinct()
    return set(session.scalars(stmt).all())


def _find_nearest_road_id(session: Session, point_wkt: str) -> Optional[int]:
    point_geom = func.ST_GeomFromText(point_wkt, SRID)
    stmt = select(Road.id).order_by(func.ST_Distance(Road.geom, point_geom)).limit(1)
    return session.scalar(stmt)


def _find_nearest_evacuation_point(
    session: Session, point_wkt: str
) -> Optional[EvacuationPoint]:
    point_geom = func.ST_GeomFromText(point_wkt, SRID)
    stmt = select(EvacuationPoint).order_by(func.ST_Distance(EvacuationPoint.geom, point_geom)).limit(1)
    return session.scalar(stmt)


def _find_candidate_point(session: Session, point_wkt: str) -> Optional[Tuple[float, float]]:
    point_geom = func.ST_GeomFromText(point_wkt, SRID)
    flooded_subquery = select(DisasterArea.area)

    best_dist = float("inf")
    best_coords: Optional[Tuple[float, float]] = None

    for end_col in (Road.end1, Road.end2):
        not_in_disaster = ~select(DisasterArea.id).where(
            func.ST_Contains(DisasterArea.area, end_col)
        ).exists()
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


def get_evacuation_route(session: Session, lat: float, lon: float) -> EvacuationResult:
    point_wkt = f"POINT({lon} {lat})"

    if not _is_in_disaster_area(session, point_wkt):
        return EvacuationResult(status="safe", in_disaster_area=False, road_ids=[])

    start_road_id = _find_nearest_road_id(session, point_wkt)
    flooded_road_ids = _get_flooded_road_ids(session)

    evac_points = session.scalars(select(EvacuationPoint)).all()
    target_road_to_point: Dict[int, EvacuationPoint] = {}
    for ep in evac_points:
        ep_road_id = session.scalar(
            select(Road.id).order_by(func.ST_Distance(Road.geom, ep.geom)).limit(1)
        )
        if ep_road_id is not None and ep_road_id not in flooded_road_ids:
            target_road_to_point[ep_road_id] = ep

    if start_road_id is not None and target_road_to_point:
        adjacency, lengths = _load_road_graph(session)
        result = find_shortest_road_path(
            start_road_id=start_road_id,
            target_road_ids=set(target_road_to_point.keys()),
            adjacency=adjacency,
            lengths=lengths,
            flooded_road_ids=flooded_road_ids,
        )
        if result is not None:
            path, distance_m = result
            ep = target_road_to_point[path[-1]]
            return EvacuationResult(
                status="route_found",
                in_disaster_area=True,
                road_ids=path,
                distance_meters=round(distance_m, 2),
                target_point_id=ep.id,
                target_point_name=ep.name,
            )

    nearest_ep = _find_nearest_evacuation_point(session, point_wkt)
    if nearest_ep is not None:
        return EvacuationResult(
            status="evacuation_point_fallback",
            in_disaster_area=True,
            road_ids=[],
            target_point_id=nearest_ep.id,
            target_point_name=nearest_ep.name,
        )

    candidate = _find_candidate_point(session, point_wkt)
    return EvacuationResult(
        status="candidate_fallback",
        in_disaster_area=True,
        road_ids=[],
        candidate_lat=candidate[0] if candidate else None,
        candidate_lon=candidate[1] if candidate else None,
    )
