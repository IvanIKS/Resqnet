"""Tests for service.ingestion_roads. Run from backend/: python -m pytest tests -q"""

import sys
import types
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path

import networkx as nx
import osmnx as ox
import pytest
from shapely.geometry import LineString, Point
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from models import Base, Road, road_end1_connections, road_end2_connections
from service import ingestion_roads
from service.ingestion_roads import ImportRefused, RoadNetwork, transform_graph, write_roads


def test_transform_preserves_segments_and_junctions():
    graph = nx.MultiDiGraph(crs="EPSG:4326")
    coords = {
        20: (26.00, 48.30),
        10: (25.95, 48.30),
        30: (26.05, 48.30),
        40: (26.10, 48.30),
        50: (26.10, 48.35),
    }
    for node, (x, y) in coords.items():
        graph.add_node(node, x=x, y=y)

    curve = LineString([coords[10], (25.97, 48.31), coords[20]])
    graph.add_edge(10, 20, key=0, osmid=[102, 101], geometry=curve)
    graph.add_edge(20, 10, key=1, osmid=[101, 102], geometry=curve.reverse())
    graph.add_edge(20, 10, key=0, osmid=103)
    graph.add_edge(30, 20, osmid=104)
    graph.add_edge(40, 50, osmid=105)

    network = transform_graph(graph)
    assert graph.number_of_edges() == 5
    assert len(network.roads) == 4
    assert {road.osm_way_ids for road in network.roads} == {"101,102", "103", "104", "105"}
    by_way = {road.osm_way_ids: road.geom for road in network.roads}
    assert by_way["101,102"].equals_exact(curve, 0)
    assert by_way["103"].equals_exact(LineString([coords[10], coords[20]]), 0)

    for road in network.roads:
        assert road.oneway is None  # no oneway attribute: unknown, never two-way
        assert road.osm_u < road.osm_v
        assert road.end1.equals(Point(coords[road.osm_u]))
        assert road.end2.equals(Point(coords[road.osm_v]))
        assert road.geom.coords[0] == coords[road.osm_u]
        assert road.geom.coords[-1] == coords[road.osm_v]

    all_connections = set(network.end1_connections) | set(network.end2_connections)
    assert all(i != j and (j, i) in all_connections for i, j in all_connections)
    for end, connections in (("osm_u", network.end1_connections), ("osm_v", network.end2_connections)):
        assert set(connections) == {
            (i, j)
            for i, road in enumerate(network.roads)
            for j, other in enumerate(network.roads)
            if i != j and getattr(road, end) in (other.osm_u, other.osm_v)
        }
    dead_end_road = next(i for i, road in enumerate(network.roads) if road.osm_v == 30)
    assert all(i != dead_end_road for i, _ in network.end2_connections)

    junction_graph = nx.Graph()
    junction_graph.add_nodes_from(range(len(network.roads)))
    junction_graph.add_edges_from(all_connections)
    junction_components = {
        frozenset(
            node
            for i in component
            for node in (network.roads[i].osm_u, network.roads[i].osm_v)
        )
        for component in nx.connected_components(junction_graph)
    }
    assert junction_components == {frozenset(c) for c in nx.connected_components(graph.to_undirected())}
    assert network.components == len(junction_components) == 2


def test_transform_keeps_curved_loops_and_skips_zero_length_loops():
    graph = nx.MultiDiGraph()
    graph.add_node(1, x=25.95, y=48.30)
    graph.add_node(2, x=26.00, y=48.30)
    loop = LineString([(25.95, 48.30), (25.97, 48.31), (25.96, 48.32), (25.95, 48.30)])
    graph.add_edge(1, 1, osmid=101, geometry=loop)
    graph.add_edge(1, 1, osmid=101, geometry=loop.reverse())
    graph.add_edge(2, 2, osmid=102)
    graph.add_edge(1, 2, osmid=103)

    network = transform_graph(graph)
    assert len(network.roads) == 2
    loop_index = next(i for i, road in enumerate(network.roads) if road.osm_u == road.osm_v)
    other_index = 1 - loop_index
    assert set(network.end1_connections) == {(loop_index, other_index), (other_index, loop_index)}
    assert network.end2_connections == [(loop_index, other_index)]
    assert network.components == 1


# --- one-way streets ----------------------------------------------------------

def _graph(coords):
    graph = nx.MultiDiGraph(crs="EPSG:4326")
    for node, (x, y) in coords.items():
        graph.add_node(node, x=x, y=y)
    return graph


def _two_way(graph, u, v, geometry=None, **attrs):
    """Add a street in both directions, as osmnx does for a way that is not one-way."""
    forward, backward = dict(attrs, oneway=False, reversed=False), dict(attrs, oneway=False, reversed=True)
    if geometry is not None:
        forward["geometry"], backward["geometry"] = geometry, geometry.reverse()
    graph.add_edge(u, v, **forward)
    graph.add_edge(v, u, **backward)


def _one_way(graph, u, v, **attrs):
    """Add a one-way street: a single edge in its travel direction."""
    graph.add_edge(u, v, oneway=True, reversed=False, **attrs)


def _index(network, way_ids):
    (index,) = [i for i, road in enumerate(network.roads) if road.osm_way_ids == way_ids]
    return index


def test_two_way_street_is_one_road_and_parallel_streets_stay_apart():
    graph = _graph({10: (26.00, 48.30), 20: (26.05, 48.30)})
    curve = LineString([(26.00, 48.30), (26.02, 48.31), (26.05, 48.30)])
    _two_way(graph, 20, 10, geometry=curve.reverse(), osmid=201)  # digitised from the higher id
    _two_way(graph, 10, 20, osmid=202)  # a parallel straight street between the same junctions

    network = transform_graph(graph)
    assert graph.number_of_edges() == 4
    assert sorted(road.osm_way_ids for road in network.roads) == ["201", "202"]
    for road in network.roads:
        assert road.oneway is False
        assert (road.osm_u, road.osm_v) == (10, 20)
    assert list(network.roads[_index(network, "201")].geom.coords) == list(curve.coords)


def test_one_way_road_keeps_its_travel_direction():
    coords = {10: (26.00, 48.30), 20: (26.05, 48.30), 30: (26.10, 48.30)}
    graph = _graph(coords)
    curve = LineString([coords[20], (26.03, 48.31), coords[10]])
    _one_way(graph, 20, 10, osmid=301, geometry=curve)  # travels from the higher id to the lower
    _one_way(graph, 20, 30, osmid=302)

    network = transform_graph(graph)
    assert len(network.roads) == 2
    assert all(road.oneway is True for road in network.roads)
    against_ids, along_ids = _index(network, "301"), _index(network, "302")
    road = network.roads[against_ids]
    assert (road.osm_u, road.osm_v) == (20, 10)
    assert list(road.geom.coords) == list(curve.coords)
    assert road.end1.equals(Point(coords[20])) and road.end2.equals(Point(coords[10]))
    assert (network.roads[along_ids].osm_u, network.roads[along_ids].osm_v) == (20, 30)

    # Both roads are entered at junction 20, so they meet at their end1.
    assert set(network.end1_connections) == {(against_ids, along_ids), (along_ids, against_ids)}
    assert network.end2_connections == []


def test_one_way_ring_and_loops_keep_their_travel_direction():
    coords = {40: (26.00, 48.30), 50: (26.02, 48.30), 60: (26.10, 48.30)}
    graph = _graph(coords)
    # A roundabout way split at its two junctions: one way id, two one-way edges.
    north = LineString([coords[40], (26.01, 48.31), coords[50]])
    south = LineString([coords[50], (26.01, 48.29), coords[40]])
    _one_way(graph, 40, 50, osmid=401, junction="roundabout", geometry=north)
    _one_way(graph, 50, 40, osmid=401, junction="roundabout", geometry=south)
    # A one-way loop whose reversed coordinates sort first, so the canonical order would flip it.
    loop = LineString([coords[60], (26.11, 48.30), (26.10, 48.31), coords[60]])
    assert tuple(loop.coords)[::-1] < tuple(loop.coords)
    _one_way(graph, 60, 60, osmid=402, geometry=loop)
    _two_way(graph, 60, 60, geometry=LineString([coords[60], (26.09, 48.30), (26.10, 48.29), coords[60]]),
             osmid=403)
    _one_way(graph, 50, 60, osmid=404)

    network = transform_graph(graph)
    assert len(network.roads) == 5
    ring = sorted((road for road in network.roads if road.osm_way_ids == "401"), key=lambda road: road.osm_u)
    assert [(road.osm_u, road.osm_v, road.oneway) for road in ring] == [(40, 50, True), (50, 40, True)]
    assert list(ring[0].geom.coords) == list(north.coords)
    assert list(ring[1].geom.coords) == list(south.coords)
    one_way_loop = network.roads[_index(network, "402")]
    assert one_way_loop.oneway is True and list(one_way_loop.geom.coords) == list(loop.coords)
    assert network.roads[_index(network, "403")].oneway is False
    assert network.components == 1


def test_missing_mixed_or_contradictory_oneway_is_unknown():
    graph = _graph({n: (26.00 + n / 1000, 48.30) for n in (10, 20, 30, 40, 50)})
    graph.add_edge(20, 10, osmid=501)  # no oneway attribute
    graph.add_edge(30, 20, osmid=502, oneway=[False, True])  # simplified from one-way and two-way parts
    _one_way(graph, 30, 40, osmid=503)  # one-way in both directions
    _one_way(graph, 40, 30, osmid=503)
    graph.add_edge(40, 50, osmid=504, oneway=False)  # the two copies disagree
    _one_way(graph, 50, 40, osmid=504)

    network = transform_graph(graph)
    assert sorted(road.osm_way_ids for road in network.roads) == ["501", "502", "503", "504"]
    for road in network.roads:
        assert road.oneway is None
        assert road.osm_u < road.osm_v


# --- writing to the database ----------------------------------------------------

SAMPLE = {10: (26.00, 48.30), 20: (26.05, 48.30), 30: (26.10, 48.30), 40: (26.15, 48.30)}


def _sample_graph():
    """Two-way streets 10-20 and 20-40, and a one-way street 30 -> 20."""
    graph = _graph(SAMPLE)
    _two_way(graph, 10, 20, osmid=601)
    _one_way(graph, 30, 20, osmid=602)
    _two_way(graph, 20, 40, osmid=603)
    return graph


def _stored(network):
    return [(road.osm_u, road.osm_v, road.osm_way_ids) for road in network.roads]


class FakeSession:
    """Just enough of a SQLAlchemy Session for write_roads; logs what it is asked to do."""

    def __init__(self, stored=()):
        self.stored = list(stored)  # (osm_u, osm_v, osm_way_ids) of roads already in the database
        self.log = []
        self.added = []
        self.inserted = {}

    def scalar(self, statement):
        self.log.append("count")
        return len(self.stored)

    def execute(self, statement, params=None):
        if statement.is_select:
            self.log.append("select")
            return types.SimpleNamespace(all=lambda: self.stored)
        self.log.append(f"{'delete' if statement.is_delete else 'insert'} {statement.table.name}")
        if params is not None:
            self.inserted[statement.table.name] = params

    def begin_nested(self):
        return nullcontext()

    def add_all(self, rows):
        self.log.append("add roads")
        self.added.extend(rows)

    def flush(self):
        for number, row in enumerate(self.added, start=1):
            row.id = number


def test_write_replaces_the_stored_network_and_keeps_oneway():
    network = transform_graph(_sample_graph())
    session = FakeSession(stored=[(1, 2, "9")])
    write_roads(session, network)  # replace is the default

    assert session.log == [
        "count",
        "delete road_end1_connections",
        "delete road_end2_connections",
        "delete roads",
        "add roads",
        "insert road_end1_connections",
        "insert road_end2_connections",
    ]
    assert [(row.osm_u, row.osm_v, row.oneway) for row in session.added] == [
        (road.osm_u, road.osm_v, road.oneway) for road in network.roads
    ]
    assert sorted(row.oneway for row in session.added) == [False, False, True]
    for table, connections in (("road_end1_connections", network.end1_connections),
                               ("road_end2_connections", network.end2_connections)):
        assert session.inserted[table] == [{"road_id": i + 1, "connected_road_id": j + 1} for i, j in connections]


def test_write_refuses_an_empty_network_before_touching_the_database():
    session = FakeSession(stored=[(1, 2, "9")])
    with pytest.raises(ImportRefused, match="empty"):
        write_roads(session, RoadNetwork([], [], [], 0))
    with pytest.raises(ImportRefused, match="empty"):
        write_roads(session, RoadNetwork([], [], [], 0), replace=False)
    assert session.log == []


def test_replace_refuses_a_network_much_smaller_than_the_stored_one():
    network = transform_graph(_sample_graph())
    stored = [(n, n + 1, str(n)) for n in range(1000, 1000 + 2 * len(network.roads) + 1)]
    session = FakeSession(stored)
    with pytest.raises(ImportRefused, match="would replace 7 stored roads with 3; refusing"):
        write_roads(session, network)
    assert session.log == ["count"] and session.added == []

    # Exactly half of the stored roads is still accepted, and force accepts less.
    write_roads(FakeSession(stored[:-1]), network)
    forced = FakeSession(stored)
    write_roads(forced, network, force=True)
    assert "delete roads" in forced.log and len(forced.added) == 3


def test_force_does_not_accept_an_empty_network():
    session = FakeSession(stored=[(1, 2, "9")])
    with pytest.raises(ImportRefused, match="empty"):
        write_roads(session, RoadNetwork([], [], [], 0), force=True)
    assert session.log == []


def test_append_refuses_roads_that_are_already_stored():
    network = transform_graph(_sample_graph())
    count = len(network.roads)
    stored = [(v, u, ids) for u, v, ids in _stored(network)]  # same segments, either orientation
    session = FakeSession(stored)
    with pytest.raises(ImportRefused, match=f"{count} of {count} roads are already stored"):
        write_roads(session, network, replace=False)
    assert session.log == ["select"] and session.added == []


def test_append_refuses_a_network_that_meets_stored_roads_at_a_junction():
    network = transform_graph(_sample_graph())
    session = FakeSession([(20, 999, "777")])  # another road through junction 20
    with pytest.raises(ImportRefused, match="0 of 3 roads are already stored and 1 OSM nodes are shared"):
        write_roads(session, network, replace=False)
    assert session.log == ["select"] and session.added == []


def test_append_adds_a_disjoint_network_without_deleting():
    network = transform_graph(_sample_graph())
    session = FakeSession([(1000, 1001, "777"), (None, None, None)])
    write_roads(session, network, replace=False)
    assert session.log == ["select", "add roads", "insert road_end1_connections", "insert road_end2_connections"]
    assert len(session.added) == len(network.roads)


@pytest.fixture(scope="module")
def scratch_engine():
    """A scratch PostGIS database on the DATABASE_URL server (never the app database); skips without one."""
    import database

    server = make_url(database.DATABASE_URL)
    admin = create_engine(server.set(database="postgres"), isolation_level="AUTOCOMMIT",
                          connect_args={"connect_timeout": 3})
    name = f"resqnet_test_{uuid.uuid4().hex[:10]}"
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except OperationalError as exc:
        admin.dispose()
        pytest.skip(f"no PostGIS server at {server.host}:{server.port}: {exc.orig}")
    engine = create_engine(server.set(database=name))
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        Base.metadata.create_all(engine)
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_postgis_rerun_replaces_and_a_failed_write_changes_nothing(scratch_engine):
    sessions = sessionmaker(bind=scratch_engine)
    network = transform_graph(_sample_graph())
    expected = (len(network.roads), len(network.end1_connections), len(network.end2_connections))

    def stored():
        with sessions() as session:
            return tuple(session.scalar(select(func.count()).select_from(table))
                         for table in (Road, road_end1_connections, road_end2_connections))

    for _ in range(2):  # a second run replaces the first, it does not add a copy
        with sessions.begin() as session:
            write_roads(session, network)
        assert stored() == expected
    with sessions() as session:
        rows = session.execute(select(Road.osm_way_ids, Road.osm_u, Road.osm_v, Road.oneway)).all()
    assert sorted(rows) == [("601", 10, 20, False), ("602", 30, 20, True), ("603", 20, 40, False)]

    with pytest.raises(ImportRefused, match="3 of 3 roads are already stored"):
        with sessions.begin() as session:
            write_roads(session, network, replace=False)
    assert stored() == expected

    # Fails after the delete and the road inserts; the savepoint undoes them even if the caller commits.
    broken = RoadNetwork(network.roads, [*network.end1_connections, (0, 99)], network.end2_connections, 1)
    with sessions() as session:
        with pytest.raises(IndexError):
            write_roads(session, broken)
        session.commit()
    assert stored() == expected


# --- command line ---------------------------------------------------------------

class FakeSessionFactory:
    """Stands in for database.SessionLocal: hands out one session, records commit or rollback."""

    def __init__(self, session):
        self.session = session
        self.outcomes = []

    @contextmanager
    def begin(self):
        try:
            yield self.session
        except Exception:
            self.outcomes.append("rollback")
            raise
        self.outcomes.append("commit")


@pytest.fixture
def cli(monkeypatch):
    """main() on the sample graph, with a fake ``database`` module."""
    factory = FakeSessionFactory(FakeSession())
    database = types.ModuleType("database")
    database.SessionLocal = factory
    database.init_db = lambda: None
    monkeypatch.setitem(sys.modules, "database", database)
    monkeypatch.setattr(ingestion_roads, "download_graph", lambda buffer: _sample_graph())
    return factory


@pytest.mark.parametrize("argv", [[], ["--replace"]])
def test_cli_replaces_by_default(cli, argv):
    cli.session.stored = _stored(transform_graph(_sample_graph()))  # a second run of the same import
    assert ingestion_roads.main(argv) == 0
    assert "delete roads" in cli.session.log
    assert len(cli.session.added) == 3
    assert cli.outcomes == ["commit"]


def test_cli_append_refuses_a_second_copy(cli, capsys):
    cli.session.stored = _stored(transform_graph(_sample_graph()))
    assert ingestion_roads.main(["--append"]) == 2
    assert "Import refused, database unchanged: refusing to append: 3 of 3 roads" in capsys.readouterr().err
    assert cli.session.log == ["select"] and cli.session.added == []
    assert cli.outcomes == ["rollback"]


def test_cli_force_accepts_a_smaller_network(cli):
    cli.session.stored = [(n, n + 1, str(n)) for n in range(1000, 1010)]
    assert ingestion_roads.main([]) == 2
    assert ingestion_roads.main(["--force"]) == 0
    assert "delete roads" in cli.session.log
    assert cli.outcomes == ["rollback", "commit"]


def test_cli_append_adds_without_deleting(cli):
    cli.session.stored = [(1000, 1001, "777")]
    assert ingestion_roads.main(["--append"]) == 0
    assert not any(entry.startswith("delete") for entry in cli.session.log)
    assert len(cli.session.added) == 3
    assert cli.outcomes == ["commit"]


def test_cli_rejects_replace_with_append(cli):
    with pytest.raises(SystemExit) as error:
        ingestion_roads.main(["--replace", "--append"])
    assert error.value.code == 2


def test_cli_dry_run_does_not_touch_the_database(cli):
    assert ingestion_roads.main(["--dry-run"]) == 0
    assert cli.session.log == [] and cli.outcomes == []


def test_download_uses_osm_cache_dir(monkeypatch):
    monkeypatch.setattr(ox.settings, "cache_folder", ox.settings.cache_folder)
    monkeypatch.setattr(ox.settings, "use_cache", ox.settings.use_cache)
    monkeypatch.setattr(ox, "graph_from_bbox", lambda bbox, **kwargs: (bbox, kwargs))

    monkeypatch.setenv("OSM_CACHE_DIR", "/srv/osm-cache")
    bbox, kwargs = ingestion_roads.download_graph(0.0)
    assert ox.settings.cache_folder == Path("/srv/osm-cache") and ox.settings.use_cache
    assert bbox == ingestion_roads.AOI02_BBOX and kwargs["network_type"] == "drive"

    monkeypatch.delenv("OSM_CACHE_DIR")
    assert ingestion_roads.cache_folder() == Path.home() / ".cache" / "resqnet" / "osm"
