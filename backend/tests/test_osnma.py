"""OSNMA runs OSNMAlib on the real recording once per sample; the verdicts are cached for the session."""

from datetime import datetime, timezone

from geoalchemy2 import WKTElement

from database import SessionLocal
from models import DisasterArea, Road
from services import osnma

FLOODED_ROAD = "LINESTRING(25.95 48.25, 26.15 48.25)"
WATER = "POLYGON((26.00 48.20, 26.10 48.20, 26.10 48.30, 26.00 48.30, 26.00 48.20))"


def add_road(wkt=FLOODED_ROAD):
    with SessionLocal() as s:
        coords = wkt[wkt.index("(") + 1:-1].split(", ")
        road = Road(geom=WKTElement(wkt, srid=4326),
                    end1=WKTElement(f"POINT({coords[0]})", srid=4326),
                    end2=WKTElement(f"POINT({coords[-1]})", srid=4326))
        s.add(road)
        s.commit()
        return road.id


def report(client, road_id, sample, status="passable", **extra):
    r = client.post("/api/reports", json={
        "road_id": road_id, "point": "POINT(26.05 48.25)", "status": status,
        "osnma_sample": sample, "observed_at": "2026-03-09T15:30:00Z", **extra,
    })
    assert r.status_code == 201, r.text
    return r.json()


def test_authentic_recording_is_authenticated_by_osnmalib():
    result = osnma.check_sample("ublox_authentic")
    detail = result["detail"]
    assert result["verdict"] == "authenticated"
    assert detail["tags_failed"] == 0 and detail["tags_authenticated"] > 100
    assert len(detail["satellites_authenticated"]) >= 4
    assert detail["gst_first"]["utc"].startswith("2026-03-09")
    assert detail["checked_by"] == "OSNMAlib v3.2.5" and detail["source"] == "recorded sample"


def test_forged_recording_is_rejected_by_osnmalib():
    result = osnma.check_sample("forged")
    detail = result["detail"]
    assert result["verdict"] == "rejected"
    assert detail["tags_failed"] > 0
    assert detail["satellites_failed"] == [f"E{s:02d}" for s in osnma.FORGED_SVIDS]
    assert not set(detail["satellites_failed"]) & set(detail["satellites_authenticated"])


def test_forged_file_differs_only_in_a_few_bits(tmp_path):
    forged = tmp_path / "forged.ubx"
    pages = osnma.forge_ubx(osnma.RECORDING, forged)
    original, changed = osnma.RECORDING.read_bytes(), forged.read_bytes()
    assert len(original) == len(changed)
    diff_bytes = sum(a != b for a, b in zip(original, changed))
    assert 0 < pages and diff_bytes <= 3 * pages


def test_create_and_list_reports(postgis_client):
    client = postgis_client
    road_id = add_road()
    good = report(client, road_id, "ublox_authentic")
    bad = report(client, road_id, "forged")
    plain = client.post("/api/reports", json={"point": "POINT(26.05 48.25)", "status": "blocked"}).json()

    assert good["osnma_verdict"] == "authenticated" and good["osnma_detail"]["satellites_authenticated"]
    assert bad["osnma_verdict"] == "rejected" and bad["osnma_detail"]["tags_failed"] > 0
    assert plain["osnma_verdict"] == "not_checked" and plain["osnma_detail"] is None
    assert (good["lon"], good["lat"]) == (26.05, 48.25)
    assert good["observed_at"].startswith("2026-03-09T15:30:00")

    listed = client.get("/api/reports").json()
    assert [r["osnma_verdict"] for r in listed] == ["authenticated", "rejected", "not_checked"]


def test_report_validation(postgis_client):
    client = postgis_client
    road_id = add_road()
    base = {"point": "POINT(26.05 48.25)", "status": "passable"}
    assert client.post("/api/reports", json={**base, "road_id": road_id + 999}).status_code == 404
    assert client.post("/api/reports", json={**base, "osnma_sample": "phone"}).status_code == 422
    assert client.post("/api/reports", json={**base, "status": "open"}).status_code == 422
    assert client.post("/api/reports", json={**base, "observed_at": "2026-03-09T15:30:00"}).status_code == 422
    assert client.post("/api/reports", json={**base, "point": "LINESTRING(1 1, 2 2)"}).status_code == 400
    assert client.post("/api/reports", json={**base, "point": "not wkt"}).status_code == 400


def road_at(client, road_id, at):
    body = client.get("/api/map/live", params={"at": at}).json()
    road = next(f for f in body["features"] if f["properties"]["kind"] == "road" and f["properties"]["id"] == road_id)
    return road["properties"], body


def test_only_authenticated_reports_close_a_road(postgis_client):
    client = postgis_client
    road_id = add_road()
    assert road_at(client, road_id, "2026-03-09T17:00:00Z")[0]["route_status"] == "unknown"

    report(client, road_id, "forged", status="blocked")
    client.post("/api/reports", json={"road_id": road_id, "point": "POINT(26.05 48.25)", "status": "flooded",
                                      "observed_at": "2026-03-09T15:40:00Z"})
    road, body = road_at(client, road_id, "2026-03-09T17:00:00Z")
    assert (road["route_status"], road["reason"]) == ("unknown", "gap")
    assert body["summary"]["reports_rejected"] == 1 and body["summary"]["reports_authenticated"] == 0

    report(client, road_id, "ublox_authentic", status="flooded", observed_at="2026-03-09T16:00:00Z")
    road, body = road_at(client, road_id, "2026-03-09T17:00:00Z")
    assert (road["route_status"], road["reason"]) == ("closed", "field_report")
    assert road["flooded"] is True and body["summary"]["roads_by_status"]["closed"] == 1
    reports = [f for f in body["features"] if f["properties"]["kind"] == "field_report"]
    assert [r["properties"]["osnma_verdict"] for r in reports] == ["rejected", "not_checked", "authenticated"]
    assert reports[0]["geometry"] == {"type": "Point", "coordinates": [26.05, 48.25]}
    assert body["summary"]["reports_total"] == 3 and body["summary"]["reports_authenticated"] == 1

    before, body = road_at(client, road_id, "2026-03-09T15:45:00Z")
    assert before["route_status"] == "unknown" and body["summary"]["reports_total"] == 2

    # A later authenticated "passable" lifts the field closure but never makes the road better than unknown.
    report(client, road_id, "ublox_authentic", status="passable", observed_at="2026-03-09T16:30:00Z")
    road, _ = road_at(client, road_id, "2026-03-09T17:00:00Z")
    assert (road["route_status"], road["reason"]) == ("unknown", "gap")
    assert road_at(client, road_id, "2026-03-09T16:15:00Z")[0]["reason"] == "field_report"


def test_authenticated_passable_report_does_not_open_a_flooded_road(postgis_client):
    client = postgis_client
    road_id = add_road()
    d = client.post("/disaster", json={"name": "Flood", "disaster_type": 1, "creation_time": "2026-03-09T10:00:00"})
    area = client.post("/disasterArea", json={"name": "water", "disaster_id": d.json()["id"], "area": WATER})
    with SessionLocal() as s:
        row = s.get(DisasterArea, area.json()["id"])
        row.observed_at = datetime(2026, 3, 9, 12, tzinfo=timezone.utc)
        row.valid_until = datetime(2026, 3, 10, 12, tzinfo=timezone.utc)
        row.source = "test image"
        s.commit()

    report(client, road_id, "ublox_authentic", status="passable", observed_at="2026-03-09T16:00:00Z")
    road, _ = road_at(client, road_id, "2026-03-09T17:00:00Z")
    assert (road["route_status"], road["reason"]) == ("closed", None)
