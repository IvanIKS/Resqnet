"""Rebuild the pinned RESQNET demo and emit its deterministic replay fixture."""

import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import text


BACKEND = Path(__file__).resolve().parents[1]
DEMO_URL = "postgresql+psycopg2://resqnet:resqnet@localhost:5432/resqnet_demo"
CONTAINER = "resqnet-db-1"
DOCKER_EXEC = ("docker", "exec", CONTAINER)
PSQL = (*DOCKER_EXEC, "psql", "-X", "-U", "resqnet", "-d", "resqnet_demo", "-v", "ON_ERROR_STOP=1")
DUMP = Path("D:/cassini-review/out/resqnet_aoi02_seed.dump")
ZIP = Path("D:/cassini-2026/data/raw/EMSR444_AOI02_DEL_PRODUCT_r1_RTP01_v1_vector.zip")
FIXTURE = Path("D:/cassini-2026/analysis/21-demo-fixture.json")
INPUT_HASHES = {
    DUMP: "0fe6447cb152cda3d5e43131cffdbb693e38fb7b654a64ea3ba456bc3d6cfbc4",
    ZIP: "ef4db492680aeadb8d49cbdb88927e1bda74672142a931cd872c3652cb807759",
}
AT = "2020-06-27T00:00:00Z"
EXPIRED_AT = "2020-06-27T09:13:00Z"
BBOX = "25.85,48.25,26.00,48.34"
BOUNDS = tuple(map(float, BBOX.split(",")))
POINT_NAME = "Synthetic evacuation point · demo only"
POINT_LAT, POINT_LON = 48.317463550, 25.878996250
PREFERRED_ROAD = 2158
FALLBACK_LAT, FALLBACK_LON = 48.312002568, 25.918210108
FALLBACK_PATH = [48, 2158, 2161, 2399, 2400, 2405, 6359, 6358, 6353, 6351, 6349, 6345, 6344]
EXPECTED_COUNTS = {
    "roads": 26793,
    "road_end1_connections": 52009,
    "road_end2_connections": 48005,
    "roads_id_seq": 26793,
    "disaster_areas": 349,
    "analysis_coverage": 93,
    "untimed_evidence": 0,
    "settlements": 0,
}


def require_equal(label, actual, expected):
    if actual != expected:
        raise RuntimeError(f"{label}: expected {expected!r}, got {actual!r}")


def run(*command, **kwargs):
    return subprocess.run(command, cwd=BACKEND, check=True, **kwargs)


def restore_roads():
    stage = run(*DOCKER_EXEC, "mktemp", "-d", "/tmp/resqnet-demo.XXXXXX",
                stdout=subprocess.PIPE, text=True).stdout.strip()
    archive = f"{stage}/roads.dump"
    restore_list = f"{stage}/roads.list"
    try:
        run("docker", "cp", DUMP, f"{CONTAINER}:{archive}")
        toc = run(*DOCKER_EXEC, "pg_restore", "-l", archive,
                  stdout=subprocess.PIPE, encoding="utf-8").stdout.splitlines()
        ordered = []
        for entry in (
            "TABLE DATA public roads",
            "TABLE DATA public road_end1_connections",
            "TABLE DATA public road_end2_connections",
            "SEQUENCE SET public roads_id_seq",
        ):
            matches = [line for line in toc if re.match(rf"\d+; \d+ \d+ {re.escape(entry)} ", line)]
            require_equal(f"TOC matches for {entry}", len(matches), 1)
            ordered.append(matches[0])
        run("docker", "exec", "-i", CONTAINER, "tee", restore_list,
            input=("\n".join(ordered) + "\n").encode("utf-8"), stdout=subprocess.DEVNULL)
        run(*DOCKER_EXEC, "pg_restore", "-U", "resqnet", "-d", "resqnet_demo",
            "--data-only", "--single-transaction", "--exit-on-error", "-L", restore_list, archive)
    finally:
        run(*DOCKER_EXEC, "rm", "-f", archive, restore_list)
        run(*DOCKER_EXEC, "rmdir", stage)


def check_database(session):
    actual = session.execute(text("SELECT current_database(), current_setting('jit')")).one()
    require_equal("Database and JIT", tuple(actual), ("resqnet_demo", "off"))


def check_counts(session):
    counts = dict(session.execute(text("""
        SELECT (SELECT count(*) FROM roads) AS roads,
               (SELECT count(*) FROM road_end1_connections) AS road_end1_connections,
               (SELECT count(*) FROM road_end2_connections) AS road_end2_connections,
               (SELECT last_value FROM roads_id_seq) AS roads_id_seq,
               (SELECT count(*) FROM disaster_areas) AS disaster_areas,
               (SELECT count(*) FROM analysis_coverage) AS analysis_coverage,
               ((SELECT count(*) FROM disaster_areas WHERE observed_at IS NULL OR valid_until IS NULL)
                + (SELECT count(*) FROM analysis_coverage WHERE observed_at IS NULL OR valid_until IS NULL)
               ) AS untimed_evidence,
               (SELECT count(*) FROM settlements) AS settlements
    """)).mappings().one())
    require_equal("Table and sequence counts", counts, EXPECTED_COUNTS)
    return counts


def insert_point(session):
    params = {"name": POINT_NAME, "lon": POINT_LON, "lat": POINT_LAT}
    session.execute(text("""
        INSERT INTO evacuation_points (name, geom)
        SELECT :name, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)
        WHERE NOT EXISTS (SELECT 1 FROM evacuation_points WHERE name = :name)
    """), params)
    point = dict(session.execute(text("""
        SELECT id, name, ST_Y(geom) AS lat, ST_X(geom) AS lon, ST_SRID(geom) AS srid
        FROM evacuation_points WHERE name = :name
    """), params).mappings().one())
    require_equal("Evacuation point geometry", (point["lat"], point["lon"], point["srid"]),
                  (POINT_LAT, POINT_LON, 4326))
    session.commit()
    return {**point, **snap(session, point["lat"], point["lon"])}


def snap(session, lat, lon):
    # Match the route service's planar nearest-road ordering, then measure in metres.
    row = session.execute(text("""
        SELECT id, ST_Distance(CAST(geom AS geography), CAST(point AS geography))
        FROM roads CROSS JOIN ST_SetSRID(ST_MakePoint(:lon, :lat), 4326) AS point
        ORDER BY ST_Distance(geom, point), id
        LIMIT 1
    """), {"lat": lat, "lon": lon}).one()
    return {"snapped_road_id": row[0], "snap_distance_meters": row[1]}


def evaluate_click(session, lat, lon, point, viewport_ids, *, fallback=False):
    from services.evacuation import get_evacuation_route

    click = {"lat": lat, "lon": lon, **snap(session, lat, lon)}
    result = get_evacuation_route(session, lat=lat, lon=lon, at=datetime.fromisoformat(AT))
    closed = [
        feature["properties"]["id"]
        for feature in (result.route or {}).get("features", [])
        if feature["properties"]["route_status"] == "closed"
    ]
    route = {
        "status": result.status,
        "route_status": result.route_status,
        "reason": result.reason,
        "start_in_flood": result.start_in_flood,
        "road_ids": result.road_ids,
        "limiting_segment_ids": result.limiting_segment_ids,
        "distance_meters": result.distance_meters,
        "target_point": {"id": result.target_point_id, "name": result.target_point_name},
        "closed_feature_ids": closed,
    }
    checks = {
        "route_found/unknown/gap": (result.status, result.route_status, result.reason)
        == ("route_found", "unknown", "gap"),
        "one to five downstream limiting IDs": 1 <= len(result.limiting_segment_ids) <= 5
        and set(result.limiting_segment_ids).issubset(result.road_ids[1:]),
        "both snaps below 30 m": click["snap_distance_meters"] < 30 and point["snap_distance_meters"] < 30,
        "path endpoints match snaps": result.road_ids[:1] == [click["snapped_road_id"]]
        and result.road_ids[-1:] == [point["snapped_road_id"]],
        "synthetic destination": route["target_point"] == {"id": point["id"], "name": POINT_NAME},
        "path in map viewport": set(result.road_ids).issubset(viewport_ids),
        "expected flood state at start": result.start_in_flood == fallback,
        "expected closed route features": closed == ([48] if fallback else []),
    }
    if fallback:
        checks["archive fallback path, limiting IDs and distance"] = (
            result.road_ids == FALLBACK_PATH
            and result.limiting_segment_ids == [6353, 6351, 6349]
            and result.distance_meters == 3610.40
        )
    failed = [check for check, passed in checks.items() if not passed]
    return {"click": click, "route": route, "failed_acceptance": failed}


def find_alternate_click(session, preferred, point, viewport, statuses):
    from services.evacuation import _find_route, _is_in_disaster_area, _load_road_graph, _route_evidence

    if point["snap_distance_meters"] >= 30:
        raise RuntimeError("The destination has no road snap within 30 m")
    adjacency, lengths = _load_road_graph(session)
    targets = {point["snapped_road_id"]}
    at = datetime.fromisoformat(AT)
    lat0, lon0 = preferred["lat"], preferred["lon"]
    candidates = sorted(viewport.values(), key=lambda road: ((road.lat - lat0)**2 + (road.lon - lon0)**2, road.id))
    west, south, east, north = BOUNDS
    for road in candidates:
        if not (west <= road.lon <= east and south <= road.lat <= north):
            continue
        # An unknown start would itself be a limiting segment.
        if statuses[road.id].route_status != "mapped_option":
            continue
        found = _find_route(road.id, targets, adjacency, lengths, statuses)
        if found is None:
            continue
        path, _ = found
        status, reason, limiting = _route_evidence(path, statuses)
        if (status, reason) != ("unknown", "gap") or not 1 <= len(limiting) <= 5:
            continue
        if not set(path).issubset(viewport) or not set(limiting).issubset(path[1:]):
            continue
        start = snap(session, road.lat, road.lon)
        if start["snapped_road_id"] != road.id or start["snap_distance_meters"] >= 30:
            continue
        if not _is_in_disaster_area(session, f"POINT({road.lon} {road.lat})", at):
            candidate = evaluate_click(session, road.lat, road.lon, point, viewport)
            if not candidate["failed_acceptance"]:
                return candidate
    raise RuntimeError("No start in the viewport met the route acceptance checks")


def make_fixture(session, hashes, git_commit):
    from services.road_status import road_statuses

    check_database(session)
    counts = check_counts(session)
    statuses = road_statuses(session, datetime.fromisoformat(AT))
    midnight = dict(Counter(evidence.route_status for evidence in statuses.values()))
    require_equal("Midnight road statuses", midnight, {"closed": 228, "mapped_option": 8348, "unknown": 18217})
    expired_statuses = road_statuses(session, datetime.fromisoformat(EXPIRED_AT))
    expired = dict(Counter(evidence.route_status for evidence in expired_statuses.values()))
    expired_reasons = dict(Counter(evidence.reason for evidence in expired_statuses.values()))
    require_equal("Expiry road statuses", expired, {"unknown": 26793})
    require_equal("Expiry reasons", expired_reasons, {"expired": 10629, "gap": 16164})

    point = insert_point(session)
    require_equal("Synthetic point snap", point["snapped_road_id"], 6344)
    viewport = {
        row.id: row for row in session.execute(text("""
            SELECT id, ST_Y(midpoint) AS lat, ST_X(midpoint) AS lon
            FROM (
                SELECT id, ST_LineInterpolatePoint(geom, 0.5) AS midpoint
                FROM roads WHERE geom && ST_MakeEnvelope(:west, :south, :east, :north, 4326)
            ) AS midpoints
        """), dict(zip(("west", "south", "east", "north"), BOUNDS)))
    }
    preferred_road = viewport[PREFERRED_ROAD]
    preferred = evaluate_click(session, preferred_road.lat, preferred_road.lon, point, viewport)
    if preferred["click"]["snapped_road_id"] != PREFERRED_ROAD:
        preferred["failed_acceptance"].append(f"snaps to road {PREFERRED_ROAD}")
    attempts = {"preferred": preferred}
    if not preferred["failed_acceptance"]:
        selection = "preferred"
        why = "Road 2158 met every route acceptance check."
    else:
        fallback = evaluate_click(session, FALLBACK_LAT, FALLBACK_LON, point, viewport, fallback=True)
        attempts["fallback"] = fallback
        if not fallback["failed_acceptance"]:
            selection = "fallback"
            why = "Road 2158 failed acceptance. Draw the start marker and omit closed road 48 from the route line."
        else:
            attempts["search"] = find_alternate_click(session, preferred["click"], point, viewport, statuses)
            selection = "search"
            why = "Both pinned clicks failed acceptance. An in-process search found a qualifying start."
    chosen = attempts[selection]
    require_equal("Chosen click failures", chosen["failed_acceptance"], [])

    return {
        "database": "resqnet_demo",
        "git_commit": git_commit,
        "input_sha256": hashes,
        "at": AT,
        "bbox": BBOX,
        "counts": counts,
        "road_statuses": {AT: midnight, EXPIRED_AT: expired},
        "expiry_reasons": expired_reasons,
        "viewport_road_statuses": dict(Counter(statuses[road_id].route_status for road_id in viewport)),
        "click": {**chosen["click"], "selection": selection, "why": why},
        "point": point,
        "route": chosen["route"],
        "click_attempts": attempts,
    }


def main():
    configured_url = os.getenv("DATABASE_URL", DEMO_URL)
    if configured_url != DEMO_URL or urlsplit(configured_url).path != "/resqnet_demo":
        raise SystemExit("Refusing to rebuild: DATABASE_URL must match the documented resqnet_demo URL.")
    os.environ["DATABASE_URL"] = DEMO_URL

    hashes = {}
    for path, expected in INPUT_HASHES.items():
        with path.open("rb") as file:
            actual = hashlib.file_digest(file, "sha256").hexdigest()
        require_equal(f"SHA-256 for {path}", actual, expected)
        hashes[path.name] = actual
    git_commit = run("git", "rev-parse", "HEAD", stdout=subprocess.PIPE, text=True).stdout.strip()

    if __package__ is None:
        sys.path.insert(0, str(BACKEND))
    from database import SessionLocal

    run(*DOCKER_EXEC, "dropdb", "-U", "resqnet", "--maintenance-db=postgres", "--if-exists", "resqnet_demo",
        stdout=sys.stderr)
    run(*DOCKER_EXEC, "createdb", "-U", "resqnet", "--maintenance-db=postgres", "-T", "template_postgis",
        "resqnet_demo", stdout=sys.stderr)
    run(*PSQL, "-c", "ALTER DATABASE resqnet_demo SET jit = off", stdout=sys.stderr)
    with SessionLocal() as session:
        check_database(session)

    run(sys.executable, "-B", "-m", "seed", stdout=sys.stderr)
    restore_roads()
    run(sys.executable, "-B", "-m", "service.ingestion_flood", "--zip", ZIP, "--max-age-hours", "24",
        stdout=sys.stderr)
    run(*PSQL, "-c", "ANALYZE", stdout=sys.stderr)

    with SessionLocal() as session:
        fixture = make_fixture(session, hashes, git_commit)
    payload = json.dumps(fixture, indent=2, sort_keys=True)
    temporary = FIXTURE.with_suffix(".json.tmp")
    temporary.write_text(payload + "\n", encoding="utf-8", newline="\n")
    temporary.replace(FIXTURE)
    print(payload)


if __name__ == "__main__":
    main()
