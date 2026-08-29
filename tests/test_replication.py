from pathlib import Path
import tempfile
import unittest

import pandas as pd

from icr_analysis.replication import (
    ReplicationConfig,
    build_replication_payload,
    run_replication,
)


class ReplicationEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.b_path = root / "b.csv"
        self.x_path = root / "x.csv"
        pd.DataFrame({
            "siteid": ["B1", "B2", "B3"],
            "latitude": [12.97, 13.20, 13.35],
            "longitude": [77.59, 77.80, 77.95],
        }).to_csv(self.b_path, index=False)
        pd.DataFrame({
            "siteid": [f"X{i:02d}" for i in range(10)],
            "latitude": [12.97, 12.98, 12.99, 13.00, 13.01, 13.07, 13.08, 13.09, 13.10, 13.11],
            "longitude": [77.59, 77.60, 77.61, 77.62, 77.63, 77.69, 77.70, 77.71, 77.72, 77.73],
        }).to_csv(self.x_path, index=False)

    def tearDown(self):
        self.temp.cleanup()

    def test_density_preserving_target_and_zone_quotas_reconcile(self):
        result = run_replication(
            self.b_path,
            self.x_path,
            ReplicationConfig(retention_percent=70, overlap_mode="fixed", overlap_km=0),
        )
        self.assertEqual(result.target_count, 7)
        self.assertEqual(result.selected_count, 7)
        zone_targets = result.sites.groupby("zone_id")["zone_target_count"].first()
        self.assertEqual(int(zone_targets.sum()), result.target_count)
        self.assertTrue((result.sites.loc[result.sites.final_selected, "zone_pick_order"] > 0).all())

    def test_overlap_removal_and_same_zone_refill(self):
        result = run_replication(
            self.b_path,
            self.x_path,
            ReplicationConfig(
                retention_percent=50,
                overlap_mode="fixed",
                overlap_km=2.0,
                refill_within_zone=True,
            ),
        )
        self.assertGreater(result.overlap_removed_count, 0)
        selected = result.sites[result.sites.final_selected]
        self.assertTrue((selected["nearest_b_distance_km"] >= selected["overlap_threshold_km"]).all())
        self.assertFalse((result.sites["final_selected"] & result.sites["overlap_rejected"]).any())

    def test_map_payload_exactly_matches_final_selection(self):
        result = run_replication(
            self.b_path,
            self.x_path,
            ReplicationConfig(retention_percent=70, overlap_mode="fixed", overlap_km=0),
        )
        payload = build_replication_payload(result)
        mapped = {site["siteid"] for site in payload["sites"] if site["status"] == "Selected X footprint"}
        expected = set(result.sites.loc[result.sites.final_selected, "operator_x_siteid"])
        self.assertEqual(mapped, expected)
        self.assertEqual(payload["summary"]["final_selected_sites"], result.selected_count)

    def test_invalid_replication_configuration_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "retention_percent"):
            run_replication(self.b_path, self.x_path, ReplicationConfig(retention_percent=0))


if __name__ == "__main__":
    unittest.main()
