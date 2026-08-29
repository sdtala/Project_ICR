from pathlib import Path
import tempfile
import unittest

import pandas as pd

from icr_analysis.dashboard import build_artifact, build_dashboard_payload
from icr_analysis.engine import AnalysisConfig, load_sites, run_analysis
from icr_analysis.geo import haversine_km


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.b_path = root / "b.csv"
        self.x_path = root / "x.csv"
        pd.DataFrame({
            "siteid": ["B1", "B2", "B3", "B4", "B5", "B6"],
            "latitude": [12.97, 12.98, 12.99, 13.00, 13.01, 13.02],
            "longitude": [77.59, 77.60, 77.61, 77.62, 77.63, 77.64],
        }).to_csv(self.b_path, index=False)
        pd.DataFrame({
            "siteid": ["X1", "X2", "X3", "X4", "X5"],
            "latitude": [12.971, 13.04, 13.08, 12.94, 13.12],
            "longitude": [77.591, 77.66, 77.70, 77.56, 77.74],
        }).to_csv(self.x_path, index=False)

    def tearDown(self):
        self.temp.cleanup()

    def test_fixed_count_returns_requested_portfolio(self):
        result = run_analysis(self.b_path, self.x_path, AnalysisConfig(mode="fixed_count", count=3, criterion="density"))
        self.assertEqual(result.selected_count, 3)
        self.assertEqual(result.ranked_sites.loc[result.ranked_sites.selected, "selection_rank"].tolist(), [1, 2, 3])
        self.assertTrue((result.ranked_sites["nearest_b_distance_km"] >= 0).all())
        self.assertGreater(result.timings["analysis_total_seconds"], 0)
        self.assertIn("spatial_scoring_seconds", result.timings)

    def test_optional_prefilter_can_return_fewer_than_count(self):
        result = run_analysis(
            self.b_path,
            self.x_path,
            AnalysisConfig(mode="fixed_count", count=5, criterion="absolute", prefilter="absolute", threshold=5.0),
        )
        self.assertLess(result.selected_count, 5)
        selected = result.ranked_sites[result.ranked_sites.selected]
        self.assertTrue((selected["nearest_b_distance_km"] >= 5.0).all())

    def test_batch_spatial_distances_match_direct_haversine(self):
        result = run_analysis(self.b_path, self.x_path, AnalysisConfig(mode="fixed_count", count=1))
        for candidate in result.ranked_sites.itertuples():
            expected = min(
                haversine_km(candidate.latitude, candidate.longitude, row.latitude, row.longitude)
                for row in result.valid_b_sites.itertuples()
            )
            self.assertAlmostEqual(candidate.nearest_b_distance_km, expected, places=5)

    def test_distance_threshold_selects_every_eligible_site(self):
        result = run_analysis(
            self.b_path,
            self.x_path,
            AnalysisConfig(mode="distance_threshold", criterion="density", threshold=1.0),
        )
        self.assertEqual(result.selected_count, int(result.ranked_sites.eligible.sum()))
        self.assertTrue(result.ranked_sites.loc[result.ranked_sites.selected, "selection_reason"].str.contains("in batch").all())

    def test_input_validation_and_duplicate_exclusions(self):
        path = Path(self.temp.name) / "dirty.csv"
        pd.DataFrame({
            " SiteID ": ["A", "a", "C", "D", "E", ""],
            " LATITUDE ": [10, 11, 10, 91, "bad", 12],
            " LONGITUDE ": [20, 21, 20, 22, 23, 24],
        }).to_csv(path, index=False)
        valid, issues = load_sites(path, "B")
        self.assertEqual(valid["siteid"].tolist(), ["A"])
        self.assertEqual(set(issues.issue), {"duplicate_siteid", "duplicate_coordinate", "latitude_out_of_range", "invalid_coordinate", "missing_siteid"})

    def test_invalid_configuration_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "requires count"):
            run_analysis(self.b_path, self.x_path, AnalysisConfig(mode="fixed_count"))
        with self.assertRaisesRegex(ValueError, "requires threshold"):
            run_analysis(self.b_path, self.x_path, AnalysisConfig(mode="distance_threshold"))

    def test_dashboard_artifact_reconciles_with_result(self):
        result = run_analysis(self.b_path, self.x_path, AnalysisConfig(mode="fixed_count", count=3))
        artifact = build_artifact(result, "B Telecom", "X Telecom")
        summary = artifact["snapshot"]["datasets"]["summary"][0]
        self.assertEqual(artifact["surface"], "dashboard")
        self.assertEqual(summary["valid_b_sites"], len(result.valid_b_sites))
        self.assertEqual(summary["selected_sites"], result.selected_count)
        self.assertEqual(len(artifact["manifest"]["cards"]), 6)
        self.assertEqual(len(artifact["manifest"]["charts"]), 2)

    def test_selected_x_sites_are_present_in_map_payload(self):
        result = run_analysis(self.b_path, self.x_path, AnalysisConfig(mode="fixed_count", count=3))
        payload = build_dashboard_payload(result)
        mapped_selected = {
            str(site["siteid"])
            for site in payload["sites"]
            if site["status"] == "Selected X"
        }
        result_selected = set(
            result.ranked_sites.loc[result.ranked_sites["selected"], "operator_x_siteid"].astype(str)
        )
        self.assertEqual(mapped_selected, result_selected)
        self.assertEqual(len(mapped_selected), result.selected_count)

    def test_adaptive_portfolio_exposes_x_density_and_marginal_metrics(self):
        result = run_analysis(
            self.b_path,
            self.x_path,
            AnalysisConfig(mode="fixed_count", count=4, diversity_weight=0.25, saturation_weight=0.15),
        )
        expected = {
            "local_x_spacing_km", "x_to_b_spacing_ratio", "nearest_selected_x_distance_km",
            "selected_x_spacing_ratio", "selected_x_neighbors", "diversity_factor",
            "saturation_factor", "marginal_score", "redundancy_filtered",
        }
        self.assertTrue(expected.issubset(result.ranked_sites.columns))
        selected = result.ranked_sites[result.ranked_sites.selected].sort_values("selection_rank")
        self.assertTrue(pd.isna(selected.iloc[0]["nearest_selected_x_distance_km"]))
        self.assertTrue((selected.iloc[1:]["nearest_selected_x_distance_km"] > 0).all())
        self.assertTrue((selected["marginal_score"] <= selected["base_score"] + 1e-9).all())

    def test_declustered_threshold_removes_overlapping_x_candidates(self):
        root = Path(self.temp.name)
        b_path = root / "decluster_b.csv"
        x_path = root / "decluster_x.csv"
        pd.DataFrame({
            "siteid": ["B1", "B2"], "latitude": [0.0, 0.0], "longitude": [0.0, 0.1],
        }).to_csv(b_path, index=False)
        pd.DataFrame({
            "siteid": ["X1", "X2", "X3"], "latitude": [0.0, 0.0, 0.0], "longitude": [0.2, 0.2001, 0.3],
        }).to_csv(x_path, index=False)
        all_sites = run_analysis(
            b_path, x_path,
            AnalysisConfig(
                mode="distance_threshold", criterion="absolute", threshold=0,
                threshold_portfolio="all_eligible", min_separation_ratio=0.5,
            ),
        )
        declustered = run_analysis(
            b_path, x_path,
            AnalysisConfig(
                mode="distance_threshold", criterion="absolute", threshold=0,
                threshold_portfolio="declustered", min_separation_ratio=0.5,
            ),
        )
        self.assertEqual(all_sites.selected_count, 3)
        self.assertEqual(declustered.selected_count, 2)
        self.assertEqual(int(declustered.ranked_sites.redundancy_filtered.sum()), 1)
        self.assertIn("minimum X-to-X separation", " ".join(declustered.ranked_sites.selection_reason))


if __name__ == "__main__":
    unittest.main()
