import networkx as nx
from shapely.geometry import LineString, Point

from service.ingestion_roads import transform_graph


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
