"""Database tests for the flood schema and the append-only flood import:
upgrading a master database in place, history, idempotent re-import, the
valid-at-T query, the empty-package guard and rollback.

Each test runs in its own scratch database on the PostGIS server from
DATABASE_URL (created and dropped here, never the app database). Skipped when
no server is reachable. Run from backend/: python -m pytest tests -q
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from geoalchemy2 import WKTElement
from shapely.geometry import LineString, box
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

import database  # noqa: E402
from models import AnalysisCoverage, Disaster, DisasterArea, Road  # noqa: E402
from service import ingestion_flood as fl  # noqa: E402
from services.road_status import SegmentStatus, road_statuses  # noqa: E402

T0 = datetime(2020, 6, 26, 9, 12, 48, tzinfo=timezone.utc)
HOUR, DAY = timedelta(hours=1), timedelta(hours=24)
NOW = datetime(2026, 11, 27, 20, 0, tzinfo=timezone.utc)

# The tables as master's models.py created them (its CreateTable output).
MASTER_DDL = """
CREATE TABLE disaster_types (
    id SERIAL NOT NULL, name VARCHAR(255) NOT NULL, PRIMARY KEY (id));
CREATE TABLE roads (
    id SERIAL NOT NULL, geom geometry(LINESTRING,4326) NOT NULL,
    end1 geometry(POINT,4326) NOT NULL, end2 geometry(POINT,4326) NOT NULL, PRIMARY KEY (id));
CREATE TABLE disasters (
    id SERIAL NOT NULL, name VARCHAR(255) NOT NULL, creation_time TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    disaster_type INTEGER NOT NULL, PRIMARY KEY (id),
    FOREIGN KEY(disaster_type) REFERENCES disaster_types (id));
CREATE TABLE road_end1_connections (
    road_id INTEGER NOT NULL, connected_road_id INTEGER NOT NULL, PRIMARY KEY (road_id, connected_road_id),
    FOREIGN KEY(road_id) REFERENCES roads (id), FOREIGN KEY(connected_road_id) REFERENCES roads (id));
CREATE TABLE road_end2_connections (
    road_id INTEGER NOT NULL, connected_road_id INTEGER NOT NULL, PRIMARY KEY (road_id, connected_road_id),
    FOREIGN KEY(road_id) REFERENCES roads (id), FOREIGN KEY(connected_road_id) REFERENCES roads (id));
CREATE TABLE disaster_areas (
    id SERIAL NOT NULL, name VARCHAR(255) NOT NULL, disaster_id INTEGER NOT NULL,
    area geometry(POLYGON,4326) NOT NULL, PRIMARY KEY (id),
    FOREIGN KEY(disaster_id) REFERENCES disasters (id));
"""


@pytest.fixture
def blank_engine(scratch_database):
    with scratch_database() as engine:
        yield engine


@pytest.fixture(scope="module")
def _initialised_engine(scratch_database):
    with scratch_database() as engine:
        database.init_db(engine)
        yield engine


@pytest.fixture
def engine(_initialised_engine):
    """The module's scratch database, emptied after each test."""
    yield _initialised_engine
    with _initialised_engine.begin() as conn:
        conn.execute(text("TRUNCATE disasters, disaster_types, roads RESTART IDENTITY CASCADE"))


def _package(flood, not_analysed, extent=box(0, 0, 10, 10), observed_at=T0, product="TEST_PRODUCT_v1"):
    def record(kind, geom, i):
        return fl.FloodRecord(name=f"{fl.EMSR_ID} {fl.AOI_ID} {kind} #{i}", geometry=geom, kind=kind,
                              source="CEMS TEST: SPOT-6-7 (6 m, Post-event)", observed_at=observed_at,
                              emsr_obj_id=f"test:{i}", part=0, product=product)
    return ([record(fl.KIND_OBSERVED_FLOOD, g, i) for i, g in enumerate(flood)]
            + [record(fl.KIND_NOT_ANALYSED, g, i) for i, g in enumerate(not_analysed)]
            + ([record(fl.KIND_ANALYSIS_EXTENT, extent, 0)] if extent is not None else []))


FLOOD = [box(i, 0, i + 0.5, 0.5) for i in range(3)]
NOT_ANALYSED = [box(6, 6, 8, 8)]


def _counts(engine) -> dict[str, int]:
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT kind, count(*) FROM disaster_areas GROUP BY kind "
            "UNION ALL SELECT kind, count(*) FROM analysis_coverage GROUP BY kind"))
        return dict(rows.all())


def _write(engine, records, **kwargs):
    with Session(engine) as session:
        return fl.write_to_db(session, records, **kwargs)


def _valid(engine, at, **kwargs) -> tuple[int, int, int]:
    """(flood, analysis_extent, not_analysed) rows that count at ``at``."""
    with Session(engine) as session:
        found = fl.observations_valid_at(session, at, **kwargs)
        return len(found.flood), len(found.analysis_extent), len(found.not_analysed)


def _rows(engine, model) -> list[tuple]:
    with Session(engine) as session:
        return session.execute(select(model.id, model.product, model.observed_at, model.valid_until,
                                      model.ingested_at).order_by(model.id)).all()


# --- schema upgrade -------------------------------------------------------------

def test_init_db_upgrades_a_master_database_in_place(blank_engine):
    with blank_engine.begin() as conn:
        conn.execute(text(MASTER_DDL))
        conn.execute(text(
            "INSERT INTO disaster_types (name) VALUES ('disaster');"
            "INSERT INTO disasters (name, creation_time, disaster_type) VALUES ('old', now(), 1);"
            "INSERT INTO disaster_areas (name, disaster_id, area) VALUES "
            "('manual', 1, ST_GeomFromText('POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))', 4326));"
            "INSERT INTO roads (geom, end1, end2) VALUES (ST_GeomFromText('LINESTRING(0 0, 2 2)', 4326),"
            " ST_SetSRID(ST_MakePoint(0, 0), 4326), ST_SetSRID(ST_MakePoint(2, 2), 4326));"))

    database.init_db(blank_engine)
    first = {t: [c["name"] for c in inspect(blank_engine).get_columns(t)] for t in database.ADDED_COLUMNS}
    database.init_db(blank_engine)  # second run changes nothing
    insp = inspect(blank_engine)
    assert {t: [c["name"] for c in insp.get_columns(t)] for t in database.ADDED_COLUMNS} == first
    for table, columns in database.ADDED_COLUMNS.items():
        assert set(columns) <= set(first[table])
    for table in database.ADDED_INDEXES:
        assert f"ix_{table}_connected_road_id" in {i["name"] for i in insp.get_indexes(table)}

    with blank_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM disaster_areas")).scalar() == 1  # rows kept
        assert conn.execute(text("SELECT valid_until FROM disaster_areas")).scalar() is None
    assert _valid(blank_engine, T0 + HOUR) == (0, 0, 0)  # the old row has no time: never current
    _write(blank_engine, _package(FLOOD, NOT_ANALYSED), now=NOW)  # the upgraded schema takes an import
    assert _valid(blank_engine, T0 + HOUR) == (3, 1, 1)


# --- append-only history ------------------------------------------------------------

def test_a_newer_product_is_appended_and_the_older_one_kept(engine):
    first = _write(engine, _package(FLOOD, NOT_ANALYSED), now=NOW)
    before = {model: _rows(engine, model) for model in (DisasterArea, AnalysisCoverage)}
    assert all(ingested == NOW and valid == T0 + DAY
               for rows in before.values() for _, _, _, valid, ingested in rows)

    later = NOW + HOUR
    second = _write(engine, _package(FLOOD[:1], [], observed_at=T0 + 6 * HOUR, product="TEST_PRODUCT_v2"),
                    now=later)  # the water receded; nothing was left unanalysed
    assert first.disaster_id == second.disaster_id
    assert second.inserted == {fl.KIND_OBSERVED_FLOOD: 1, fl.KIND_ANALYSIS_EXTENT: 1, fl.KIND_NOT_ANALYSED: 0}
    assert _counts(engine) == {"observed_flood": 4, "analysis_extent": 2, "not_analysed": 1}
    for model, rows in before.items():  # earlier rows untouched
        assert _rows(engine, model)[:len(rows)] == rows

    assert _valid(engine, T0 + HOUR) == (3, 1, 1)  # only the first product had been observed
    assert _valid(engine, T0 + 7 * HOUR) == (4, 2, 1)  # both count: its flood and its gap stay visible
    assert _valid(engine, T0 + 25 * HOUR) == (1, 1, 0)  # the first expired at T0 + 24 h
    assert _valid(engine, T0 + 30 * HOUR) == (0, 0, 0)  # both expired: everything unknown
    with Session(engine) as session:  # the disaster keeps the time of its first observation
        assert session.scalar(select(Disaster.creation_time)) == T0.replace(tzinfo=None)


def test_the_same_product_twice_is_stored_once(engine):
    first = _write(engine, _package(FLOOD, NOT_ANALYSED), now=NOW)
    before = _counts(engine)
    again = _write(engine, _package(FLOOD, NOT_ANALYSED), now=NOW + HOUR)
    assert again.nothing_new and again.already_stored == first.inserted
    assert _counts(engine) == before
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(DISTINCT ingested_at) FROM disaster_areas")).scalar() == 1


# --- observations valid at T ---------------------------------------------------------

def test_observations_count_from_the_image_time_until_valid_until(engine):
    result = _write(engine, _package(FLOOD, NOT_ANALYSED))  # default validity: 24 h
    assert _valid(engine, T0 - timedelta(seconds=1)) == (0, 0, 0)  # not observed yet
    assert _valid(engine, T0) == (3, 1, 1)
    assert _valid(engine, T0 + DAY - timedelta(seconds=1)) == (3, 1, 1)
    assert _valid(engine, T0 + DAY) == (0, 0, 0)  # at valid_until it is stale
    assert _valid(engine, datetime.now(timezone.utc)) == (0, 0, 0)
    assert _valid(engine, T0 + HOUR, disaster_id=result.disaster_id) == (3, 1, 1)
    assert _valid(engine, T0 + HOUR, disaster_id=result.disaster_id + 1) == (0, 0, 0)


def test_road_statuses_use_full_geometry_and_observation_validity(engine):
    imported = _write(engine, _package(FLOOD, NOT_ANALYSED))
    with Session(engine) as session:
        def road(x1, y, x2):
            return Road(geom=WKTElement(f"LINESTRING({x1} {y},{x2} {y})", srid=4326),
                        end1=WKTElement(f"POINT({x1} {y})", srid=4326),
                        end2=WKTElement(f"POINT({x2} {y})", srid=4326))

        wet, mapped, outside, touches_gap, partial, untimed = [
            road(0.1, 0.25, 0.4), road(1, 2, 2), road(11, 2, 12),
            road(5, 7, 6), road(9, 4, 11), road(3.25, 2, 3.75),
        ]
        session.add_all([wet, mapped, outside, touches_gap, partial, untimed])
        session.add(DisasterArea(disaster_id=imported.disaster_id, name="Manual report",
                                 area=WKTElement(box(3, 1, 4, 3).wkt, srid=4326)))
        session.flush()

        at = datetime(2020, 6, 27, tzinfo=timezone.utc)
        gap = SegmentStatus("unknown", "gap")
        expired = SegmentStatus("unknown", "expired")
        assert road_statuses(session, at) == {
            wet.id: SegmentStatus("closed"), mapped.id: SegmentStatus("mapped_option"),
            outside.id: gap, touches_gap.id: gap, partial.id: gap,
            untimed.id: SegmentStatus("closed", "untimed_report"),
        }
        assert road_statuses(session, T0 - HOUR, road_ids={mapped.id}) == {mapped.id: gap}
        assert road_statuses(session, T0 + DAY, bbox=(0, 0, 2, 3)) == {
            wet.id: expired, mapped.id: expired,
        }


def test_unknown_observation_time_is_never_current(engine):
    _write(engine, _package(FLOOD, NOT_ANALYSED, observed_at=None), now=NOW)
    assert _counts(engine) == {"observed_flood": 3, "analysis_extent": 1, "not_analysed": 1}
    for at in (T0, NOW - HOUR, NOW, NOW + HOUR):
        assert _valid(engine, at) == (0, 0, 0)
    with Session(engine) as session:
        assert session.scalar(select(DisasterArea.valid_until).limit(1)) is None
        assert session.scalar(select(Disaster.creation_time)) == NOW.replace(tzinfo=None)  # never NULL


# --- guards and transaction ---------------------------------------------------------

def test_a_package_without_flood_is_refused_and_nothing_is_written(engine):
    with pytest.raises(fl.ImportRefused):
        _write(engine, _package([], NOT_ANALYSED))
    assert _counts(engine) == {}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM disasters")).scalar() == 0


def test_failed_insert_rolls_back_the_whole_package(engine):
    _write(engine, _package(FLOOD, NOT_ANALYSED))
    before = _counts(engine)
    broken = _package(FLOOD, NOT_ANALYSED, product="TEST_PRODUCT_v2")
    broken[-1].geometry = LineString([(0, 0), (1, 1)])  # the extent's MULTIPOLYGON column rejects it
    with pytest.raises(Exception, match="(?i)geometry"):
        _write(engine, broken)
    assert _counts(engine) == before


# --- real data ------------------------------------------------------------------------

@pytest.mark.skipif(not fl.DEFAULT_ZIP.exists(), reason="EMSR444 AOI02 zip not available (set EMSR_ZIP)")
def test_real_package_replays_at_its_image_time(engine):
    first = _write(engine, fl.read_package(fl.DEFAULT_ZIP), now=NOW)
    assert first.inserted == {fl.KIND_OBSERVED_FLOOD: 349, fl.KIND_ANALYSIS_EXTENT: 1, fl.KIND_NOT_ANALYSED: 92}
    assert _write(engine, fl.read_package(fl.DEFAULT_ZIP), now=NOW + HOUR).nothing_new
    assert _valid(engine, datetime(2020, 6, 26, 12, 0, tzinfo=timezone.utc)) == (349, 1, 92)
    assert _valid(engine, datetime.now(timezone.utc)) == (0, 0, 0)
