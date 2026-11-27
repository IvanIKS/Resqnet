"""Tests for service.ingestion_flood. Run from backend/: python -m pytest tests -q"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import MultiPolygon, Polygon, box

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

from service import ingestion_flood as fl  # noqa: E402

WGS84 = "EPSG:4326"


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


def test_flood_record_gets_time_and_source_from_join():
    recs = fl.build_flood_records(_events([box(0, 0, 1, 1)]), _sources())
    assert recs[0].observed_at == datetime(2020, 6, 26, 9, 13, tzinfo=timezone.utc)
    assert recs[0].source == "SPOT-6-7 (6 m, Post-event)"
    assert "2020-06-26T09:13Z" in recs[0].name


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

def _footprints():
    return gpd.GeoDataFrame({
        "obj_type": ["Image Footprint", "Not Analysed", "Not Analysed"],
        "or_src_id": [2, 2, 2],
    }, geometry=[box(0, 0, 10, 10), box(0, 0, 1, 1), box(1, 0, 2, 1)], crs=WGS84)


def test_not_analysed_rows_go_to_mask_only():
    recs = fl.build_not_analysed_records(_footprints(), _sources())
    assert len(recs) == 2  # the scene footprint row is not a mask
    assert {r.kind for r in recs} == {fl.KIND_NOT_ANALYSED}
    assert fl.not_analysed_mask(recs).area == pytest.approx(2.0)


def test_write_to_db_never_writes_not_analysed():
    from models import DisasterArea

    class FakeSession:
        def __init__(self):
            self.added, self.next_id = [], 1

        def scalar(self, _stmt):
            return None

        def add(self, obj):
            self.added.append(obj)

        def flush(self):
            for obj in self.added:
                if getattr(obj, "id", None) is None:
                    obj.id, self.next_id = self.next_id, self.next_id + 1

        def execute(self, _stmt):
            pass

        def commit(self):
            pass

    sources = _sources()
    records = (fl.build_flood_records(_events([box(0, 0, 1, 1)]), sources)
               + fl.build_not_analysed_records(_footprints(), sources))
    session = FakeSession()
    assert fl.write_to_db(session, records) == 1
    areas = [o for o in session.added if isinstance(o, DisasterArea)]
    assert len(areas) == 1
    assert "observed flood" in areas[0].name and "not analysed" not in areas[0].name


# --- real data ----------------------------------------------------------------

@pytest.mark.skipif(not fl.DEFAULT_ZIP.exists(), reason="EMSR444 AOI02 zip not available")
def test_real_emsr444_aoi02_package():
    records = fl.read_package(fl.DEFAULT_ZIP)
    flood = [r for r in records if r.kind == fl.KIND_OBSERVED_FLOOD]
    na = [r for r in records if r.kind == fl.KIND_NOT_ANALYSED]
    assert len({r.emsr_obj_id for r in flood}) == 346  # features in observedEventA
    assert len(flood) >= 346 and len(na) == 92
    assert all(isinstance(r.geometry, Polygon) and r.geometry.is_valid for r in records)
    assert {r.observed_at.date().isoformat() for r in flood} == {"2020-06-26"}
    s = fl.summarize(records)
    assert s["flood_area_km2"] == pytest.approx(75.4, abs=0.5)
    assert s["flood_inside_mask_km2"] == pytest.approx(0.0, abs=0.01)
