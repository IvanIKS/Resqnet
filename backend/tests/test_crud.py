"""Basic CRUD tests for the API endpoints.

Covers create / get-by-id / get-all / delete for disasters, disaster areas
and evacuation points, plus the alarm side effect of declaring a disaster.
"""


# --- Disaster ---------------------------------------------------------------

def test_create_disaster_raises_alarm(client, alarm_spy):
    resp = client.post("/disaster", json={"name": "Flood A", "disaster_type": 1})
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == 1
    assert body["name"] == "Flood A"
    assert body["disaster_type"] == 1
    # Declaring a disaster must trigger the injected alarm service.
    assert alarm_spy.raised == [1]


def test_create_disaster_unknown_type_404(client):
    resp = client.post("/disaster", json={"name": "X", "disaster_type": 999})
    assert resp.status_code == 404


def test_get_disaster(client):
    created = client.post("/disaster", json={"name": "Flood A", "disaster_type": 1}).json()
    resp = client.get(f"/disaster/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Flood A"


def test_get_disaster_not_found(client):
    assert client.get("/disaster/123").status_code == 404


def test_list_disasters(client):
    client.post("/disaster", json={"name": "Flood A", "disaster_type": 1})
    client.post("/disaster", json={"name": "Flood B", "disaster_type": 1})
    resp = client.get("/disaster/all")
    assert resp.status_code == 200
    names = {d["name"] for d in resp.json()}
    assert names == {"Flood A", "Flood B"}


def test_delete_disaster(client):
    created = client.post("/disaster", json={"name": "Flood A", "disaster_type": 1}).json()
    assert client.delete(f"/disaster/{created['id']}").status_code == 204
    assert client.get(f"/disaster/{created['id']}").status_code == 404


def test_delete_disaster_not_found(client):
    assert client.delete("/disaster/123").status_code == 404


# --- Disaster area ----------------------------------------------------------

AREA_WKT = "POLYGON((30.0 50.0, 30.1 50.0, 30.1 50.1, 30.0 50.1, 30.0 50.0))"


def _make_disaster(client):
    return client.post("/disaster", json={"name": "D", "disaster_type": 1}).json()["id"]


def test_disaster_area_crud(client):
    disaster_id = _make_disaster(client)

    created = client.post(
        "/disasterArea",
        json={"name": "Zone 1", "disaster_id": disaster_id, "area": AREA_WKT},
    )
    assert created.status_code == 201
    area_id = created.json()["id"]

    assert client.get(f"/disasterArea/{area_id}").status_code == 200
    assert len(client.get("/disasterArea/all").json()) == 1
    assert client.delete(f"/disasterArea/{area_id}").status_code == 204
    assert client.get(f"/disasterArea/{area_id}").status_code == 404


def test_disaster_area_unknown_disaster_404(client):
    resp = client.post(
        "/disasterArea",
        json={"name": "Zone", "disaster_id": 999, "area": AREA_WKT},
    )
    assert resp.status_code == 404


# --- Evacuation point -------------------------------------------------------

POINT_WKT = "POINT(30.5 50.4)"


def test_evacuation_point_crud(client):
    created = client.post(
        "/evacuation-point", json={"name": "Shelter", "geom": POINT_WKT}
    )
    assert created.status_code == 201
    point_id = created.json()["id"]
    assert created.json()["name"] == "Shelter"

    assert client.get(f"/evacuation-point/{point_id}").status_code == 200
    assert len(client.get("/evacuation-point/all").json()) == 1
    assert client.delete(f"/evacuation-point/{point_id}").status_code == 204
    assert client.get(f"/evacuation-point/{point_id}").status_code == 404


def test_evacuation_point_not_found(client):
    assert client.get("/evacuation-point/123").status_code == 404
