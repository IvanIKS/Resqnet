import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.evacuation import find_shortest_road_path, get_evacuation_route


class TestFindShortestRoadPath(unittest.TestCase):
    def test_finds_shortest_non_flooded_path(self):
        # 1 -> 2 -> 4 (cost 10+10+10=30) vs 1 -> 3 -> 4 (cost 10+50+10=70)
        adjacency = {1: [2, 3], 2: [1, 4], 3: [1, 4], 4: [2, 3]}
        lengths = {1: 10.0, 2: 10.0, 3: 50.0, 4: 10.0}
        result = find_shortest_road_path(
            start_road_id=1,
            target_road_ids={4},
            adjacency=adjacency,
            lengths=lengths,
            flooded_road_ids=set(),
        )
        self.assertEqual(result, ([1, 2, 4], 30.0))

    def test_allows_starting_on_flooded_road_but_avoids_other_flooded(self):
        # Road 1 is flooded (start), road 2 is flooded (blocked), road 3 is dry
        adjacency = {1: [2, 3], 2: [1, 4], 3: [1, 4], 4: [2, 3]}
        lengths = {1: 10.0, 2: 10.0, 3: 50.0, 4: 10.0}
        result = find_shortest_road_path(
            start_road_id=1,
            target_road_ids={4},
            adjacency=adjacency,
            lengths=lengths,
            flooded_road_ids={1, 2},
        )
        self.assertEqual(result, ([1, 3, 4], 70.0))

    def test_returns_none_when_all_paths_blocked(self):
        adjacency = {1: [2], 2: [1, 3], 3: [2]}
        lengths = {1: 10.0, 2: 10.0, 3: 10.0}
        result = find_shortest_road_path(
            start_road_id=1,
            target_road_ids={3},
            adjacency=adjacency,
            lengths=lengths,
            flooded_road_ids={2},
        )
        self.assertIsNone(result)


class TestGetEvacuationRoute(unittest.TestCase):
    @patch("services.evacuation._is_in_disaster_area", return_value=False)
    def test_safe_when_outside_disaster_area(self, _mock_in_area):
        session = MagicMock()
        res = get_evacuation_route(session, lat=50.45, lon=30.52)
        self.assertEqual(res.status, "safe")
        self.assertFalse(res.in_disaster_area)
        self.assertEqual(res.road_ids, [])

    @patch("services.evacuation._find_candidate_point", return_value=(50.46, 30.53))
    @patch("services.evacuation._find_nearest_evacuation_point", return_value=None)
    @patch("services.evacuation._get_flooded_road_ids", return_value={1, 2})
    @patch("services.evacuation._find_nearest_road_id", return_value=1)
    @patch("services.evacuation._is_in_disaster_area", return_value=True)
    def test_candidate_fallback_when_no_ep_and_no_path(
        self, _m1, _m2, _m3, _m4, _m5
    ):
        session = MagicMock()
        session.scalars.return_value.all.return_value = []
        res = get_evacuation_route(session, lat=50.45, lon=30.52)
        self.assertEqual(res.status, "candidate_fallback")
        self.assertTrue(res.in_disaster_area)
        self.assertEqual(res.candidate_lat, 50.46)
        self.assertEqual(res.candidate_lon, 30.53)


if __name__ == "__main__":
    unittest.main()
