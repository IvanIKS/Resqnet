"""Import the drivable OSM road network for the EMSR444 AOI02 area.

Overpass responses are cached in $OSM_CACHE_DIR (default ~/.cache/resqnet/osm).
"""

import argparse
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

import networkx as nx
import osmnx as ox
from geoalchemy2.shape import from_shape
from shapely.geometry import LineString, Point
from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

from models import SRID, Road, road_end1_connections, road_end2_connections

AOI02_BBOX = (25.17, 48.17, 26.31, 48.51)
DEFAULT_BUFFER = 0.05
# A replace may shrink the stored network to this fraction of its roads; below
# that it needs force: a failed or cut-off download must not wipe the map.
MIN_KEPT_FRACTION = 0.5


class ImportRefused(RuntimeError):
    """The write would damage the stored road network; nothing was changed."""


def cache_folder() -> Path:
    return Path(os.environ.get("OSM_CACHE_DIR") or Path.home() / ".cache" / "resqnet" / "osm")


def download_graph(buffer: float = DEFAULT_BUFFER) -> nx.MultiDiGraph:
    """Download the buffered AOI02 graph in WGS84, including disconnected pieces."""
    west, south, east, north = AOI02_BBOX
    bbox = (west - buffer, south - buffer, east + buffer, north + buffer)
    ox.settings.cache_folder = cache_folder()
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
    # True: traffic may only go end1 -> end2. False: both ways. None: unknown.
    oneway: bool | None


@dataclass
class RoadNetwork:
    roads: list[RoadSegment]
    # Pairs of zero-based indexes into roads, one list per association table.
    end1_connections: list[tuple[int, int]]
    end2_connections: list[tuple[int, int]]
    components: int


def transform_graph(graph: nx.MultiDiGraph) -> RoadNetwork:
    """Build one road per physical segment and its per-end junction connections.

    osmnx stores a two-way street as two directed edges (u->v and v->u) and a
    one-way street as one edge in its travel direction. The two copies of a
    two-way street become one road with ``oneway=False`` and osm_u <= osm_v.
    A one-way road keeps its travel direction: osm_u, end1 and the first point
    of geom are where traffic enters, so osm_u > osm_v is possible. ``oneway``
    is None when OSM gives no single value or the copies disagree; such a road
    gets the two-way orientation but must not be treated as two-way.
    """
    points = {node: Point(data["x"], data["y"]) for node, data in graph.nodes(data=True)}
    # (u, v, way_ids, coords) in canonical orientation -> (oneway values, flipped flags)
    segments: dict[tuple, tuple[set, set]] = {}

    for u, v, data in graph.edges(data=True):
        start, end = points[u].coords[0], points[v].coords[0]
        geometry = data.get("geometry")
        coords = tuple(geometry.coords) if geometry is not None else (start, end)
        if coords[0] != start or coords[-1] != end:
            coords = coords[::-1]

        # A curved loop can share its endpoint. A zero-length loop has no usable line.
        if u == v and len(set(coords)) < 2:
            continue

        # Canonical orientation, the same for both copies of a two-way street:
        # smaller node id first, or for a loop the smaller coordinate sequence.
        flipped = u > v if u != v else coords[::-1] < coords
        if flipped:
            u, v, coords = v, u, coords[::-1]

        way_ids = data.get("osmid")
        if way_ids is not None:
            ids = way_ids if isinstance(way_ids, list) else [way_ids]
            way_ids = ",".join(map(str, sorted(set(ids))))

        # Ignore directed edge keys: opposite directions can have different keys,
        # and different streets can have the same key in opposite directions.
        oneway_values, flips = segments.setdefault((u, v, way_ids, coords), (set(), set()))
        # osmnx sets a bool; an edge simplified from one-way and two-way parts gets a list.
        oneway = data.get("oneway")
        oneway_values.add(oneway if isinstance(oneway, bool) else None)
        flips.add(flipped)

    roads = []
    touching = defaultdict(set)
    for (u, v, way_ids, coords), (oneway_values, flips) in segments.items():
        oneway = None
        if oneway_values == {False}:
            oneway = False
        elif oneway_values == {True} and len(flips) == 1:
            oneway = True
            if flips == {True}:
                u, v, coords = v, u, coords[::-1]  # back to the travel direction
        index = len(roads)
        roads.append(RoadSegment(u, v, way_ids, LineString(coords), points[u], points[v], oneway))
        touching[u].add(index)
        touching[v].add(index)

    end1 = [(i, j) for i, road in enumerate(roads) for j in sorted(touching[road.osm_u] - {i})]
    end2 = [(i, j) for i, road in enumerate(roads) for j in sorted(touching[road.osm_v] - {i})]
    junctions = nx.Graph([(road.osm_u, road.osm_v) for road in roads])
    return RoadNetwork(roads, end1, end2, nx.number_connected_components(junctions))


def write_roads(
    session: Session, network: RoadNetwork, *, replace: bool = True, force: bool = False
) -> None:
    """Store roads and junctions, all or nothing, in the caller's transaction.

    replace=True swaps all stored roads and both junction tables for this
    network; unless ``force``, it is refused if the network keeps less than
    MIN_KEPT_FRACTION of the stored roads. replace=False adds the network and
    is refused if it shares an OSM node with the stored roads: junctions are
    built within one network only, so an overlapping append would duplicate
    roads and leave the copies unconnected. An empty network is always
    refused. Every check runs before the first change (``ImportRefused``),
    and the writes run in a savepoint, so an error leaves the caller's
    transaction as it was.
    """
    if not network.roads:
        raise ImportRefused("no drivable road in the OSM download: refusing to write an empty road network")

    rows = [
        Road(
            osm_u=road.osm_u,
            osm_v=road.osm_v,
            osm_way_ids=road.osm_way_ids,
            oneway=road.oneway,
            geom=from_shape(road.geom, srid=SRID),
            end1=from_shape(road.end1, srid=SRID),
            end2=from_shape(road.end2, srid=SRID),
        )
        for road in network.roads
    ]
    tables = (road_end1_connections, road_end2_connections)

    if replace:
        stored = session.scalar(select(func.count()).select_from(Road))
        if not force and len(rows) < stored * MIN_KEPT_FRACTION:
            raise ImportRefused(
                f"would replace {stored} stored roads with {len(rows)}; refusing "
                "(use force=True / --force if the network really shrank)"
            )
    else:
        _refuse_overlap(session, network)

    with session.begin_nested():
        if replace:
            for table in tables:
                session.execute(delete(table))
            session.execute(delete(Road))

        session.add_all(rows)
        session.flush()

        for table, connections in zip(tables, (network.end1_connections, network.end2_connections)):
            if connections:
                session.execute(
                    insert(table),
                    [{"road_id": rows[i].id, "connected_road_id": rows[j].id} for i, j in connections],
                )


def _refuse_overlap(session: Session, network: RoadNetwork) -> None:
    """Raise if an appended network repeats stored roads or meets them at a junction."""
    stored = session.execute(select(Road.osm_u, Road.osm_v, Road.osm_way_ids)).all()
    stored_nodes = {node for u, v, _ in stored for node in (u, v)}
    shared = stored_nodes & {node for road in network.roads for node in (road.osm_u, road.osm_v)}
    if not shared:
        return
    stored_segments = {(min(u, v), max(u, v), ids) for u, v, ids in stored if None not in (u, v)}
    repeated = sum(
        (min(road.osm_u, road.osm_v), max(road.osm_u, road.osm_v), road.osm_way_ids) in stored_segments
        for road in network.roads
    )
    raise ImportRefused(
        f"refusing to append: {repeated} of {len(network.roads)} roads are already stored and "
        f"{len(shared)} OSM nodes are shared with stored roads, whose junctions would not be connected "
        "(run without --append to replace the stored network)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--buffer", type=float, default=DEFAULT_BUFFER, help="AOI buffer in degrees")
    parser.add_argument("--dry-run", action="store_true", help="Download and transform without a database")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--replace", dest="append", action="store_false", default=False,
                      help="Replace all stored roads and junctions (default)")
    mode.add_argument("--append", action="store_true",
                      help="Add to the stored roads; refused if the two networks share an OSM node")
    parser.add_argument("--force", action="store_true",
                        help="Accept a replace that keeps less than half of the stored roads")
    args = parser.parse_args(argv)

    started = perf_counter()
    network = transform_graph(download_graph(args.buffer))
    oneway = Counter(road.oneway for road in network.roads)
    print(
        f"Roads: {len(network.roads)} (one-way: {oneway[True]}, two-way: {oneway[False]}, "
        f"direction unknown: {oneway[None]}), end1 connections: {len(network.end1_connections)}, "
        f"end2 connections: {len(network.end2_connections)}, connected components: {network.components}"
    )
    if not args.dry_run:
        from database import SessionLocal, init_db

        init_db()
        try:
            with SessionLocal.begin() as session:
                write_roads(session, network, replace=not args.append, force=args.force)
        except ImportRefused as error:
            print(f"Import refused, database unchanged: {error}", file=sys.stderr)
            return 2
        print("Appended to the stored roads." if args.append else "Replaced the stored roads.")
    print(f"Elapsed: {perf_counter() - started:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
