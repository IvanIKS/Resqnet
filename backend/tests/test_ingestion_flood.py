"""Tests for service.ingestion_flood. Run from backend/: python -m pytest tests -q"""

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import MultiPolygon, Polygon, box

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

from service import ingestion_flood as fl  # noqa: E402

WGS84 = "EPSG:4326"
SPOT = "CEMS EMSR444 AOI02: SPOT-6-7 (6 m, Post-event)"
T13 = datetime(2020, 6, 26, 9, 13, tzinfo=timezone.utc)
DAY = timedelta(hours=24)


def _sources():
    df = pd.DataFrame({
        "source_nam": ["SPOT-6-7", "Open Street Map", "GlobeLand30"],
        "src_date": ["26/06/2020", "Not Applicable", "2010"],
        "source_tm": ["T09:13:00Z", "Not Applicable", "Not Applicable"],
        "sensor_gsd": ["6 m", "Not Applicable", "Not Applicable"],
        "eventphase": ["Post-event", "Pre-event", "Pre-event"],
        "src_id": [2, 994, 986],
    })
    return fl.load_sources(df)


def _events(geoms, src_ids=None):
    n = len(geoms)
    return gpd.GeoDataFrame({
        "notation": ["Flooded area"] * n,
        "dmg_src_id": src_ids or [2] * n,
    }, geometry=geoms, crs=WGS84)


# --- observed_at --------------------------------------------------------------

def test_observed_at_combines_date_and_time_as_utc():
    dt, has_time = fl.parse_observed_at("26/06/2020", "T09:13:00Z")
    assert dt == datetime(2020, 6, 26, 9, 13, 0, tzinfo=timezone.utc)
    assert has_time


def test_observed_at_day_first_and_missing_time():
    dt, has_time = fl.parse_observed_at("03/04/2020", "Not Applicable")
    assert dt == datetime(2020, 4, 3, tzinfo=timezone.utc)  # 3 April, not 4 March
    assert not has_time


@pytest.mark.parametrize("date", ["Not Applicable", "2010", None, ""])
def test_observed_at_unknown_date(date):
    assert fl.parse_observed_at(date, "T09:00:00Z") == (None, False)


@pytest.mark.parametrize("date", ["31/02/2020", "00/06/2020", "26/13/2020"])
def test_observed_at_impossible_date_is_unknown_not_an_error(date, caplog):
    with caplog.at_level(logging.WARNING, logger="resqnet.ingestion_flood"):
        assert fl.parse_observed_at(date, "T09:00:00Z") == (None, False)
    assert date in caplog.text


@pytest.mark.parametrize("time", ["T25:00:00Z", "T09:61:00Z", "noon"])
def test_observed_at_unusable_time_keeps_the_date_at_midnight(time, caplog):
    with caplog.at_level(logging.WARNING, logger="resqnet.ingestion_flood"):
        dt, has_time = fl.parse_observed_at("26/06/2020", time)
    assert (dt, has_time) == (datetime(2020, 6, 26, tzinfo=timezone.utc), False)
    assert time in caplog.text


def test_impossible_source_date_does_not_abort_load_sources():
    df = pd.DataFrame({"source_nam": ["SPOT-6-7"], "src_date": ["31/02/2020"], "source_tm": ["T09:13:00Z"],
                       "sensor_gsd": ["6 m"], "eventphase": ["Post-event"], "src_id": [2]})
    assert fl.load_sources(df)[2].observed_at is None


def test_flood_record_gets_time_and_source_from_join():
    recs = fl.build_flood_records(_events([box(0, 0, 1, 1)]), _sources())
    assert recs[0].observed_at == T13
    assert recs[0].source == SPOT  # provider first, then the image
    assert "2020-06-26T09:13Z" in recs[0].name


def test_source_without_a_known_image_still_names_the_provider():
    (rec,) = fl.build_flood_records(_events([box(0, 0, 1, 1)], src_ids=[12345]), _sources())
    assert (rec.source, rec.observed_at) == ("CEMS EMSR444 AOI02", None)


# --- geometry -----------------------------------------------------------------

def test_multipolygon_is_exploded_with_part_index():
    multi = MultiPolygon([box(0, 0, 1, 1), box(2, 2, 3, 3)])
    recs = fl.build_flood_records(_events([multi, box(5, 5, 6, 6)]), _sources())
    assert len(recs) == 3
    assert all(isinstance(r.geometry, Polygon) for r in recs)
    assert [(r.emsr_obj_id, r.part) for r in recs] == [
        ("observedEventA:0", 0), ("observedEventA:0", 1), ("observedEventA:1", 0)]
    assert recs[1].name.endswith("#0.1")


def test_invalid_geometry_is_fixed():
    bowtie = Polygon([(0, 0), (2, 2), (2, 0), (0, 2)])  # self-intersecting
    assert not bowtie.is_valid
    recs = fl.build_flood_records(_events([bowtie]), _sources())
    assert len(recs) == 2  # make_valid splits the bow-tie into two triangles
    assert all(r.geometry.is_valid and isinstance(r.geometry, Polygon) for r in recs)
    assert sum(r.geometry.area for r in recs) == pytest.approx(2.0)


def test_empty_geometry_is_dropped():
    recs = fl.build_flood_records(_events([Polygon(), box(0, 0, 1, 1)]), _sources())
    assert len(recs) == 1


def test_reprojects_to_wgs84():
    gdf = _events([box(0, 0, 1, 1)]).to_crs("EPSG:3857")
    recs = fl.build_flood_records(gdf, _sources())
    assert recs[0].geometry.bounds == pytest.approx((0, 0, 1, 1), abs=1e-9)


# --- Not Analysed mask --------------------------------------------------------

def _footprints(obj_types=("Image Footprint", "Not Analysed", "Not Analysed")):
    return gpd.GeoDataFrame({
        "obj_type": list(obj_types),
        "or_src_id": [2] * len(obj_types),
    }, geometry=[box(0, 0, 10, 10), box(0, 0, 1, 1), box(1, 0, 2, 1)][:len(obj_types)], crs=WGS84)


def test_not_analysed_rows_go_to_mask_only():
    recs = fl.build_not_analysed_records(_footprints(), _sources())
    assert len(recs) == 2  # the scene footprint row is not a mask
    assert {r.kind for r in recs} == {fl.KIND_NOT_ANALYSED}
    assert fl.not_analysed_mask(recs).area == pytest.approx(2.0)


@pytest.mark.parametrize("odd", ["Not analyzed", "Cloud", None])
def test_unknown_footprint_type_is_refused(odd):
    # Skipping it could hide a "not analysed" area under another name.
    with pytest.raises(fl.ImportRefused, match="obj_type"):
        fl.build_not_analysed_records(_footprints(("Image Footprint", "Not Analysed", odd)), _sources())


# --- analysis extent (areaOfInterestA) ------------------------------------------

def _aoi(geoms=None):
    return gpd.GeoDataFrame({"emsr_id": ["EMSR444"], "area_id": ["02"]},
                            geometry=geoms or [box(0, 0, 10, 10)], crs=WGS84)


def test_extent_record_gets_the_earliest_post_event_time():
    sources = _sources()
    sources[3] = fl.SourceInfo(3, "SPOT-6-7", datetime(2020, 6, 26, 9, 12, 48, tzinfo=timezone.utc),
                               True, "6 m", "Post-event")
    (rec,) = fl.build_extent_records(_aoi(), sources)
    assert rec.kind == fl.KIND_ANALYSIS_EXTENT
    assert rec.observed_at == datetime(2020, 6, 26, 9, 12, 48, tzinfo=timezone.utc)
    assert rec.source == SPOT  # pre-event sources are not the analysis
    assert rec.geometry.equals(box(0, 0, 10, 10))


def test_extent_time_is_unknown_if_any_post_event_image_has_no_date():
    sources = _sources()
    sources[3] = fl.SourceInfo(3, "SPOT-6-7", None, False, "6 m", "Post-event")
    (rec,) = fl.build_extent_records(_aoi(), sources)
    assert rec.observed_at is None
    assert "time-unknown" in rec.name


def test_multipart_extent_stays_one_record():
    multi = MultiPolygon([box(0, 0, 1, 1), box(2, 2, 3, 3)])
    (rec,) = fl.build_extent_records(_aoi([multi]), _sources())
    assert isinstance(rec.geometry, MultiPolygon) and rec.geometry.area == pytest.approx(2.0)


# --- validity (valid_until) -------------------------------------------------------

def _package(sources=None, product="EMSR444_AOI02_DEL_PRODUCT_r1_RTP01_v1"):
    sources = sources or _sources()
    records = (fl.build_flood_records(_events([box(0, 0, 1, 1)]), sources)
               + fl.build_not_analysed_records(_footprints(), sources)
               + fl.build_extent_records(_aoi(), sources))
    for r in records:
        r.product = product
    return records


def _by_kind(records, kind):
    return [r for r in records if r.kind == kind]


def test_valid_until_is_24_hours_after_the_image_by_default(monkeypatch):
    monkeypatch.delenv(fl.MAX_AGE_ENV, raising=False)
    records = _package()
    fl.assign_validity(records, fl.max_age_from_env())
    assert {r.valid_until for r in records} == {T13 + DAY}


def test_max_age_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv(fl.MAX_AGE_ENV, "6")
    assert fl.max_age_from_env() == timedelta(hours=6)
    for bad in ("25", "0", "-1", "a day"):
        monkeypatch.setenv(fl.MAX_AGE_ENV, bad)
        with pytest.raises(ValueError):
            fl.max_age_from_env()


@pytest.mark.parametrize("max_age", [timedelta(0), timedelta(hours=-1), DAY + timedelta(seconds=1)])
def test_max_age_outside_the_contract_is_refused(max_age):
    with pytest.raises(ValueError, match="at most"):
        fl.assign_validity(_package(), max_age)


def test_unknown_observation_time_is_never_current():
    no_dates = {k: fl.SourceInfo(s.src_id, s.name, None, False, s.sensor_gsd, s.event_phase)
                for k, s in _sources().items()}
    records = _package(no_dates)
    fl.assign_validity(records, DAY)
    assert all(r.observed_at is None and r.valid_until is None for r in records)


def test_extent_never_outlives_the_flood_and_not_analysed_layers():
    records = _package()
    flood = _by_kind(records, fl.KIND_OBSERVED_FLOOD)[0]
    flood.observed_at = T13 - timedelta(hours=2)  # an older image in the same package
    fl.assign_validity(records, DAY)
    (extent,) = _by_kind(records, fl.KIND_ANALYSIS_EXTENT)
    assert flood.valid_until == extent.valid_until == T13 + timedelta(hours=22)

    flood.observed_at = None  # a flood polygon without a time: the "dry" claim has no end we can trust
    fl.assign_validity(records, DAY)
    assert extent.valid_until is None
    assert {r.valid_until for r in _by_kind(records, fl.KIND_NOT_ANALYSED)} == {T13 + DAY}


# --- write_to_db (fake session) -------------------------------------------------

class FakeSession:
    """Records every call. scalar() answers None (no disaster yet); execute()
    answers ``stored``: the (source, product, observed_at) already in the database."""

    def __init__(self, stored=(), fail_on_commit=False):
        self.added, self.calls, self.next_id = [], [], 1
        self.stored = list(stored)
        self.fail_on_commit = fail_on_commit

    def scalar(self, _stmt):
        self.calls.append("scalar")
        return None

    def add(self, obj):
        self.calls.append("add")
        self.added.append(obj)

    def flush(self):
        self.calls.append("flush")
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id, self.next_id = self.next_id, self.next_id + 1

    def execute(self, stmt, params=None):
        self.calls.append("delete" if getattr(stmt, "is_delete", False)
                          else "update" if getattr(stmt, "is_update", False) else "execute")
        return list(self.stored)

    def commit(self):
        self.calls.append("commit")
        if self.fail_on_commit:
            raise RuntimeError("database went away")

    def rollback(self):
        self.calls.append("rollback")


NOW = datetime(2026, 11, 27, 20, 0, tzinfo=timezone.utc)


def test_write_to_db_keeps_not_analysed_out_of_disaster_areas():
    from models import AnalysisCoverage, DisasterArea

    session = FakeSession()
    result = fl.write_to_db(session, _package())
    assert result.inserted == {fl.KIND_OBSERVED_FLOOD: 1, fl.KIND_ANALYSIS_EXTENT: 1, fl.KIND_NOT_ANALYSED: 2}
    assert not result.nothing_new and result.disaster_id is not None
    areas = [o for o in session.added if isinstance(o, DisasterArea)]
    assert len(areas) == 1
    assert "observed flood" in areas[0].name and "not analysed" not in areas[0].name
    coverage = [o for o in session.added if isinstance(o, AnalysisCoverage)]
    assert sorted(c.kind for c in coverage) == ["analysis_extent", "not_analysed", "not_analysed"]
    assert session.calls[-1] == "commit"


def test_write_to_db_stores_source_product_and_times():
    from geoalchemy2.shape import to_shape

    from models import AnalysisCoverage, DisasterArea

    session = FakeSession()
    fl.write_to_db(session, _package(), max_age=timedelta(hours=12), now=NOW)
    (area,) = [o for o in session.added if isinstance(o, DisasterArea)]
    assert (area.kind, area.source, area.product) == (
        "observed_flood", SPOT, "EMSR444_AOI02_DEL_PRODUCT_r1_RTP01_v1")
    assert (area.observed_at, area.valid_until, area.ingested_at) == (T13, T13 + timedelta(hours=12), NOW)
    for cov in (o for o in session.added if isinstance(o, AnalysisCoverage)):
        assert (cov.product, cov.valid_until, cov.ingested_at) == (area.product, area.valid_until, NOW)
        assert to_shape(cov.area).geom_type == "MultiPolygon"  # the column type


def test_write_to_db_only_appends():
    session = FakeSession()
    fl.write_to_db(session, _package())
    assert "delete" not in session.calls and "update" not in session.calls


def test_same_product_again_writes_nothing():
    records = _package()
    stored = {(r.source, r.product, r.observed_at) for r in records}
    session = FakeSession(stored=stored)
    result = fl.write_to_db(session, records)
    assert result.nothing_new and result.disaster_id is None
    assert result.already_stored == {fl.KIND_OBSERVED_FLOOD: 1, fl.KIND_ANALYSIS_EXTENT: 1, fl.KIND_NOT_ANALYSED: 2}
    assert session.added == [] and session.calls[-1] == "rollback"


def test_only_the_new_observations_of_a_package_are_appended():
    records = _package()
    later = _by_kind(records, fl.KIND_OBSERVED_FLOOD)[0]
    stored = {(r.source, r.product, r.observed_at) for r in records}
    later.observed_at = T13 + timedelta(hours=1)  # not stored yet under this time
    result = fl.write_to_db(FakeSession(stored=stored), records)
    assert result.inserted == {fl.KIND_OBSERVED_FLOOD: 1, fl.KIND_ANALYSIS_EXTENT: 0, fl.KIND_NOT_ANALYSED: 0}


def test_write_to_db_refuses_a_package_without_flood_and_touches_nothing():
    sources = _sources()
    session = FakeSession()
    no_flood = fl.build_not_analysed_records(_footprints(), sources) + fl.build_extent_records(_aoi(), sources)
    for r in no_flood:
        r.product = "P"
    with pytest.raises(fl.ImportRefused, match="observed dry"):
        fl.write_to_db(session, no_flood)  # the extent would read as "observed: no water"
    with pytest.raises(fl.ImportRefused):
        fl.write_to_db(session, [])
    assert session.calls == []


def test_write_to_db_needs_a_product_id_and_an_aware_now():
    records = _package(product=None)
    with pytest.raises(ValueError, match="product"):
        fl.write_to_db(FakeSession(), records)
    with pytest.raises(ValueError, match="timezone"):
        fl.write_to_db(FakeSession(), _package(), now=datetime(2026, 11, 27, 20, 0))


def test_write_to_db_rolls_back_on_error():
    session = FakeSession(fail_on_commit=True)
    with pytest.raises(RuntimeError, match="went away"):
        fl.write_to_db(session, _package())
    assert session.calls[-2:] == ["commit", "rollback"]


def test_creation_time_falls_back_to_import_time_without_observation_date():
    from models import Disaster

    no_dates = {k: fl.SourceInfo(s.src_id, s.name, None, False, s.sensor_gsd, s.event_phase)
                for k, s in _sources().items()}
    records = _package(no_dates)
    assert all(r.observed_at is None for r in records)
    session = FakeSession()
    fl.write_to_db(session, records, now=NOW)
    (disaster,) = [o for o in session.added if isinstance(o, Disaster)]
    assert disaster.creation_time == datetime(2026, 11, 27, 20, 0)  # naive UTC, never NULL
    assert all(o.observed_at is None and o.valid_until is None for o in session.added if hasattr(o, "valid_until"))


def test_creation_time_is_the_earliest_observation_when_known():
    from models import Disaster

    session = FakeSession()
    fl.write_to_db(session, _package(), now=NOW)
    (disaster,) = [o for o in session.added if isinstance(o, Disaster)]
    assert disaster.creation_time == datetime(2020, 6, 26, 9, 13)


def test_one_unknown_time_makes_the_package_time_unknown():
    records = _package()
    assert fl.earliest_observation(records) == T13
    records[0].observed_at = None
    assert fl.earliest_observation(records) is None
    assert fl.earliest_observation([]) is None


def test_valid_at_is_half_open_and_needs_an_aware_moment():
    from models import DisasterArea

    sql = str(fl.valid_at(DisasterArea, NOW).compile())
    assert "observed_at <=" in sql and "valid_until >" in sql
    with pytest.raises(ValueError, match="timezone"):
        fl.observations_valid_at(FakeSession(), datetime(2020, 6, 26, 12, 0))


# --- paths and constants --------------------------------------------------------

def test_missing_zip_is_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="EMSR_ZIP"):
        fl.read_package(tmp_path / "missing.zip")


def test_product_id_from_zip_name():
    assert (fl.product_id(Path("x/EMSR444_AOI02_DEL_PRODUCT_r1_RTP01_v1_vector.zip"))
            == "EMSR444_AOI02_DEL_PRODUCT_r1_RTP01_v1")


def test_checkout_root_locally_and_in_docker():
    from pathlib import PurePosixPath, PureWindowsPath

    assert fl.checkout_root(PureWindowsPath("D:/cassini-review/Resqnet/backend")) == PureWindowsPath(
        "D:/cassini-review/Resqnet")
    assert fl.checkout_root(PurePosixPath("/app")) == PurePosixPath("/app")  # not "/": exports stay possible
    assert (fl.REPO_ROOT / "backend").is_dir()


def test_exports_never_land_inside_the_checkout():
    with pytest.raises(ValueError, match="inside the repo"):
        fl.export_geojson([], fl.BACKEND_DIR / "out")


def test_kinds_match_the_models():
    import models

    assert fl.KIND_OBSERVED_FLOOD == models.AREA_KIND_OBSERVED_FLOOD
    assert fl.KIND_ANALYSIS_EXTENT == models.COVERAGE_KIND_ANALYSIS_EXTENT
    assert fl.KIND_NOT_ANALYSED == models.COVERAGE_KIND_NOT_ANALYSED


# --- real data ----------------------------------------------------------------

@pytest.mark.skipif(not fl.DEFAULT_ZIP.exists(), reason="EMSR444 AOI02 zip not available (set EMSR_ZIP)")
def test_real_emsr444_aoi02_package():
    records = fl.read_package(fl.DEFAULT_ZIP)
    flood = _by_kind(records, fl.KIND_OBSERVED_FLOOD)
    na = _by_kind(records, fl.KIND_NOT_ANALYSED)
    extent = _by_kind(records, fl.KIND_ANALYSIS_EXTENT)
    assert len({r.emsr_obj_id for r in flood}) == 346  # features in observedEventA
    assert len(flood) >= 346 and len(na) == 92 and len(extent) == 1
    assert all(isinstance(r.geometry, Polygon) and r.geometry.is_valid for r in records)
    assert {r.observed_at.date().isoformat() for r in flood} == {"2020-06-26"}
    assert {r.source for r in records} == {SPOT}
    t0 = datetime(2020, 6, 26, 9, 12, 48, tzinfo=timezone.utc)
    assert extent[0].observed_at == t0
    assert {r.product for r in records} == {"EMSR444_AOI02_DEL_PRODUCT_r1_RTP01_v1"}
    fl.assign_validity(records, DAY)
    assert extent[0].valid_until == t0 + DAY  # the earliest image ends the "observed dry" claim
    assert {r.valid_until for r in flood + na} == {t0 + DAY, T13 + DAY}
    s = fl.summarize(records)
    assert s["flood_area_km2"] == pytest.approx(75.4, abs=0.5)
    assert s["flood_inside_mask_km2"] == pytest.approx(0.0, abs=0.01)
    assert s["analysis_extent_km2"] == pytest.approx(1160.6, abs=1.0)
