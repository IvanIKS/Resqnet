from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from api.schemas import EvacuationRouteIn
from models import EvacuationPoint
from services import evacuation
from services.road_status import SegmentStatus, road_statuses

REPLAY = datetime(2020, 6, 27, tzinfo=timezone.utc)
MAPPED = SegmentStatus("mapped_option")
CLOSED = SegmentStatus("closed")
GAP = SegmentStatus("unknown", "gap")
EXPIRED = SegmentStatus("unknown", "expired")


def test_shortest_path_can_leave_closed_start_but_never_enter_closed_road():
    adjacency = {1: [2, 3], 2: [1, 4], 3: [1, 4], 4: [2, 3]}
    lengths = {1: 10.0, 2: 10.0, 3: 50.0, 4: 10.0}
    search = evacuation.find_shortest_road_path
    assert search(1, {4}, adjacency, lengths, {1, 2, 3, 4}) == ([1, 2, 4], 30.0)
    assert search(1, {1, 4}, adjacency, lengths, {3, 4}) == ([1, 3, 4], 70.0)
    assert search(1, {4}, adjacency, lengths, {4}) is None


@pytest.fixture
def route_setup(monkeypatch):
    session = MagicMock()
    session.scalars.return_value = [EvacuationPoint(id=7, name="School", geom="target")]
    session.execute.return_value.all.return_value = [
        (road_id, '{"type":"LineString","coordinates":[[30.52,50.45],[30.53,50.46]]}')
        for road_id in (1, 2, 3, 4, 5, 6)
    ]
    monkeypatch.setattr(evacuation, "_is_in_disaster_area", MagicMock(return_value=False))
    monkeypatch.setattr(evacuation, "_find_nearest_road_id", MagicMock(side_effect=[1, 5]))
    adjacency = {1: [2, 3, 4], 2: [6], 6: [5], 3: [5], 4: [5], 5: []}
    lengths = {1: 1.0, 2: 2.0, 3: 8.0, 4: 1.0, 5: 1.0, 6: 2.0}
    monkeypatch.setattr(evacuation, "_load_road_graph", lambda _: (adjacency, lengths))
    statuses = {1: MAPPED, 2: GAP, 3: MAPPED, 4: CLOSED, 5: MAPPED, 6: EXPIRED}
    classify = MagicMock(return_value=statuses)
    monkeypatch.setattr(evacuation, "road_statuses", classify)
    return session, statuses, classify


def test_route_is_computed_outside_water_and_prefers_mapped_segments(route_setup):
    session, _, classify = route_setup
    result = evacuation.get_evacuation_route(session, lat=50.45, lon=30.52, at=REPLAY)
    classify.assert_called_once_with(session, REPLAY)
    assert result.status == "route_found" and not result.start_in_flood
    assert result.road_ids == [1, 3, 5] and result.distance_meters == 10.0
    assert result.route_status == "mapped_option" and result.reason is None
    assert result.limiting_segment_ids == [1, 3, 5]


def test_fallback_to_unknown_with_weakest_segments_and_closed_start(route_setup):
    session, statuses, _ = route_setup
    statuses[1] = CLOSED
    statuses[3] = CLOSED
    result = evacuation.get_evacuation_route(session, 50.45, 30.52, REPLAY)
    assert result.status == "route_found" and result.start_in_flood and result.in_disaster_area
    assert result.road_ids == [1, 2, 6, 5]
    assert result.route_status == "unknown" and result.reason == "expired"
    assert result.limiting_segment_ids == [2, 6]
    assert result.route["type"] == "FeatureCollection"
    assert [feature["properties"] for feature in result.route["features"]] == [
        {"id": 1, "route_status": "closed", "reason": None},
        {"id": 2, "route_status": "unknown", "reason": "gap"},
        {"id": 6, "route_status": "unknown", "reason": "expired"},
        {"id": 5, "route_status": "mapped_option", "reason": None},
    ]


def test_single_segment_route_uses_its_own_status(route_setup, monkeypatch):
    session, statuses, _ = route_setup
    statuses[1] = GAP
    monkeypatch.setattr(evacuation, "_find_nearest_road_id", lambda *args: 1)
    result = evacuation.get_evacuation_route(session, 50.45, 30.52, REPLAY)
    assert result.road_ids == result.limiting_segment_ids == [1]
    assert (result.route_status, result.reason) == ("unknown", "gap")


def test_unknown_start_still_prefers_mapped_and_rates_the_start(route_setup):
    session, statuses, _ = route_setup
    statuses[1] = GAP
    result = evacuation.get_evacuation_route(session, 50.45, 30.52, REPLAY)
    assert result.road_ids == [1, 3, 5] and not result.start_in_flood
    assert (result.route_status, result.reason) == ("unknown", "gap")
    assert result.limiting_segment_ids == [1]


def test_unknown_start_that_is_a_target_is_not_skipped_by_the_mapped_pass(route_setup, monkeypatch):
    session, statuses, _ = route_setup
    statuses[1] = GAP
    session.scalars.return_value = [EvacuationPoint(id=7, name="Here"), EvacuationPoint(id=8, name="Far")]
    monkeypatch.setattr(evacuation, "_find_nearest_road_id", MagicMock(side_effect=[1, 1, 5]))
    result = evacuation.get_evacuation_route(session, 50.45, 30.52, REPLAY)
    assert result.road_ids == [1] and result.target_point_id == 7
    assert (result.route_status, result.reason) == ("unknown", "gap")


def test_closed_target_has_no_route_and_uses_candidate_fallback(route_setup, monkeypatch):
    session, statuses, _ = route_setup
    statuses[5] = CLOSED
    monkeypatch.setattr(evacuation, "_find_nearest_evacuation_point", lambda *args: None)
    candidate = MagicMock(return_value=(50.46, 30.53))
    monkeypatch.setattr(evacuation, "_find_candidate_point", candidate)
    result = evacuation.get_evacuation_route(session, 50.45, 30.52, REPLAY)
    candidate.assert_called_once_with(session, "POINT(30.52 50.45)", REPLAY)
    assert result.status == "candidate_fallback"
    assert (result.candidate_lat, result.candidate_lon) == (50.46, 30.53)
    assert (result.route_status, result.reason, result.route) == ("unknown", "error", None)
    assert result.road_ids == result.limiting_segment_ids == []
    assert result.detail == "No route to an evacuation point."


def test_route_exception_returns_unknown_and_no_geometry(route_setup, monkeypatch):
    session, _, classify = route_setup
    classify.side_effect = RuntimeError("database computation failed")
    monkeypatch.setattr(evacuation, "_find_nearest_evacuation_point", lambda *args: EvacuationPoint(id=7))
    result = evacuation.get_evacuation_route(session, 50.45, 30.52, REPLAY)
    session.rollback.assert_called_once()
    assert result.status == "evacuation_point_fallback" and result.target_point_id == 7
    assert (result.route_status, result.reason, result.route) == ("unknown", "error", None)
    assert result.detail == "Route computation failed."


def test_route_api_rejects_naive_at(client):
    response = client.post("/evacuation-route", json={"lat": 50.45, "lon": 30.52, "at": "2020-06-27T00:00:00"})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "at"]


def test_at_accepts_offset_and_defaults_to_utc():
    assert EvacuationRouteIn(lat=0, lon=0, at="2020-06-27T03:00:00+03:00").at == REPLAY
    assert EvacuationRouteIn(lat=0, lon=0).at.tzinfo is timezone.utc
    with pytest.raises(ValueError, match="timezone-aware"):
        road_statuses(MagicMock(), REPLAY.replace(tzinfo=None))
