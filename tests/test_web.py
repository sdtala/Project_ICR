from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from icr_analysis.web import RunStore, create_app


B_CSV = b"siteid,latitude,longitude\nB1,12.97,77.59\nB2,12.99,77.61\nB3,13.01,77.63\n"
X_CSV = b"siteid,latitude,longitude\nX1,12.971,77.591\nX2,13.04,77.66\nX3,13.08,77.70\n"


def output_stub(result, output_dir, **kwargs):
    destination = Path(output_dir)
    paths = {
        "ranked_csv": destination / "icr_ranked_sites.csv",
        "issues_csv": destination / "icr_data_issues.csv",
        "summary_csv": destination / "icr_dashboard_summary.csv",
        "artifact_json": destination / "artifact.json",
        "online_dashboard": destination / "icr_dashboard_online.html",
        "offline_dashboard": destination / "icr_dashboard_offline.html",
    }
    result.ranked_sites.to_csv(paths["ranked_csv"], index=False)
    result.issues.to_csv(paths["issues_csv"], index=False)
    paths["summary_csv"].write_text("selected_sites\n%d\n" % result.selected_count, encoding="utf-8")
    paths["artifact_json"].write_text("{}", encoding="utf-8")
    paths["online_dashboard"].write_text("<html>online</html>", encoding="utf-8")
    paths["offline_dashboard"].write_text("<html>offline</html>", encoding="utf-8")
    return {
        **{key: str(path.resolve()) for key, path in paths.items()},
        "verification": {"ok": True, "stages": {"verification": "passed"}},
        "warnings": [],
    }


def partial_output_stub(result, output_dir, **kwargs):
    outputs = output_stub(result, output_dir, **kwargs)
    offline = Path(outputs.pop("offline_dashboard"))
    offline.unlink()
    outputs["warnings"] = ["Offline dashboard was not generated: builder unavailable"]
    outputs["verification"] = None
    return outputs


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = RunStore(Path(self.temp.name) / "runs", ttl_seconds=3600)
        self.app = create_app({"TESTING": True, "PER_FILE_UPLOAD_LIMIT": 1024 * 1024}, run_store=self.store)
        self.client = self.app.test_client()
        self.output_patch = patch("icr_analysis.web.write_outputs", side_effect=output_stub)
        self.output_patch.start()

    def tearDown(self):
        self.output_patch.stop()
        self.store.close()
        self.temp.cleanup()

    def upload(self, **overrides):
        data = {
            "operator_b_name": "B Telecom",
            "operator_x_name": "X Telecom",
            "mode": "fixed_count",
            "criterion": "density",
            "count": "2",
            "prefilter": "none",
            "threshold": "",
            "local_k": "3",
            "x_local_k": "3",
            "diversity_km": "",
            "diversity_weight": "0.20",
            "saturation_weight": "0.10",
            "min_separation_ratio": "0",
            "threshold_portfolio": "all_eligible",
            "operator_b_csv": (BytesIO(B_CSV), "b sites.csv"),
            "operator_x_csv": (BytesIO(X_CSV), "x sites.csv"),
        }
        data.update(overrides)
        return self.client.post("/runs", data=data, content_type="multipart/form-data")

    def replication_upload(self, **overrides):
        data = {
            "operator_b_name": "B Telecom",
            "operator_x_name": "X Telecom",
            "retention_percent": "70",
            "x_local_k": "3",
            "b_local_k": "3",
            "zone_scale": "4",
            "min_per_zone": "1",
            "overlap_mode": "adaptive",
            "overlap_ratio": "0.40",
            "overlap_km": "0.50",
            "refill_within_zone": "yes",
            "operator_b_csv": (BytesIO(B_CSV), "b sites.csv"),
            "operator_x_csv": (BytesIO(X_CSV), "x sites.csv"),
        }
        data.update(overrides)
        return self.client.post("/replication/runs", data=data, content_type="multipart/form-data")

    @staticmethod
    def run_id(response):
        return response.headers["Location"].rstrip("/").split("/")[-1]

    def test_home_and_valid_fixed_count_result(self):
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn(b"Run site selection", home.data)
        self.assertIn(b"Methodology and parameter guide", home.data)
        help_page = self.client.get("/help")
        self.assertEqual(help_page.status_code, 200)
        self.assertIn(b"Recommended starting values", help_page.data)
        self.assertIn(b"marginal score", help_page.data)
        self.assertIn(b"A picture-first explanation", help_page.data)
        self.assertGreaterEqual(help_page.data.count(b'role="img"'), 4)
        stylesheet = self.client.get("/static/app.css")
        self.assertIn(b"[hidden]{display:none!important}", stylesheet.data)
        stylesheet.close()
        leaflet = self.client.get("/static/vendor/leaflet/leaflet.js")
        self.assertEqual(leaflet.status_code, 200)
        self.assertGreater(len(leaflet.data), 100_000)
        leaflet.close()
        response = self.upload()
        self.assertEqual(response.status_code, 302)
        run_id = self.run_id(response)
        stored = self.store.get(run_id)
        self.assertEqual(stored.result.selected_count, 2)
        result_page = self.client.get(response.headers["Location"])
        self.assertEqual(result_page.status_code, 200)
        self.assertIn(b"B Telecom", json.dumps(stored.manifest).encode())
        self.assertIn(b"Ranked candidate preview", result_page.data)
        self.assertIn(b"Geographic gap proxy", result_page.data)
        self.assertIn(b"Processing performance", result_page.data)
        self.assertIn(b"vendor/leaflet/leaflet.js", result_page.data)
        self.assertNotIn(b"unpkg.com/leaflet", result_page.data)
        self.assertIn(b"larger gold circles", result_page.data)
        selected_map_sites = [site for site in stored.payload["sites"] if site["status"] == "Selected X"]
        self.assertEqual(len(selected_map_sites), stored.result.selected_count)
        map_script = self.client.get("/static/results.js")
        self.assertIn(b"layers[site.status].addLayer(marker)", map_script.data)
        map_script.close()
        self.assertIn("analysis_total_seconds", stored.manifest["timings"])

    def test_threshold_and_prefilter_forms(self):
        threshold = self.upload(mode="distance_threshold", criterion="absolute", count="", threshold="3")
        self.assertEqual(threshold.status_code, 302)
        stored = self.store.get(self.run_id(threshold))
        self.assertTrue((stored.result.ranked_sites.loc[stored.result.ranked_sites.selected, "nearest_b_distance_km"] >= 3).all())

        filtered = self.upload(prefilter="absolute", threshold="3", count="3")
        self.assertEqual(filtered.status_code, 302)
        filtered_run = self.store.get(self.run_id(filtered))
        self.assertTrue((filtered_run.result.ranked_sites.loc[filtered_run.result.ranked_sites.selected, "nearest_b_distance_km"] >= 3).all())

        declustered = self.upload(
            mode="distance_threshold", criterion="absolute", count="", threshold="0",
            threshold_portfolio="declustered", min_separation_ratio="0.5",
            diversity_weight="0.3", saturation_weight="0.2", x_local_k="2",
        )
        self.assertEqual(declustered.status_code, 302)
        configured = self.store.get(self.run_id(declustered)).result.config
        self.assertEqual(configured.threshold_portfolio, "declustered")
        self.assertEqual(configured.min_separation_ratio, 0.5)
        self.assertEqual(configured.x_local_k, 2)

    def test_sample_run(self):
        response = self.client.post("/runs/sample")
        self.assertEqual(response.status_code, 302)
        stored = self.store.get(self.run_id(response))
        self.assertEqual(stored.result.selected_count, 5)
        self.assertEqual(stored.manifest["configuration"]["criterion"], "density")

    def test_separate_replication_workflow_result_and_downloads(self):
        page = self.client.get("/replication")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Replicate a balanced percentage", page.data)
        self.assertIn(b"Separate planning strategy", page.data)
        guide = self.client.get("/replication/help")
        self.assertEqual(guide.status_code, 200)
        self.assertIn(b"70% of sites is not 70% coverage", guide.data)
        self.assertIn(b"Calculation and audit rules", guide.data)
        self.assertIn(b"overall target = max", guide.data)
        self.assertIn(b"548 or more sites", guide.data)
        self.assertIn(b"not a commercial-benefit ranking", guide.data)

        response = self.replication_upload(overlap_mode="fixed", overlap_km="0")
        self.assertEqual(response.status_code, 302)
        run_id = self.run_id(response)
        stored = self.store.get(run_id)
        self.assertEqual(stored.workflow, "replication")
        self.assertEqual(stored.result.target_count, 2)
        self.assertEqual(stored.result.selected_count, 2)
        selected_map = [site for site in stored.payload["sites"] if site["status"] == "Selected X footprint"]
        self.assertEqual(len(selected_map), stored.result.selected_count)
        result = self.client.get(response.headers["Location"])
        self.assertIn(b"Representative Operator X site-plan result", result.data)
        self.assertIn(b"Site representation", result.data)
        self.assertEqual(self.client.get(f"/runs/{run_id}").status_code, 404)

        selected_csv = self.client.get(f"/runs/{run_id}/files/replication_selected_csv")
        self.assertEqual(selected_csv.status_code, 200)
        selected_csv.close()
        package = self.client.get(f"/runs/{run_id}/package.zip")
        with ZipFile(BytesIO(package.data)) as archive:
            names = set(archive.namelist())
            self.assertIn("icr_replication_selected_x_sites.csv", names)
            manifest = json.loads(archive.read("run_manifest.json"))
        self.assertEqual(manifest["workflow"], "x_footprint_replication")
        self.assertEqual(manifest["counts"]["final_selected_sites"], 2)
        package.close()

    def test_replication_sample_and_validation(self):
        sample = self.client.post("/replication/runs/sample")
        self.assertEqual(sample.status_code, 302)
        stored = self.store.get(self.run_id(sample))
        self.assertEqual(stored.workflow, "replication")
        self.assertEqual(stored.result.target_count, 7)

        invalid = self.replication_upload(retention_percent="101")
        self.assertEqual(invalid.status_code, 400)
        self.assertIn(b"retention percentage", invalid.data)
        missing = self.client.post("/replication/runs", data={"retention_percent": "70"})
        self.assertEqual(missing.status_code, 400)
        self.assertIn(b"Choose the Operator B CSV", missing.data)

    def test_upload_and_parameter_validation(self):
        missing = self.client.post("/runs", data={"mode": "fixed_count", "count": "1"})
        self.assertEqual(missing.status_code, 400)
        self.assertIn(b"Choose the Operator B CSV", missing.data)

        wrong_extension = self.upload(operator_b_csv=(BytesIO(B_CSV), "b.xlsx"))
        self.assertEqual(wrong_extension.status_code, 400)
        self.assertIn(b"Only .csv files", wrong_extension.data)

        no_threshold = self.upload(mode="distance_threshold", count="", threshold="")
        self.assertEqual(no_threshold.status_code, 400)
        self.assertIn(b"Enter a non-negative threshold", no_threshold.data)

        no_spacing = self.upload(
            mode="distance_threshold", count="", threshold="0",
            threshold_portfolio="declustered", min_separation_ratio="0",
        )
        self.assertEqual(no_spacing.status_code, 400)
        self.assertIn(b"requires a ratio greater than 0", no_spacing.data)

        invalid_weights = self.upload(diversity_weight="0.8", saturation_weight="0.4")
        self.assertEqual(invalid_weights.status_code, 400)
        self.assertIn(b"cannot exceed 1", invalid_weights.data)

        missing_columns = self.upload(operator_b_csv=(BytesIO(b"site,lat,lon\nB1,1,2\n"), "b.csv"))
        self.assertEqual(missing_columns.status_code, 400)
        self.assertIn(b"missing required column", missing_columns.data)

        invalid_sites = self.upload(operator_b_csv=(BytesIO(b"siteid,latitude,longitude\nB1,999,2\n"), "b.csv"))
        self.assertEqual(invalid_sites.status_code, 400)
        self.assertIn(b"no valid sites", invalid_sites.data)

    def test_per_file_and_total_upload_limits(self):
        self.app.config["PER_FILE_UPLOAD_LIMIT"] = 20
        per_file = self.upload()
        self.assertEqual(per_file.status_code, 400)
        self.assertIn(b"25 MB per-file limit", per_file.data)

        self.app.config["MAX_CONTENT_LENGTH"] = 100
        total = self.upload()
        self.assertEqual(total.status_code, 413)
        self.assertIn(b"50 MB request limit", total.data)

    def test_downloads_zip_manifest_and_whitelist(self):
        response = self.upload()
        run_id = self.run_id(response)
        ranked = self.client.get(f"/runs/{run_id}/files/ranked_csv")
        self.assertEqual(ranked.status_code, 200)
        self.assertIn("attachment", ranked.headers["Content-Disposition"])
        ranked.close()
        self.assertEqual(self.client.get(f"/runs/{run_id}/files/not_allowed").status_code, 404)
        self.assertEqual(self.client.get(f"/runs/{run_id}/files/%2e%2e").status_code, 404)

        package = self.client.get(f"/runs/{run_id}/package.zip")
        self.assertEqual(package.status_code, 200)
        with ZipFile(BytesIO(package.data)) as archive:
            names = set(archive.namelist())
            self.assertIn("icr_ranked_sites.csv", names)
            self.assertIn("run_manifest.json", names)
            manifest = json.loads(archive.read("run_manifest.json"))
        self.assertEqual(manifest["source_files"]["operator_b"], "b_sites.csv")
        self.assertEqual(manifest["counts"]["selected_sites"], 2)
        package.close()

    def test_partial_offline_failure_is_nonfatal(self):
        self.output_patch.stop()
        self.output_patch = patch("icr_analysis.web.write_outputs", side_effect=partial_output_stub)
        self.output_patch.start()
        response = self.upload()
        self.assertEqual(response.status_code, 302)
        run_id = self.run_id(response)
        page = self.client.get(response.headers["Location"])
        self.assertIn(b"Offline dashboard was not generated", page.data)
        self.assertEqual(self.client.get(f"/runs/{run_id}/files/offline_dashboard").status_code, 404)
        package = self.client.get(f"/runs/{run_id}/package.zip")
        with ZipFile(BytesIO(package.data)) as archive:
            self.assertNotIn("icr_dashboard_offline.html", archive.namelist())

    def test_uuid_isolation_manual_delete_and_expiry(self):
        first = self.upload()
        second = self.upload()
        first_id, second_id = self.run_id(first), self.run_id(second)
        self.assertNotEqual(first_id, second_id)
        first_dir = self.store.get(first_id).directory
        deleted = self.client.post(f"/runs/{first_id}/delete")
        self.assertEqual(deleted.status_code, 302)
        self.assertFalse(first_dir.exists())
        self.assertEqual(self.client.get(f"/runs/{first_id}").status_code, 404)
        self.assertEqual(self.client.get(f"/runs/{second_id}").status_code, 200)

        clock = [1000.0]
        expiring_store = RunStore(Path(self.temp.name) / "expiring", ttl_seconds=10, clock=lambda: clock[0])
        expiring_app = create_app({"TESTING": True}, run_store=expiring_store)
        with patch("icr_analysis.web.write_outputs", side_effect=output_stub):
            client = expiring_app.test_client()
            response = client.post("/runs/sample")
            run_id = self.run_id(response)
            run_dir = expiring_store.get(run_id).directory
            clock[0] = 1011.0
            self.assertEqual(client.get(f"/runs/{run_id}").status_code, 404)
            self.assertFalse(run_dir.exists())
        expiring_store.close()


if __name__ == "__main__":
    unittest.main()
