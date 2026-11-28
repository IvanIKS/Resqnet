from datetime import datetime, timezone

from geoalchemy2 import WKTElement

from database import SessionLocal
from models import DisasterArea, Settlement

# Village 0.02 x 0.02 deg. LEFT covers its western half, RIGHT its eastern half,
# BOTH covers all of it.
VILLAGE = "POLYGON((26.00 48.00, 26.02 48.00, 26.02 48.02, 26.00 48.02, 26.00 48.00))"
LEFT = "POLYGON((25.90 47.90, 26.01 47.90, 26.01 48.10, 25.90 48.10, 25.90 47.90))"
RIGHT = "POLYGON((26.01 47.90, 26.10 47.90, 26.10 48.10, 26.01 48.10, 26.01 47.90))"
BOTH = "POLYGON((25.90 47.90, 26.10 47.90, 26.10 48.10, 25.90 48.10, 25.90 47.90))"


def add_village(population=1000, wkt=VILLAGE, name="Village"):
    with SessionLocal() as s:
        s.add(Settlement(name=name, population=population, area=WKTElement(wkt, srid=4326)))
        s.commit()


def new_disaster(postgis_client, when="2020-06-23T10:00:00"):
    r = postgis_client.post("/disaster", json={"name": "Flood", "disaster_type": 1, "creation_time": when})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def add_zone(postgis_client, disaster_id, wkt, name="zone"):
    r = postgis_client.post("/disasterArea", json={"name": name, "disaster_id": disaster_id, "area": wkt})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def estimate(postgis_client, disaster_id, **params):
    r = postgis_client.get(f"/api/disasters/{disaster_id}/people-estimate", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_zone_over_half_a_village_counts_half_its_people(postgis_client):
    add_village(1000)
    d = new_disaster(postgis_client)
    zone_id = add_zone(postgis_client, d, LEFT, "left")

    body = estimate(postgis_client, d)

    assert len(body["zones"]) == 1
    zone = body["zones"][0]
    assert zone["area_id"] == zone_id and zone["name"] == "left"
    assert 490 <= zone["people"] <= 510
    assert zone["settlements"][0]["name"] == "Village"
    assert 0.49 <= zone["settlements"][0]["share"] <= 0.51
    assert body["total_people"] == zone["people"]
    assert body["method"] == "settlement_population_by_area"


def test_each_zone_is_counted_separately_and_total_has_no_double_count(postgis_client):
    add_village(1000)
    d = new_disaster(postgis_client)
    add_zone(postgis_client, d, LEFT, "left")
    add_zone(postgis_client, d, RIGHT, "right")
    add_zone(postgis_client, d, BOTH, "both")

    body = estimate(postgis_client, d)

    people = {z["name"]: z["people"] for z in body["zones"]}
    assert 490 <= people["left"] <= 510 and 490 <= people["right"] <= 510
    assert people["both"] == 1000
    assert body["total_people"] == 1000


def test_dry_village_and_other_disaster_are_ignored(postgis_client):
    add_village(1000)
    add_village(500, "POLYGON((27 49, 27.01 49, 27.01 49.01, 27 49.01, 27 49))", "Dry")
    d = new_disaster(postgis_client)
    other = new_disaster(postgis_client)
    add_zone(postgis_client, other, BOTH, "other flood")

    body = estimate(postgis_client, d)

    assert body["zones"] == [] and body["total_people"] == 0


def test_zone_without_settlements_reports_zero(postgis_client):
    d = new_disaster(postgis_client)
    add_zone(postgis_client, d, BOTH)

    body = estimate(postgis_client, d)

    assert body["zones"][0]["people"] == 0 and body["zones"][0]["settlements"] == []


def test_satellite_zone_counts_only_while_valid(postgis_client):
    add_village(1000)
    d = new_disaster(postgis_client)
    with SessionLocal() as s:
        s.add(DisasterArea(
            name="EMS", disaster_id=d, area=WKTElement(BOTH, srid=4326), kind="observed_flood",
            observed_at=datetime(2020, 6, 24, 9, tzinfo=timezone.utc),
            valid_until=datetime(2020, 6, 24, 21, tzinfo=timezone.utc),
        ))
        s.commit()

    assert estimate(postgis_client, d, at="2020-06-24T08:00:00Z")["total_people"] == 0
    assert estimate(postgis_client, d, at="2020-06-24T12:00:00Z")["total_people"] == 1000
    assert estimate(postgis_client, d, at="2020-06-25T00:00:00Z")["total_people"] == 0


def test_manual_zone_counts_from_disaster_start(postgis_client):
    add_village(1000)
    d = new_disaster(postgis_client, when="2020-06-23T10:00:00")
    add_zone(postgis_client, d, BOTH)

    assert estimate(postgis_client, d, at="2020-06-23T09:00:00Z")["total_people"] == 0
    assert estimate(postgis_client, d, at="2020-06-23T11:00:00+00:00")["total_people"] == 1000


def test_unknown_disaster_is_404(postgis_client):
    r = postgis_client.get("/api/disasters/999/people-estimate")
    assert r.status_code == 404
