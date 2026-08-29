"""Local Flask GUI for intra-circle roaming site selection."""

from __future__ import annotations

import argparse
import atexit
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from typing import Any, Callable
from uuid import uuid4
import webbrowser
from zipfile import ZIP_DEFLATED, ZipFile

import pandas as pd
from flask import Flask, abort, redirect, render_template, request, send_file, url_for
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from .dashboard import build_dashboard_payload, write_outputs
from .engine import AnalysisConfig, AnalysisResult, run_analysis
from .replication import (
    ReplicationConfig,
    ReplicationResult,
    build_replication_payload,
    replication_summary,
    run_replication,
    write_replication_outputs,
)

TOTAL_UPLOAD_LIMIT = 50 * 1024 * 1024
PER_FILE_UPLOAD_LIMIT = 25 * 1024 * 1024
RUN_TTL_SECONDS = 60 * 60
FILE_LABELS = {
    "ranked_csv": "Ranked candidate sites",
    "issues_csv": "Data-quality issues",
    "summary_csv": "Dashboard KPI source",
    "artifact_json": "Canonical dashboard artifact",
    "online_dashboard": "Online map dashboard",
    "offline_dashboard": "Offline management dashboard",
    "run_manifest": "Run manifest",
    "replication_all_csv": "All X sites with replication decisions",
    "replication_selected_csv": "Selected X footprint sites",
    "replication_issues_csv": "Replication data-quality issues",
    "replication_summary_csv": "Replication KPI summary",
}


class UploadTooLarge(ValueError):
    pass


@dataclass
class StoredRun:
    run_id: str
    created_at: float
    directory: Path
    result: AnalysisResult | ReplicationResult
    payload: dict[str, Any]
    outputs: dict[str, Any]
    files: dict[str, Path]
    manifest: dict[str, Any]
    warnings: list[str]
    timings: dict[str, float]
    workflow: str = "gap"


class RunStore:
    """Thread-safe, temporary, process-local storage for GUI analysis runs."""

    def __init__(
        self,
        root: str | Path | None = None,
        ttl_seconds: int = RUN_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root) if root is not None else Path(tempfile.mkdtemp(prefix="icr-gui-runs-"))
        self.root.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._runs: dict[str, StoredRun] = {}
        self._lock = threading.RLock()
        self._closed = False

    def allocate(self) -> tuple[str, Path]:
        with self._lock:
            self.cleanup_expired()
            run_id = uuid4().hex
            directory = self.root / run_id
            directory.mkdir()
            return run_id, directory

    def add(self, stored_run: StoredRun) -> None:
        with self._lock:
            self._runs[stored_run.run_id] = stored_run

    def get(self, run_id: str) -> StoredRun | None:
        with self._lock:
            self.cleanup_expired()
            return self._runs.get(run_id)

    def delete(self, run_id: str) -> bool:
        with self._lock:
            stored = self._runs.pop(run_id, None)
            directory = stored.directory if stored else self.root / run_id
            try:
                resolved = directory.resolve()
                if resolved.parent == self.root.resolve() and resolved.exists():
                    shutil.rmtree(resolved)
            except OSError:
                return False
            return stored is not None

    def cleanup_expired(self) -> int:
        now = self.clock()
        expired = [run_id for run_id, run in self._runs.items() if now - run.created_at >= self.ttl_seconds]
        for run_id in expired:
            self.delete(run_id)
        return len(expired)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._runs.clear()
            try:
                shutil.rmtree(self.root.resolve())
            except FileNotFoundError:
                pass


def _default_form_values() -> dict[str, str]:
    return {
        "operator_b_name": "Operator B",
        "operator_x_name": "Operator X",
        "mode": "fixed_count",
        "criterion": "density",
        "count": "100",
        "prefilter": "none",
        "threshold": "",
        "local_k": "5",
        "x_local_k": "5",
        "diversity_km": "",
        "diversity_weight": "0.20",
        "saturation_weight": "0.10",
        "min_separation_ratio": "0",
        "threshold_portfolio": "all_eligible",
    }


def _default_replication_values() -> dict[str, str]:
    return {
        "operator_b_name": "Operator B",
        "operator_x_name": "Operator X",
        "retention_percent": "70",
        "x_local_k": "5",
        "b_local_k": "5",
        "zone_scale": "4",
        "min_per_zone": "1",
        "overlap_mode": "adaptive",
        "overlap_ratio": "0.40",
        "overlap_km": "0.50",
        "refill_within_zone": "yes",
    }


def _clean_display_name(value: str | None, fallback: str) -> str:
    cleaned = " ".join((value or "").strip().split())
    return cleaned[:80] if cleaned else fallback


def _parse_config(form: Any) -> tuple[AnalysisConfig | None, dict[str, str], dict[str, str]]:
    values = _default_form_values()
    for key in values:
        if key in form:
            values[key] = str(form.get(key, "")).strip()
    errors: dict[str, str] = {}

    mode = values["mode"]
    criterion = values["criterion"]
    prefilter = values["prefilter"]
    threshold_portfolio = values["threshold_portfolio"]
    if mode not in {"fixed_count", "distance_threshold"}:
        errors["mode"] = "Choose a valid selection mode."
    if criterion not in {"absolute", "density"}:
        errors["criterion"] = "Choose a valid scoring method."
    if prefilter not in {"none", "absolute", "density"}:
        errors["prefilter"] = "Choose a valid pre-filter."
    if threshold_portfolio not in {"all_eligible", "declustered"}:
        errors["threshold_portfolio"] = "Choose a valid threshold portfolio rule."

    count: int | None = None
    if mode == "fixed_count":
        try:
            count = int(values["count"])
            if count < 1:
                raise ValueError
        except ValueError:
            errors["count"] = "Enter a site count of at least 1."

    needs_threshold = mode == "distance_threshold" or (mode == "fixed_count" and prefilter != "none")
    threshold: float | None = None
    if needs_threshold:
        try:
            threshold = float(values["threshold"])
            if threshold < 0:
                raise ValueError
        except ValueError:
            errors["threshold"] = "Enter a non-negative threshold."

    try:
        local_k = int(values["local_k"])
        if local_k < 1:
            raise ValueError
    except ValueError:
        local_k = 5
        errors["local_k"] = "Enter a local-density neighbour count of at least 1."

    try:
        x_local_k = int(values["x_local_k"])
        if x_local_k < 1:
            raise ValueError
    except ValueError:
        x_local_k = 5
        errors["x_local_k"] = "Enter an X-density neighbour count of at least 1."

    diversity_km: float | None = None
    if values["diversity_km"]:
        try:
            diversity_km = float(values["diversity_km"])
            if diversity_km < 0:
                raise ValueError
        except ValueError:
            errors["diversity_km"] = "Enter a non-negative distance or leave it blank."

    weights: dict[str, float] = {}
    for field, label in [("diversity_weight", "diversity"), ("saturation_weight", "saturation")]:
        try:
            weights[field] = float(values[field])
            if not 0 <= weights[field] <= 1:
                raise ValueError
        except ValueError:
            weights[field] = 0.20 if field == "diversity_weight" else 0.10
            errors[field] = f"Enter a {label} weight between 0 and 1."
    if sum(weights.values()) > 1:
        errors["weights"] = "Diversity and saturation weights together cannot exceed 1."

    try:
        min_separation_ratio = float(values["min_separation_ratio"])
        if min_separation_ratio < 0:
            raise ValueError
    except ValueError:
        min_separation_ratio = 0.0
        errors["min_separation_ratio"] = "Enter a non-negative spacing ratio."
    if mode == "distance_threshold" and threshold_portfolio == "declustered" and min_separation_ratio <= 0:
        errors["min_separation_ratio"] = "De-clustered threshold selection requires a ratio greater than 0."

    if errors:
        return None, errors, values
    config = AnalysisConfig(
        mode=mode, criterion=criterion, count=count, prefilter=prefilter,
        threshold=threshold, diversity_km=diversity_km, local_k=local_k, x_local_k=x_local_k,
        diversity_weight=weights["diversity_weight"], saturation_weight=weights["saturation_weight"],
        min_separation_ratio=min_separation_ratio, threshold_portfolio=threshold_portfolio,
    )
    try:
        config.validate()
    except ValueError as error:
        errors["form"] = str(error)
        return None, errors, values
    return config, errors, values


def _parse_replication_config(form: Any) -> tuple[ReplicationConfig | None, dict[str, str], dict[str, str]]:
    values = _default_replication_values()
    for key in values:
        if key in form:
            values[key] = str(form.get(key, "")).strip()
    values["refill_within_zone"] = "yes" if form.get("refill_within_zone") else ""
    errors: dict[str, str] = {}

    def number(field: str, label: str, minimum: float, maximum: float | None = None) -> float:
        try:
            value = float(values[field])
            if value < minimum or (maximum is not None and value > maximum):
                raise ValueError
            return value
        except ValueError:
            boundary = f" between {minimum:g} and {maximum:g}" if maximum is not None else f" of at least {minimum:g}"
            errors[field] = f"Enter {label}{boundary}."
            return minimum

    retention_percent = number("retention_percent", "a retention percentage", 0.01, 100)
    zone_scale = number("zone_scale", "a zone scale", 0.1)
    overlap_ratio = number("overlap_ratio", "a non-negative overlap ratio", 0)
    overlap_km = number("overlap_km", "a non-negative fixed distance", 0)
    integer_values: dict[str, int] = {}
    for field, label, minimum in [
        ("x_local_k", "an X-neighbour count", 1),
        ("b_local_k", "a B-neighbour count", 1),
        ("min_per_zone", "a minimum-per-zone value", 0),
    ]:
        try:
            integer_values[field] = int(values[field])
            if integer_values[field] < minimum:
                raise ValueError
        except ValueError:
            integer_values[field] = minimum
            errors[field] = f"Enter {label} of at least {minimum}."
    overlap_mode = values["overlap_mode"]
    if overlap_mode not in {"adaptive", "fixed"}:
        errors["overlap_mode"] = "Choose adaptive or fixed B-overlap distance."

    if errors:
        return None, errors, values
    config = ReplicationConfig(
        retention_percent=retention_percent,
        x_local_k=integer_values["x_local_k"],
        b_local_k=integer_values["b_local_k"],
        zone_scale=zone_scale,
        min_per_zone=integer_values["min_per_zone"],
        overlap_mode=overlap_mode,
        overlap_ratio=overlap_ratio,
        overlap_km=overlap_km,
        refill_within_zone=bool(values["refill_within_zone"]),
    )
    try:
        config.validate()
    except ValueError as error:
        errors["form"] = str(error)
        return None, errors, values
    return config, errors, values


def _save_upload(file_storage: Any, destination: Path, limit: int) -> None:
    written = 0
    with destination.open("wb") as target:
        while True:
            chunk = file_storage.stream.read(64 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > limit:
                raise UploadTooLarge(f"{file_storage.filename or 'CSV'} exceeds the 25 MB per-file limit")
            target.write(chunk)
    if written == 0:
        raise ValueError(f"{file_storage.filename or 'CSV'} is empty")


def _dataframe_records(frame: pd.DataFrame, limit: int) -> list[dict[str, Any]]:
    return json.loads(frame.head(limit).to_json(orient="records"))


def _run_manifest(
    run_id: str,
    created_at: float,
    source_names: dict[str, str],
    config: AnalysisConfig,
    result: AnalysisResult,
    outputs: dict[str, Any],
    files: dict[str, Path],
    warnings: list[str],
    operator_names: dict[str, str],
    ttl_seconds: int,
    timings: dict[str, float],
) -> dict[str, Any]:
    verification = outputs.get("verification")
    return {
        "schema_version": 1,
        "run_id": run_id,
        "created_at": datetime.fromtimestamp(created_at, timezone.utc).isoformat().replace("+00:00", "Z"),
        "expires_at": datetime.fromtimestamp(created_at + ttl_seconds, timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_files": source_names,
        "operator_names": operator_names,
        "configuration": asdict(config),
        "counts": {
            "valid_b_sites": len(result.valid_b_sites),
            "valid_x_sites": len(result.valid_x_sites),
            "eligible_sites": int(result.ranked_sites["eligible"].sum()),
            "selected_sites": result.selected_count,
            "data_issue_rows": len(result.issues),
        },
        "effective_diversity_km": result.diversity_km,
        "diversity_strategy": "adaptive_local_b_spacing" if config.diversity_km is None else "fixed_km",
        "timings": timings,
        "verification": verification,
        "warnings": warnings,
        "generated_files": [
            {"key": key, "name": path.name, "bytes": path.stat().st_size}
            for key, path in files.items() if path.exists()
        ],
    }


def _replication_manifest(
    run_id: str,
    created_at: float,
    source_names: dict[str, str],
    operator_names: dict[str, str],
    result: ReplicationResult,
    files: dict[str, Path],
    ttl_seconds: int,
    timings: dict[str, float],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "workflow": "x_footprint_replication",
        "run_id": run_id,
        "created_at": datetime.fromtimestamp(created_at, timezone.utc).isoformat().replace("+00:00", "Z"),
        "expires_at": datetime.fromtimestamp(created_at + ttl_seconds, timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_files": source_names,
        "operator_names": operator_names,
        "configuration": asdict(result.config),
        "counts": replication_summary(result),
        "method": {
            "zone_cell_size_km": result.cell_size_km,
            "global_x_spacing_km": result.global_x_spacing_km,
            "global_b_spacing_km": result.global_b_spacing_km,
            "site_percentage_is_not_rf_coverage_percentage": True,
        },
        "timings": timings,
        "warnings": [],
        "generated_files": [
            {"key": key, "name": path.name, "bytes": path.stat().st_size}
            for key, path in files.items() if path.exists()
        ],
    }


def create_app(test_config: dict[str, Any] | None = None, run_store: RunStore | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        MAX_CONTENT_LENGTH=TOTAL_UPLOAD_LIMIT,
        PER_FILE_UPLOAD_LIMIT=PER_FILE_UPLOAD_LIMIT,
        RUN_TTL_SECONDS=RUN_TTL_SECONDS,
        BUILDER_ROOT=os.environ.get("ICR_DATA_ANALYTICS_PLUGIN_ROOT"),
        NODE_EXECUTABLE=os.environ.get("ICR_NODE_EXECUTABLE"),
    )
    if test_config:
        app.config.update(test_config)
    store = run_store or RunStore(ttl_seconds=int(app.config["RUN_TTL_SECONDS"]))
    app.extensions["icr_run_store"] = store
    if run_store is None:
        atexit.register(store.close)

    def form_response(errors: dict[str, str] | None = None, values: dict[str, str] | None = None, status: int = 200):
        return render_template(
            "index.html",
            errors=errors or {},
            values=values or _default_form_values(),
            cleared=request.args.get("cleared") == "1",
        ), status

    def replication_form_response(errors: dict[str, str] | None = None, values: dict[str, str] | None = None, status: int = 200):
        return render_template(
            "replication.html",
            errors=errors or {},
            values=values or _default_replication_values(),
            cleared=request.args.get("cleared") == "1",
        ), status

    @app.before_request
    def cleanup_expired_runs() -> None:
        store.cleanup_expired()

    @app.errorhandler(RequestEntityTooLarge)
    def upload_too_large(_: RequestEntityTooLarge):
        if request.path.startswith("/replication"):
            return replication_form_response({"form": "The combined upload exceeds the 50 MB request limit."}, status=413)
        return form_response({"form": "The combined upload exceeds the 50 MB request limit."}, status=413)

    @app.errorhandler(404)
    def not_found(_: Any):
        return render_template("error.html", title="Run not found", message="This run does not exist or its one-hour retention period has expired."), 404

    @app.get("/")
    def index():
        return form_response()[0]

    @app.get("/help")
    def help_page():
        return render_template("help.html")

    @app.get("/replication")
    def replication_index():
        return replication_form_response()[0]

    @app.get("/replication/help")
    def replication_help():
        return render_template("replication_help.html")

    def execute_run(
        b_path: Path,
        x_path: Path,
        source_names: dict[str, str],
        operator_names: dict[str, str],
        config: AnalysisConfig,
        run_id: str,
        directory: Path,
    ) -> StoredRun:
        run_started = time.perf_counter()
        result = run_analysis(b_path, x_path, config)
        output_started = time.perf_counter()
        outputs = write_outputs(
            result,
            directory,
            operator_b_name=operator_names["operator_b"],
            operator_x_name=operator_names["operator_x"],
            builder_root=app.config.get("BUILDER_ROOT"),
            node_executable=app.config.get("NODE_EXECUTABLE"),
            offline_required=False,
            verify_offline_browser=False,
        )
        output_seconds = time.perf_counter() - output_started
        warnings = list(outputs.get("warnings", []))
        files = {
            key: Path(value) for key, value in outputs.items()
            if key in FILE_LABELS and isinstance(value, str) and Path(value).is_file()
        }
        payload_started = time.perf_counter()
        payload = build_dashboard_payload(result)
        payload_seconds = time.perf_counter() - payload_started
        timings = {
            **result.timings,
            "output_generation_seconds": round(output_seconds, 6),
            "dashboard_payload_seconds": round(payload_seconds, 6),
            "gui_run_total_seconds": round(time.perf_counter() - run_started, 6),
        }
        created_at = store.clock()
        manifest = _run_manifest(
            run_id, created_at, source_names, config, result, outputs, files, warnings,
            operator_names, store.ttl_seconds, timings,
        )
        manifest_path = directory / "run_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        files["run_manifest"] = manifest_path
        stored_run = StoredRun(
            run_id=run_id,
            created_at=created_at,
            directory=directory,
            result=result,
            payload=payload,
            outputs=outputs,
            files=files,
            manifest=manifest,
            warnings=warnings,
            timings=timings,
        )
        store.add(stored_run)
        return stored_run

    def execute_replication_run(
        b_path: Path,
        x_path: Path,
        source_names: dict[str, str],
        operator_names: dict[str, str],
        config: ReplicationConfig,
        run_id: str,
        directory: Path,
    ) -> StoredRun:
        run_started = time.perf_counter()
        result = run_replication(b_path, x_path, config)
        output_started = time.perf_counter()
        outputs = write_replication_outputs(result, directory)
        output_seconds = time.perf_counter() - output_started
        files = {
            key: Path(value) for key, value in outputs.items()
            if key in FILE_LABELS and isinstance(value, str) and Path(value).is_file()
        }
        payload_started = time.perf_counter()
        payload = build_replication_payload(result)
        payload_seconds = time.perf_counter() - payload_started
        timings = {
            **result.timings,
            "output_generation_seconds": round(output_seconds, 6),
            "dashboard_payload_seconds": round(payload_seconds, 6),
            "gui_run_total_seconds": round(time.perf_counter() - run_started, 6),
        }
        created_at = store.clock()
        manifest = _replication_manifest(
            run_id, created_at, source_names, operator_names, result, files, store.ttl_seconds, timings,
        )
        manifest_path = directory / "run_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        files["run_manifest"] = manifest_path
        stored_run = StoredRun(
            run_id=run_id,
            created_at=created_at,
            directory=directory,
            result=result,
            payload=payload,
            outputs=outputs,
            files=files,
            manifest=manifest,
            warnings=[],
            timings=timings,
            workflow="replication",
        )
        store.add(stored_run)
        return stored_run

    @app.post("/runs")
    def create_run():
        config, errors, values = _parse_config(request.form)
        b_file = request.files.get("operator_b_csv")
        x_file = request.files.get("operator_x_csv")
        for field, uploaded, label in [
            ("operator_b_csv", b_file, "Operator B CSV"),
            ("operator_x_csv", x_file, "Operator X CSV"),
        ]:
            if uploaded is None or not uploaded.filename:
                errors[field] = f"Choose the {label}."
            elif Path(uploaded.filename).suffix.lower() != ".csv":
                errors[field] = "Only .csv files are accepted."
        if errors or config is None:
            return form_response(errors, values, status=400)

        run_id, directory = store.allocate()
        source_names = {
            "operator_b": secure_filename(b_file.filename) or "operator_b.csv",
            "operator_x": secure_filename(x_file.filename) or "operator_x.csv",
        }
        operator_names = {
            "operator_b": _clean_display_name(request.form.get("operator_b_name"), "Operator B"),
            "operator_x": _clean_display_name(request.form.get("operator_x_name"), "Operator X"),
        }
        try:
            b_path = directory / "operator_b.csv"
            x_path = directory / "operator_x.csv"
            _save_upload(b_file, b_path, int(app.config["PER_FILE_UPLOAD_LIMIT"]))
            _save_upload(x_file, x_path, int(app.config["PER_FILE_UPLOAD_LIMIT"]))
            execute_run(b_path, x_path, source_names, operator_names, config, run_id, directory)
        except (UploadTooLarge, ValueError, pd.errors.ParserError, UnicodeDecodeError) as error:
            store.delete(run_id)
            return form_response({"form": str(error)}, values, status=400)
        except Exception:
            app.logger.exception("ICR analysis run failed")
            store.delete(run_id)
            return form_response({"form": "The analysis could not be completed. Check the CSV content and application configuration."}, values, status=500)
        return redirect(url_for("run_result", run_id=run_id))

    @app.post("/runs/sample")
    def sample_run():
        run_id, directory = store.allocate()
        sample_root = Path(__file__).resolve().parent / "sample_data"
        config = AnalysisConfig(mode="fixed_count", criterion="density", count=5, local_k=5)
        source_names = {"operator_b": "operator_b.csv", "operator_x": "operator_x.csv"}
        operator_names = {"operator_b": "Operator B", "operator_x": "Operator X"}
        try:
            execute_run(
                sample_root / "operator_b.csv",
                sample_root / "operator_x.csv",
                source_names, operator_names, config, run_id, directory,
            )
        except Exception:
            app.logger.exception("Sample ICR analysis run failed")
            store.delete(run_id)
            return form_response({"form": "The bundled sample run could not be completed."}, status=500)
        return redirect(url_for("run_result", run_id=run_id))

    @app.post("/replication/runs")
    def create_replication_run():
        config, errors, values = _parse_replication_config(request.form)
        b_file = request.files.get("operator_b_csv")
        x_file = request.files.get("operator_x_csv")
        for field, uploaded, label in [
            ("operator_b_csv", b_file, "Operator B CSV"),
            ("operator_x_csv", x_file, "Operator X CSV"),
        ]:
            if uploaded is None or not uploaded.filename:
                errors[field] = f"Choose the {label}."
            elif Path(uploaded.filename).suffix.lower() != ".csv":
                errors[field] = "Only .csv files are accepted."
        if errors or config is None:
            return replication_form_response(errors, values, status=400)

        run_id, directory = store.allocate()
        source_names = {
            "operator_b": secure_filename(b_file.filename) or "operator_b.csv",
            "operator_x": secure_filename(x_file.filename) or "operator_x.csv",
        }
        operator_names = {
            "operator_b": _clean_display_name(request.form.get("operator_b_name"), "Operator B"),
            "operator_x": _clean_display_name(request.form.get("operator_x_name"), "Operator X"),
        }
        try:
            b_path = directory / "operator_b.csv"
            x_path = directory / "operator_x.csv"
            _save_upload(b_file, b_path, int(app.config["PER_FILE_UPLOAD_LIMIT"]))
            _save_upload(x_file, x_path, int(app.config["PER_FILE_UPLOAD_LIMIT"]))
            execute_replication_run(b_path, x_path, source_names, operator_names, config, run_id, directory)
        except (UploadTooLarge, ValueError, pd.errors.ParserError, UnicodeDecodeError) as error:
            store.delete(run_id)
            return replication_form_response({"form": str(error)}, values, status=400)
        except Exception:
            app.logger.exception("X footprint replication run failed")
            store.delete(run_id)
            return replication_form_response(
                {"form": "The replication analysis could not be completed. Check the CSV content and parameters."},
                values,
                status=500,
            )
        return redirect(url_for("replication_result", run_id=run_id))

    @app.post("/replication/runs/sample")
    def sample_replication_run():
        run_id, directory = store.allocate()
        sample_root = Path(__file__).resolve().parent / "sample_data"
        config = ReplicationConfig()
        source_names = {"operator_b": "operator_b.csv", "operator_x": "operator_x.csv"}
        operator_names = {"operator_b": "Operator B", "operator_x": "Operator X"}
        try:
            execute_replication_run(
                sample_root / "operator_b.csv",
                sample_root / "operator_x.csv",
                source_names, operator_names, config, run_id, directory,
            )
        except Exception:
            app.logger.exception("Sample X footprint replication run failed")
            store.delete(run_id)
            return replication_form_response({"form": "The bundled replication sample could not be completed."}, status=500)
        return redirect(url_for("replication_result", run_id=run_id))

    def required_run(run_id: str) -> StoredRun:
        stored_run = store.get(run_id)
        if stored_run is None:
            abort(404)
        return stored_run

    @app.get("/runs/<run_id>")
    def run_result(run_id: str):
        stored_run = required_run(run_id)
        if stored_run.workflow != "gap":
            abort(404)
        bands = list(stored_run.payload["bands"])
        maximum = max((row["candidate_count"] for row in bands), default=0) or 1
        for row in bands:
            row["bar_percent"] = round(100 * row["candidate_count"] / maximum, 2)
        return render_template(
            "results.html",
            run=stored_run,
            summary=stored_run.payload["summary"],
            bands=bands,
            map_payload=stored_run.payload,
            ranked_rows=_dataframe_records(stored_run.result.ranked_sites, 100),
            issue_rows=_dataframe_records(stored_run.result.issues, 100),
            file_labels=FILE_LABELS,
        )

    @app.get("/replication/runs/<run_id>")
    def replication_result(run_id: str):
        stored_run = required_run(run_id)
        if stored_run.workflow != "replication":
            abort(404)
        return render_template(
            "replication_results.html",
            run=stored_run,
            summary=stored_run.payload["summary"],
            map_payload=stored_run.payload,
            site_rows=_dataframe_records(stored_run.result.sites, 100),
            issue_rows=_dataframe_records(stored_run.result.issues, 100),
            file_labels=FILE_LABELS,
        )

    @app.get("/runs/<run_id>/files/<file_key>")
    def download_file(run_id: str, file_key: str):
        stored_run = required_run(run_id)
        path = stored_run.files.get(file_key)
        if file_key not in FILE_LABELS or path is None or not path.is_file():
            abort(404)
        return send_file(path, as_attachment=True, download_name=path.name)

    @app.get("/runs/<run_id>/package.zip")
    def download_package(run_id: str):
        stored_run = required_run(run_id)
        archive = BytesIO()
        with ZipFile(archive, "w", compression=ZIP_DEFLATED) as package:
            for key, path in stored_run.files.items():
                if key in FILE_LABELS and path.is_file():
                    package.write(path, arcname=path.name)
        archive.seek(0)
        prefix = "icr_replication" if stored_run.workflow == "replication" else "icr_run"
        return send_file(archive, mimetype="application/zip", as_attachment=True, download_name=f"{prefix}_{run_id[:8]}.zip")

    @app.post("/runs/<run_id>/delete")
    def delete_run(run_id: str):
        stored_run = store.get(run_id)
        if stored_run is None:
            abort(404)
        store.delete(run_id)
        destination = "replication_index" if stored_run.workflow == "replication" else "index"
        return redirect(url_for(destination, cleared="1"))

    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local ICR site-selection GUI.")
    parser.add_argument("--port", type=int, default=5000, help="Localhost port (default: 5000)")
    parser.add_argument("--no-open", action="store_true", help="Do not open the GUI in the default browser")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")
    url = f"http://127.0.0.1:{args.port}/"
    if not args.no_open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"ICR GUI available at {url}")
    create_app().run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
