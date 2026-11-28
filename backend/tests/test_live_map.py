from datetime import datetime, timezone

from models import AnalysisCoverage, DisasterArea, Road, Settlement
from database import SessionLocal
from geoalchemy2 import WKTElement

WATER = "POLYGON((26.00 48.20, 26.10 48.20, 26.10 48.30, 26.00 48.30, 26.00 48.20))"
OBSERVED = datetime(2020, 6, 26, 9, 13, tzinfo=timezone.utc)
EXPIRES = datetime(2020, 6, 27, 9, 13, tzinfo=timezone.utc)
REPLAY = "2020-06-27T00:00:00Z"
SOURCE = "CEMS EMSR444 AOI02"


def add_roads_and_village():
    with SessionLocal() as s:
        s.add(Road(geom=WKTElement("LINESTRING(25.95 48.25, 26.15 48.25)", srid=4326),
                   end1=WKTElement("POINT(25.95 48.25)", srid=4326),
                   end2=WKTElement("POINT(26.15 48.25)", srid=4326)))
        s.add(Road(geom=WKTElement("LINESTRING(26.50 48.50, 26.60 48.60)", srid=4326),
                   end1=WKTElement("POINT(26.50 48.50)", srid=4326),
                   end2=WKTElement("POINT(26.60 48.60)", srid=4326)))
        s.add(Settlement(name="Novoselytsia", population=7500,
                         area=WKTElement("POLYGON((26.05 48.22, 26.08 48.22, 26.08 48.24, 26.05 48.24, 26.05 48.22))", srid=4326)))
        s.add(Settlement(name="Dry village", population=300,
                         area=WKTElement("POLYGON((27 49, 27.01 49, 27.01 49.01, 27 49.01, 27 49))", srid=4326)))
        s.commit()


def flood(client, when="2020-06-23T10:00:00", *, timed=False):
    d = client.post("/disaster", json={"name": "Flood EMSR444", "disaster_type": 1, "creation_time": when})
    assert d.status_code == 201, d.text
    a = client.post("/disasterArea", json={"name": "water", "disaster_id": d.json()["id"], "area": WATER})
    assert a.status_code == 201, a.text
    if timed:
        with SessionLocal() as s:
            area = s.get(DisasterArea, a.json()["id"])
            area.observed_at, area.valid_until, area.source = OBSERVED, EXPIRES, SOURCE
            s.commit()
    return d.json()["id"]


def by_kind(body, kind):
    return [f for f in body["features"] if f["properties"]["kind"] == kind]


def test_seed_runs_on_startup_so_disasters_can_be_created(postgis_client):
    r = postgis_client.post("/disaster", json={"name": "x", "disaster_type": 1})
    assert r.status_code == 201, r.text


def test_empty_map(postgis_client):
    body = postgis_client.get("/api/map/live").json()
    assert body["type"] == "FeatureCollection"
    assert body["features"] == []
    assert body["summary"]["people_affected"] == 0
    assert body["summary"]["observations"] == []
    assert body["summary"]["roads_by_status"] == {"closed": 0, "mapped_option": 0, "unknown": 0}


def test_flooded_road_village_and_evacuation_point(postgis_client):
    client = postgis_client
    add_roads_and_village()
    flood(client)
    client.post("/evacuation-point", json={"name": "school", "geom": "POINT(26.05 48.25)"})
    client.post("/evacuation-point", json={"name": "stadium", "geom": "POINT(27.5 49.5)"})

    body = client.get("/api/map/live").json()
    areas = by_kind(body, "disaster_area")
    assert len(areas) == 1 and areas[0]["geometry"]["type"] == "Polygon"
    assert areas[0]["properties"]["untimed"] is True
    roads = [r["properties"] for r in by_kind(body, "road")]
    assert [r["flooded"] for r in roads] == [True, False]
    assert [(r["route_status"], r["reason"]) for r in roads] == [
        ("closed", "untimed_report"), ("unknown", "gap"),
    ]
    points = {p["properties"]["name"]: p["properties"]["in_danger"] for p in by_kind(body, "evacuation_point")}
    assert points == {"school": True, "stadium": False}
    assert body["summary"]["people_affected"] == 7500
    assert body["summary"]["roads_flooded"] == 1


def test_at_replays_time(postgis_client):
    client = postgis_client
    add_roads_and_village()
    flood(client, timed=True)
    naive = client.get("/api/map/live", params={"at": "2020-06-26T00:00:00"})
    assert naive.status_code == 400 and "UTC offset" in naive.json()["detail"]
    before = client.get("/api/map/live", params={"at": "2020-06-24T00:00:00Z"}).json()
    after = client.get("/api/map/live", params={"at": REPLAY}).json()
    expired = client.get("/api/map/live", params={"at": EXPIRES.isoformat()}).json()
    assert before["summary"]["disaster_areas"] == 0 and before["summary"]["roads_flooded"] == 0
    assert after["summary"]["disaster_areas"] == 1 and after["summary"]["roads_flooded"] == 1
    assert expired["summary"]["disaster_areas"] == 0 and expired["summary"]["roads_flooded"] == 0
    assert expired["summary"]["observations"] == []
    assert after["summary"]["observations"] == [{
        "source": SOURCE, "observed_at": OBSERVED.isoformat(), "valid_until": EXPIRES.isoformat(),
    }]


def test_bbox_filters_and_validates(postgis_client):
    client = postgis_client
    add_roads_and_village()
    flood(client)
    body = client.get("/api/map/live", params={"bbox": "26.4,48.4,26.7,48.7"}).json()
    assert len(by_kind(body, "road")) == 1 and by_kind(body, "disaster_area") == []
    assert client.get("/api/map/live", params={"bbox": "1,2,3"}).status_code == 400
    assert client.get("/api/map/live", params={"bbox": "5,5,1,1"}).status_code == 400


def test_disaster_filter(postgis_client):
    client = postgis_client
    add_roads_and_village()
    first = flood(client)
    body = client.get("/api/map/live", params={"disaster_id": first + 999}).json()
    assert body["summary"]["disaster_areas"] == 0
    assert body["summary"]["roads_flooded"] == 1


def test_route_and_map_share_evidence_on_replay(postgis_client):
    client = postgis_client
    add_roads_and_village()
    disaster_id = flood(client, timed=True)
    with SessionLocal() as s:
        start = s.get(Road, 1)
        next_road = Road(geom=WKTElement("LINESTRING(26.15 48.25, 26.4 48.25)", srid=4326),
                         end1=start.end2, end2=WKTElement("POINT(26.4 48.25)", srid=4326))
        start.end2_roads.append(next_road)
        s.add(AnalysisCoverage(
            disaster_id=disaster_id, name="Extent", kind="analysis_extent",
            observed_at=OBSERVED, valid_until=EXPIRES, source=SOURCE,
            area=WKTElement("MULTIPOLYGON(((25 48, 27 48, 27 49, 25 49, 25 48)))", srid=4326),
        ))
        s.flush()
        next_id = next_road.id
        s.commit()

    assert client.post("/evacuation-point", json={"name": "School", "geom": "POINT(26.3 48.25)"}).status_code == 201
    route_response = client.post("/evacuation-route", json={"lat": 48.25, "lon": 25.97, "at": REPLAY})
    assert route_response.status_code == 200, route_response.text
    route = route_response.json()
    map_body = client.get("/api/map/live", params={"at": REPLAY}).json()
    assert route["at"] == map_body["summary"]["at"] == REPLAY
    assert len(map_body["summary"]["observations"]) == 1
    assert map_body["summary"]["roads_by_status"] == {"closed": 1, "mapped_option": 2, "unknown": 0}
    assert route["status"] == "route_found" and route["route_status"] == "mapped_option"
    assert route["start_in_flood"] and route["in_disaster_area"]
    assert route["road_ids"] == [1, next_id] and route["limiting_segment_ids"] == [next_id]
    map_roads = {f["properties"]["id"]: f["properties"] for f in by_kind(map_body, "road")}
    for segment in route["route"]["features"]:
        props = segment["properties"]
        assert props == {key: map_roads[props["id"]][key] for key in props}

    expired = client.post("/evacuation-route", json={
        "lat": 48.25, "lon": 25.97, "at": EXPIRES.isoformat(),
    }).json()
    assert expired["road_ids"] == route["road_ids"]
    assert (expired["route_status"], expired["reason"], expired["start_in_flood"]) == ("unknown", "expired", False)
    assert expired["limiting_segment_ids"] == [next_id]
