import unittest

import numpy as np

from icr_analysis.geo import SphericalIndex, haversine_km


class GeoTests(unittest.TestCase):
    def test_known_equatorial_distance(self):
        self.assertAlmostEqual(haversine_km(0, 0, 0, 1), 111.195, places=2)

    def test_index_returns_nearest_and_honors_exclusion(self):
        index = SphericalIndex(np.array([0.0, 0.0, 0.0]), np.array([0.0, 1.0, 3.0]))
        nearest = index.query(0.0, 0.2, k=2)
        self.assertEqual([item[0] for item in nearest], [0, 1])
        excluded = index.query(0.0, 0.0, k=1, exclude_index=0)
        self.assertEqual(excluded[0][0], 1)


if __name__ == "__main__":
    unittest.main()

