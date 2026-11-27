"""Import the drivable OSM road network for the EMSR444 AOI02 area."""

import argparse
from collections import defaultdict
from dataclasses import dataclass
from time import perf_counter

import networkx as nx
import osmnx as ox
from geoalchemy2.shape import from_shape
from shapely.geometry import LineString, Point
from sqlalchemy import delete, insert
from sqlalchemy.orm import Session

from models import SRID, Road, road_end1_connections, road_end2_connections

AOI02_BBOX = (25.17, 48.17, 26.31, 48.51)
DEFAULT_BUFFER = 0.05
CACHE_FOLDER = "D:/cassini-review/osm-cache"


def download_graph(buffer: float = DEFAULT_BUFFER) -> nx.MultiDiGraph:
    """Download the buffered AOI02 graph in WGS84, including disconnected pieces."""
    west, south, east, north = AOI02_BBOX
    bbox = (west - buffer, south - buffer, east + buffer, north + buffer)
    ox.settings.cache_folder = CACHE_FOLDER
    ox.settings.use_cache = True
    return ox.graph_from_bbox(bbox, network_type="drive", simplify=True, retain_all=True)


@dataclass(frozen=True)
class RoadSegment:
    osm_u: int
    osm_v: int
    osm_way_ids: str | None
    geom: LineString
    end1: Point
    end2: Point


@dataclass
class RoadNetwork:
    roads: list[RoadSegment]
    # Pairs of zero-based indexes into roads, one list per association table.
    end1_connections: list[tuple[int, int]]
    end2_connections: list[tuple[int, int]]
    components: int


def transform_graph(graph: nx.MultiDiGraph) -> RoadNetwork:
    """Build undirected road segments and their per-end junction connections."""
    points = {node: Point(data["x"], data["y"]) for node, data in graph.nodes(data=True)}
    undirected = nx.MultiGraph()

    for u, v, data in graph.edges(data=True):
        u, v = min(u, v), max(u, v)
        end1, end2 = points[u], points[v]
        geometry = data.get("geometry")
        coords = tuple(geometry.coords) if geometry is not None else (end1.coords[0], end2.coords[0])
        if coords[0] != end1.coords[0] or coords[-1] != end2.coords[0]:
            coords = coords[::-1]

        # A curved loop can share its endpoint. A zero-length loop has no usable line.
        if u == v:
            if len(set(coords)) < 2:
                continue
            coords = min(coords, coords[::-1])

        way_ids = data.get("osmid")
        if way_ids is not None:
            ids = way_ids if isinstance(way_ids, list) else [way_ids]
            way_ids = ",".join(map(str, sorted(set(ids))))

        # Ignore directed edge keys: opposite directions can have different keys,
        # and different streets can have the same key in opposite directions.
        key = (way_ids, coords)
        undirected.add_edge(u, v, key=key)

    roads = []
    touching = defaultdict(set)
    for u, v, (way_ids, coords) in undirected.edges(keys=True):
        # NetworkX reports an undirected edge relative to its node insertion order.
        u, v = min(u, v), max(u, v)
        index = len(roads)
        roads.append(RoadSegment(u, v, way_ids, LineString(coords), points[u], points[v]))
        touching[u].add(index)
        touching[v].add(index)

    end1 = [(i, j) for i, road in enumerate(roads) for j in sorted(touching[road.osm_u] - {i})]
    end2 = [(i, j) for i, road in enumerate(roads) for j in sorted(touching[road.osm_v] - {i})]
    return RoadNetwork(roads, end1, end2, nx.number_connected_components(undirected))


def write_roads(session: Session, network: RoadNetwork, *, replace: bool = False) -> None:
    """Insert roads and junctions in the caller's transaction."""
    tables = (road_end1_connections, road_end2_connections)
    if replace:
        for table in tables:
            session.execute(delete(table))
        session.execute(delete(Road))

    rows = [
        Road(
            osm_u=road.osm_u,
            osm_v=road.osm_v,
            osm_way_ids=road.osm_way_ids,
            geom=from_shape(road.geom, srid=SRID),
            end1=from_shape(road.end1, srid=SRID),
            end2=from_shape(road.end2, srid=SRID),
        )
        for road in network.roads
    ]
    session.add_all(rows)
    session.flush()

    for table, connections in zip(tables, (network.end1_connections, network.end2_connections)):
        if connections:
            session.execute(
                insert(table),
                [{"road_id": rows[i].id, "connected_road_id": rows[j].id} for i, j in connections],
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--buffer", type=float, default=DEFAULT_BUFFER, help="AOI buffer in degrees")
    parser.add_argument("--dry-run", action="store_true", help="Download and transform without a database")
    parser.add_argument("--replace", action="store_true", help="Clear all existing roads and junctions first")
    args = parser.parse_args()

    started = perf_counter()
    network = transform_graph(download_graph(args.buffer))
    if not args.dry_run:
        from database import SessionLocal, init_db

        init_db()
        with SessionLocal.begin() as session:
            write_roads(session, network, replace=args.replace)

    print(
        f"Roads: {len(network.roads)}, end1 connections: {len(network.end1_connections)}, "
        f"end2 connections: {len(network.end2_connections)}, "
        f"connected components: {network.components}, elapsed: {perf_counter() - started:.2f}s"
    )


if __name__ == "__main__":
    main()
