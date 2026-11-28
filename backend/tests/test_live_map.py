from datetime import datetime

from models import Road, Settlement
from database import SessionLocal
from geoalchemy2 import WKTElement

WATER = "POLYGON((26.00 48.20, 26.10 48.20, 26.10 48.30, 26.00 48.30, 26.00 48.20))"


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


def flood(client, when="2020-06-23T10:00:00"):
    d = client.post("/disaster", json={"name": "Flood EMSR444", "disaster_type": 1, "creation_time": when})
    assert d.status_code == 201, d.text
    a = client.post("/disasterArea", json={"name": "water", "disaster_id": d.json()["id"], "area": WATER})
    assert a.status_code == 201, a.text
    return d.json()["id"]


def by_kind(body, kind):
    return [f for f in body["features"] if f["properties"]["kind"] == kind]


def test_seed_runs_on_startup_so_disasters_can_be_created(client):
    r = client.post("/disaster", json={"name": "x", "disaster_type": 1})
    assert r.status_code == 201, r.text


def test_empty_map(client):
    body = client.get("/api/map/live").json()
    assert body["type"] == "FeatureCollection"
    assert body["features"] == []
    assert body["summary"]["people_affected"] == 0


def test_flooded_road_village_and_evacuation_point(client):
    add_roads_and_village()
    flood(client)
    client.post("/evacuation-point", json={"name": "school", "geom": "POINT(26.05 48.25)"})
    client.post("/evacuation-point", json={"name": "stadium", "geom": "POINT(27.5 49.5)"})

    body = client.get("/api/map/live").json()
    areas = by_kind(body, "disaster_area")
    assert len(areas) == 1 and areas[0]["geometry"]["type"] == "Polygon"
    assert [r["properties"]["flooded"] for r in by_kind(body, "road")] == [True, False]
    points = {p["properties"]["name"]: p["properties"]["in_danger"] for p in by_kind(body, "evacuation_point")}
    assert points == {"school": True, "stadium": False}
    assert body["summary"]["people_affected"] == 7500
    assert body["summary"]["roads_flooded"] == 1


def test_at_replays_time(client):
    add_roads_and_village()
    flood(client, when="2020-06-23T10:00:00")
    before = client.get("/api/map/live", params={"at": "2020-06-22T00:00:00"}).json()
    after = client.get("/api/map/live", params={"at": "2020-06-24T00:00:00Z"}).json()
    assert before["summary"]["disaster_areas"] == 0 and before["summary"]["roads_flooded"] == 0
    assert after["summary"]["disaster_areas"] == 1 and after["summary"]["roads_flooded"] == 1


def test_bbox_filters_and_validates(client):
    add_roads_and_village()
    flood(client)
    body = client.get("/api/map/live", params={"bbox": "26.4,48.4,26.7,48.7"}).json()
    assert len(by_kind(body, "road")) == 1 and by_kind(body, "disaster_area") == []
    assert client.get("/api/map/live", params={"bbox": "1,2,3"}).status_code == 400
    assert client.get("/api/map/live", params={"bbox": "5,5,1,1"}).status_code == 400


def test_disaster_filter(client):
    add_roads_and_village()
    first = flood(client)
    body = client.get("/api/map/live", params={"disaster_id": first + 999}).json()
    assert body["summary"]["disaster_areas"] == 0
