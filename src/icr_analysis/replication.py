"""Density-preserving Operator X footprint replication analysis."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from time import perf_counter
from typing import Literal

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

from .engine import ISSUE_COLUMNS, load_sites
from .geo import EARTH_RADIUS_KM, latlon_to_unit

OverlapMode = Literal["adaptive", "fixed"]


@dataclass(frozen=True)
class ReplicationConfig:
    retention_percent: float = 70.0
    x_local_k: int = 5
    b_local_k: int = 5
    zone_scale: float = 4.0
    min_per_zone: int = 1
    overlap_mode: OverlapMode = "adaptive"
    overlap_ratio: float = 0.40
    overlap_km: float = 0.50
    refill_within_zone: bool = True

    def validate(self) -> None:
        if not 0 < self.retention_percent <= 100:
            raise ValueError("retention_percent must be greater than 0 and at most 100")
        if self.x_local_k < 1 or self.b_local_k < 1:
            raise ValueError("local neighbour counts must be at least 1")
        if self.zone_scale <= 0:
            raise ValueError("zone_scale must be greater than 0")
        if self.min_per_zone < 0:
            raise ValueError("min_per_zone cannot be negative")
        if self.overlap_mode not in {"adaptive", "fixed"}:
            raise ValueError("overlap_mode must be adaptive or fixed")
        if self.overlap_ratio < 0:
            raise ValueError("overlap_ratio cannot be negative")
        if self.overlap_km < 0:
            raise ValueError("overlap_km cannot be negative")


@dataclass
class ReplicationResult:
    sites: pd.DataFrame
    issues: pd.DataFrame
    valid_b_sites: pd.DataFrame
    valid_x_sites: pd.DataFrame
    config: ReplicationConfig
    target_count: int
    zone_count: int
    cell_size_km: float
    global_x_spacing_km: float
    global_b_spacing_km: float
    timings: dict[str, float]

    @property
    def selected_count(self) -> int:
        return int(self.sites["final_selected"].sum())

    @property
    def overlap_removed_count(self) -> int:
        return int(self.sites["overlap_rejected"].sum())

    @property
    def refill_count(self) -> int:
        return int(self.sites["refill_selected"].sum())


def replication_summary(result: ReplicationResult) -> dict[str, float | int]:
    selected = result.sites[result.sites["final_selected"]]
    initial_overlap = result.sites["initial_representative"] & result.sites["overlap_rejected"]
    return {
        "valid_b_sites": len(result.valid_b_sites),
        "valid_x_sites": len(result.valid_x_sites),
        "retention_percent": float(result.config.retention_percent),
        "representative_target": int(result.target_count),
        "initial_overlap_removed": int(initial_overlap.sum()),
        "overlap_rejected_sites": int(result.overlap_removed_count),
        "refill_sites": int(result.refill_count),
        "final_selected_sites": int(result.selected_count),
        "achieved_retention_percent": round(100.0 * result.selected_count / len(result.valid_x_sites), 3),
        "geographic_zones": int(result.zone_count),
        "zone_cell_size_km": round(float(result.cell_size_km), 6),
        "median_selected_x_spacing_km": round(float(selected["local_x_spacing_km"].median()), 6) if not selected.empty else 0.0,
        "median_selected_b_distance_km": round(float(selected["nearest_b_distance_km"].median()), 6) if not selected.empty else 0.0,
        "data_issue_rows": len(result.issues),
    }


def build_replication_payload(result: ReplicationResult) -> dict[str, object]:
    b_sites = result.valid_b_sites[["siteid", "latitude", "longitude"]].copy()
    b_sites["status"] = "Operator B"
    x_sites = result.sites[[
        "operator_x_siteid", "latitude", "longitude", "status", "zone_id",
        "local_x_spacing_km", "nearest_b_siteid", "nearest_b_distance_km",
        "overlap_threshold_km", "selection_rank", "selection_reason",
    ]].rename(columns={"operator_x_siteid": "siteid", "nearest_b_siteid": "nearest_b"})
    site_records = json.loads(pd.concat([b_sites, x_sites], ignore_index=True).to_json(orient="records"))
    return {"summary": replication_summary(result), "sites": site_records}


def write_replication_outputs(result: ReplicationResult, output_dir: str | Path) -> dict[str, str]:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    paths = {
        "replication_all_csv": destination / "icr_replication_all_x_sites.csv",
        "replication_selected_csv": destination / "icr_replication_selected_x_sites.csv",
        "replication_issues_csv": destination / "icr_replication_data_issues.csv",
        "replication_summary_csv": destination / "icr_replication_summary.csv",
    }
    result.sites.to_csv(paths["replication_all_csv"], index=False)
    result.sites.loc[result.sites["final_selected"]].to_csv(paths["replication_selected_csv"], index=False)
    result.issues.to_csv(paths["replication_issues_csv"], index=False)
    pd.DataFrame([replication_summary(result)]).to_csv(paths["replication_summary_csv"], index=False)
    return {key: str(path.resolve()) for key, path in paths.items()}


def _chord_to_km(chord: np.ndarray | float) -> np.ndarray:
    values = np.asarray(chord, dtype=float)
    return 2.0 * EARTH_RADIUS_KM * np.arcsin(np.clip(values / 2.0, 0.0, 1.0))


def _query(tree: cKDTree, points: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    distances, indices = tree.query(points, k=k, workers=-1)
    if k == 1:
        distances = np.asarray(distances)[:, None]
        indices = np.asarray(indices)[:, None]
    return np.asarray(distances, dtype=float), np.asarray(indices, dtype=int)


def _nearest_spacing(points: np.ndarray) -> tuple[cKDTree, np.ndarray, float]:
    tree = cKDTree(points)
    if len(points) == 1:
        spacing = np.array([1.0], dtype=float)
    else:
        chord, _ = _query(tree, points, 2)
        spacing = _chord_to_km(chord[:, 1])
    positive = spacing[spacing > 0]
    global_spacing = float(np.median(positive)) if len(positive) else 1.0
    return tree, spacing, global_spacing


def _local_x_spacing(tree: cKDTree, points: np.ndarray, nearest: np.ndarray, local_k: int) -> np.ndarray:
    if len(points) == 1:
        return nearest.copy()
    effective = min(local_k + 1, len(points))
    chord, _ = _query(tree, points, effective)
    distances = _chord_to_km(chord[:, 1:])
    return np.median(distances, axis=1)


def _project_km(latitude: np.ndarray, longitude: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    latitude = np.asarray(latitude, dtype=float)
    longitude = np.asarray(longitude, dtype=float)
    latitude_origin = float(np.median(latitude))
    longitude_origin = float(np.median(longitude))
    longitude_delta = (longitude - longitude_origin + 180.0) % 360.0 - 180.0
    northing = EARTH_RADIUS_KM * np.radians(latitude - latitude_origin)
    easting = EARTH_RADIUS_KM * np.cos(np.radians(latitude_origin)) * np.radians(longitude_delta)
    return easting, northing


def _allocate_quotas(zone_members: dict[str, list[int]], target: int, fraction: float, minimum: int) -> dict[str, int]:
    keys = sorted(zone_members)
    enforce_minimum = minimum if target >= minimum * len(keys) else 0
    raw = {key: fraction * len(zone_members[key]) for key in keys}
    quotas = {
        key: min(len(zone_members[key]), max(enforce_minimum, int(np.floor(raw[key]))))
        for key in keys
    }
    while sum(quotas.values()) < target:
        available = [key for key in keys if quotas[key] < len(zone_members[key])]
        if not available:
            break
        available.sort(key=lambda key: (-(raw[key] - np.floor(raw[key])), -len(zone_members[key]), key))
        for key in available:
            if sum(quotas.values()) >= target:
                break
            quotas[key] += 1
    while sum(quotas.values()) > target:
        available = [key for key in keys if quotas[key] > enforce_minimum]
        if not available:
            break
        available.sort(key=lambda key: (raw[key] - np.floor(raw[key]), len(zone_members[key]), key))
        for key in available:
            if sum(quotas.values()) <= target:
                break
            quotas[key] -= 1
    return quotas


def _spread_order(
    members: list[int],
    easting: np.ndarray,
    northing: np.ndarray,
    local_spacing: np.ndarray,
    site_ids: np.ndarray,
) -> list[int]:
    """Return a deterministic, well-spread order inside one local zone."""
    if len(members) <= 1:
        return list(members)
    indices = np.asarray(members, dtype=int)
    if len(indices) * len(indices) > 300_000:
        x = easting[indices]
        y = northing[indices]
        x_norm = (x - x.min()) / max(float(np.ptp(x)), 1e-9)
        y_norm = (y - y.min()) / max(float(np.ptp(y)), 1e-9)
        phase = np.mod(x_norm * 0.754877666 + y_norm * 0.569840291, 1.0)
        order = np.lexsort((site_ids[indices], phase))
        return indices[order].tolist()

    first_candidates = indices[local_spacing[indices] == np.max(local_spacing[indices])]
    first = int(min(first_candidates, key=lambda idx: str(site_ids[idx]).casefold()))
    chosen = [first]
    remaining = set(int(idx) for idx in indices if int(idx) != first)
    nearest_squared = np.full(len(easting), np.inf, dtype=float)
    while remaining:
        last = chosen[-1]
        active = np.fromiter(remaining, dtype=int)
        squared = (easting[active] - easting[last]) ** 2 + (northing[active] - northing[last]) ** 2
        nearest_squared[active] = np.minimum(nearest_squared[active], squared)
        maximum = float(np.max(nearest_squared[active]))
        candidates = active[np.isclose(nearest_squared[active], maximum)]
        best_spacing = float(np.max(local_spacing[candidates]))
        candidates = candidates[np.isclose(local_spacing[candidates], best_spacing)]
        chosen_index = int(min(candidates, key=lambda idx: str(site_ids[idx]).casefold()))
        chosen.append(chosen_index)
        remaining.remove(chosen_index)
    return chosen


def run_replication(
    operator_b_csv: str | Path,
    operator_x_csv: str | Path,
    config: ReplicationConfig,
) -> ReplicationResult:
    started = perf_counter()
    config.validate()
    load_started = perf_counter()
    b_sites, b_issues = load_sites(operator_b_csv, "B")
    x_sites, x_issues = load_sites(operator_x_csv, "X")
    if b_sites.empty:
        raise ValueError("Operator B CSV has no valid sites")
    if x_sites.empty:
        raise ValueError("Operator X CSV has no valid sites")
    load_seconds = perf_counter() - load_started

    spatial_started = perf_counter()
    b_points = latlon_to_unit(b_sites["latitude"].to_numpy(), b_sites["longitude"].to_numpy())
    x_points = latlon_to_unit(x_sites["latitude"].to_numpy(), x_sites["longitude"].to_numpy())
    b_tree, b_nearest_spacing, global_b_spacing = _nearest_spacing(b_points)
    x_tree, x_nearest_spacing, global_x_spacing = _nearest_spacing(x_points)
    local_x_spacing = _local_x_spacing(x_tree, x_points, x_nearest_spacing, config.x_local_k)

    b_chord, b_indices = _query(b_tree, x_points, min(config.b_local_k, len(b_sites)))
    nearest_b_distance = _chord_to_km(b_chord[:, 0])
    nearest_b_index = b_indices[:, 0]
    if len(b_sites) == 1:
        local_b_spacing = np.full(len(x_sites), global_b_spacing, dtype=float)
    else:
        local_values = b_nearest_spacing[b_indices]
        local_values = np.where(local_values > 0, local_values, np.nan)
        with np.errstate(all="ignore"):
            local_b_spacing = np.nanmedian(local_values, axis=1)
        local_b_spacing = np.where(np.isfinite(local_b_spacing), local_b_spacing, global_b_spacing)
    spatial_seconds = perf_counter() - spatial_started

    selection_started = perf_counter()
    easting, northing = _project_km(x_sites["latitude"].to_numpy(), x_sites["longitude"].to_numpy())
    cell_size_km = max(global_x_spacing * config.zone_scale, 0.05)
    columns = np.floor((easting - easting.min()) / cell_size_km).astype(int)
    rows = np.floor((northing - northing.min()) / cell_size_km).astype(int)
    zone_ids = np.asarray([f"{row}:{column}" for row, column in zip(rows, columns, strict=True)], dtype=object)
    zone_members: dict[str, list[int]] = {}
    for idx, zone_id in enumerate(zone_ids):
        zone_members.setdefault(str(zone_id), []).append(idx)
    fraction = config.retention_percent / 100.0
    target = min(len(x_sites), max(1, int(np.floor(len(x_sites) * fraction + 0.5))))
    quotas = _allocate_quotas(zone_members, target, fraction, config.min_per_zone)

    site_ids = x_sites["siteid"].astype(str).to_numpy()
    zone_orders = {
        zone_id: _spread_order(members, easting, northing, local_x_spacing, site_ids)
        for zone_id, members in zone_members.items()
    }
    overlap_threshold = (
        config.overlap_ratio * local_b_spacing
        if config.overlap_mode == "adaptive"
        else np.full(len(x_sites), config.overlap_km, dtype=float)
    )
    passes_overlap = nearest_b_distance >= overlap_threshold
    initial_selected = np.zeros(len(x_sites), dtype=bool)
    overlap_rejected = np.zeros(len(x_sites), dtype=bool)
    refill_selected = np.zeros(len(x_sites), dtype=bool)
    final_selected = np.zeros(len(x_sites), dtype=bool)
    zone_pick_order = np.zeros(len(x_sites), dtype=int)
    for zone_id, order in zone_orders.items():
        quota = quotas[zone_id]
        for pick, idx in enumerate(order, start=1):
            zone_pick_order[idx] = pick
        initial = order[:quota]
        initial_selected[initial] = True
        initial_pass = [idx for idx in initial if passes_overlap[idx]]
        initial_fail = [idx for idx in initial if not passes_overlap[idx]]
        final_selected[initial_pass] = True
        overlap_rejected[initial_fail] = True
        if config.refill_within_zone and len(initial_pass) < quota:
            for idx in order[quota:]:
                if len(initial_pass) + int(refill_selected[order].sum()) >= quota:
                    break
                if passes_overlap[idx]:
                    refill_selected[idx] = True
                    final_selected[idx] = True
                else:
                    overlap_rejected[idx] = True
    selection_seconds = perf_counter() - selection_started

    finalization_started = perf_counter()
    sites = pd.DataFrame({
        "operator_x_siteid": site_ids,
        "latitude": x_sites["latitude"].to_numpy(dtype=float),
        "longitude": x_sites["longitude"].to_numpy(dtype=float),
        "zone_id": zone_ids,
        "zone_site_count": np.asarray([len(zone_members[str(zone)]) for zone in zone_ids], dtype=int),
        "zone_target_count": np.asarray([quotas[str(zone)] for zone in zone_ids], dtype=int),
        "zone_pick_order": zone_pick_order,
        "local_x_spacing_km": local_x_spacing,
        "nearest_b_siteid": b_sites["siteid"].to_numpy()[nearest_b_index],
        "nearest_b_distance_km": nearest_b_distance,
        "local_b_spacing_km": local_b_spacing,
        "overlap_threshold_km": overlap_threshold,
        "initial_representative": initial_selected,
        "overlap_rejected": overlap_rejected,
        "refill_selected": refill_selected,
        "final_selected": final_selected,
    })
    sites["selection_rank"] = pd.Series(pd.NA, index=sites.index, dtype="Int64")
    final_order = sites.loc[sites["final_selected"]].sort_values(
        ["zone_id", "zone_pick_order", "operator_x_siteid"]
    ).index
    sites.loc[final_order, "selection_rank"] = np.arange(1, len(final_order) + 1)

    def reason(row: pd.Series) -> str:
        if bool(row["refill_selected"]):
            return "selected as a same-zone replacement after a B-overlap removal"
        if bool(row["final_selected"]):
            return "selected in the density-preserving X footprint sample and clear of B overlap"
        if bool(row["overlap_rejected"]):
            return "removed because the nearest B site is inside the configured overlap distance"
        return "not required by this zone's site-retention quota"

    sites["selection_reason"] = sites.apply(reason, axis=1)
    sites["status"] = np.select(
        [sites["final_selected"], sites["overlap_rejected"]],
        ["Selected X footprint", "B overlap removed"],
        default="X not retained",
    )
    sites = sites.sort_values(
        ["final_selected", "selection_rank", "overlap_rejected", "zone_id", "zone_pick_order"],
        ascending=[False, True, False, True, True],
        na_position="last",
    ).reset_index(drop=True)
    for column in [
        "local_x_spacing_km", "nearest_b_distance_km", "local_b_spacing_km", "overlap_threshold_km"
    ]:
        sites[column] = sites[column].round(6)
    issues = pd.concat([b_issues, x_issues], ignore_index=True)
    if issues.empty:
        issues = pd.DataFrame(columns=ISSUE_COLUMNS)
    finalization_seconds = perf_counter() - finalization_started
    timings = {
        "load_validation_seconds": round(load_seconds, 6),
        "spatial_context_seconds": round(spatial_seconds, 6),
        "replication_selection_seconds": round(selection_seconds, 6),
        "result_finalization_seconds": round(finalization_seconds, 6),
        "analysis_total_seconds": round(perf_counter() - started, 6),
    }
    return ReplicationResult(
        sites=sites,
        issues=issues,
        valid_b_sites=b_sites,
        valid_x_sites=x_sites,
        config=config,
        target_count=target,
        zone_count=len(zone_members),
        cell_size_km=float(cell_size_km),
        global_x_spacing_km=global_x_spacing,
        global_b_spacing_km=global_b_spacing,
        timings=timings,
    )
