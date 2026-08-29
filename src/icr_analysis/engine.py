"""Data validation, gap scoring, and portfolio selection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Literal

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

from .geo import EARTH_RADIUS_KM, latlon_to_unit

Criterion = Literal["absolute", "density"]
Mode = Literal["fixed_count", "distance_threshold"]
Prefilter = Literal["none", "absolute", "density"]
ThresholdPortfolio = Literal["all_eligible", "declustered"]


@dataclass(frozen=True)
class AnalysisConfig:
    mode: Mode
    criterion: Criterion = "density"
    count: int | None = None
    prefilter: Prefilter = "none"
    threshold: float | None = None
    diversity_km: float | None = None
    local_k: int = 5
    x_local_k: int = 5
    diversity_weight: float = 0.20
    saturation_weight: float = 0.10
    min_separation_ratio: float = 0.0
    threshold_portfolio: ThresholdPortfolio = "all_eligible"

    def validate(self) -> None:
        if self.mode not in {"fixed_count", "distance_threshold"}:
            raise ValueError("mode must be fixed_count or distance_threshold")
        if self.criterion not in {"absolute", "density"}:
            raise ValueError("criterion must be absolute or density")
        if self.prefilter not in {"none", "absolute", "density"}:
            raise ValueError("prefilter must be none, absolute, or density")
        if self.local_k < 1:
            raise ValueError("local_k must be at least 1")
        if self.x_local_k < 1:
            raise ValueError("x_local_k must be at least 1")
        if self.threshold_portfolio not in {"all_eligible", "declustered"}:
            raise ValueError("threshold_portfolio must be all_eligible or declustered")
        if not 0 <= self.diversity_weight <= 1:
            raise ValueError("diversity_weight must be between 0 and 1")
        if not 0 <= self.saturation_weight <= 1:
            raise ValueError("saturation_weight must be between 0 and 1")
        if self.diversity_weight + self.saturation_weight > 1:
            raise ValueError("diversity_weight plus saturation_weight cannot exceed 1")
        if self.min_separation_ratio < 0:
            raise ValueError("min_separation_ratio cannot be negative")
        if self.mode == "fixed_count" and (self.count is None or self.count < 1):
            raise ValueError("fixed_count mode requires count >= 1")
        needs_threshold = self.mode == "distance_threshold" or self.prefilter != "none"
        if needs_threshold and (self.threshold is None or self.threshold < 0):
            raise ValueError("the selected mode/filter requires threshold >= 0")
        if self.diversity_km is not None and self.diversity_km < 0:
            raise ValueError("diversity_km cannot be negative")
        if self.mode == "distance_threshold" and self.threshold_portfolio == "declustered" and self.min_separation_ratio <= 0:
            raise ValueError("de-clustered threshold mode requires min_separation_ratio > 0")


@dataclass
class AnalysisResult:
    ranked_sites: pd.DataFrame
    issues: pd.DataFrame
    valid_b_sites: pd.DataFrame
    valid_x_sites: pd.DataFrame
    config: AnalysisConfig
    diversity_km: float
    global_b_spacing_km: float
    timings: dict[str, float]

    @property
    def selected_count(self) -> int:
        return int(self.ranked_sites["selected"].sum())


ISSUE_COLUMNS = ["operator", "row_number", "siteid", "issue", "action"]


def load_sites(path: str | Path, operator: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    frame = pd.read_csv(path, dtype=str)
    normalized = {str(column).strip().lower(): column for column in frame.columns}
    required = {"siteid", "latitude", "longitude"}
    missing = sorted(required - normalized.keys())
    if missing:
        raise ValueError(f"{operator} CSV is missing required column(s): {', '.join(missing)}")

    sites = frame[[normalized["siteid"], normalized["latitude"], normalized["longitude"]]].copy()
    sites.columns = ["siteid", "latitude", "longitude"]
    sites["row_number"] = np.arange(2, len(sites) + 2)
    sites["siteid"] = sites["siteid"].fillna("").astype(str).str.strip()
    sites["latitude"] = pd.to_numeric(sites["latitude"], errors="coerce")
    sites["longitude"] = pd.to_numeric(sites["longitude"], errors="coerce")

    issues: list[dict[str, object]] = []
    valid_mask = pd.Series(True, index=sites.index)
    checks = [
        (sites["siteid"].eq(""), "missing_siteid"),
        (sites["latitude"].isna() | sites["longitude"].isna(), "invalid_coordinate"),
        ((sites["latitude"] < -90) | (sites["latitude"] > 90), "latitude_out_of_range"),
        ((sites["longitude"] < -180) | (sites["longitude"] > 180), "longitude_out_of_range"),
    ]
    for mask, issue in checks:
        mask = mask & valid_mask
        for row in sites.loc[mask].itertuples():
            issues.append({"operator": operator, "row_number": row.row_number, "siteid": row.siteid, "issue": issue, "action": "excluded"})
        valid_mask &= ~mask

    valid = sites.loc[valid_mask].copy()
    duplicate_id = valid["siteid"].str.casefold().duplicated(keep="first")
    duplicate_coord = valid[["latitude", "longitude"]].duplicated(keep="first") & ~duplicate_id
    for mask, issue in [(duplicate_id, "duplicate_siteid"), (duplicate_coord, "duplicate_coordinate")]:
        for row in valid.loc[mask].itertuples():
            issues.append({"operator": operator, "row_number": row.row_number, "siteid": row.siteid, "issue": issue, "action": "excluded"})
    valid = valid.loc[~(duplicate_id | duplicate_coord), ["siteid", "latitude", "longitude", "row_number"]].reset_index(drop=True)
    issue_frame = pd.DataFrame(issues, columns=ISSUE_COLUMNS)
    return valid, issue_frame


def _chord_to_km(chord: np.ndarray | float) -> np.ndarray:
    values = np.asarray(chord, dtype=float)
    return 2.0 * EARTH_RADIUS_KM * np.arcsin(np.clip(values / 2.0, 0.0, 1.0))


def _query_tree(tree: cKDTree, points: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    distances, indices = tree.query(points, k=k, workers=-1)
    if k == 1:
        distances = np.asarray(distances)[:, None]
        indices = np.asarray(indices)[:, None]
    return np.asarray(distances, dtype=float), np.asarray(indices, dtype=int)


def _spatial_context(
    b_sites: pd.DataFrame,
    x_sites: pd.DataFrame,
    local_k: int,
    x_local_k: int,
) -> tuple[pd.DataFrame, float]:
    """Calculate all B-gap and local-density metrics in batch on the unit sphere."""
    b_points = latlon_to_unit(b_sites["latitude"].to_numpy(), b_sites["longitude"].to_numpy())
    x_points = latlon_to_unit(x_sites["latitude"].to_numpy(), x_sites["longitude"].to_numpy())
    b_tree = cKDTree(b_points)

    if len(b_sites) == 1:
        b_spacing = np.array([0.0])
    else:
        b_self_chord, _ = _query_tree(b_tree, b_points, 2)
        b_spacing = _chord_to_km(b_self_chord[:, 1])
    positive_spacing = b_spacing[b_spacing > 0]
    global_spacing = float(np.median(positive_spacing)) if len(positive_spacing) else 1.0

    effective_b_k = min(local_k, len(b_sites))
    b_chord, b_indices = _query_tree(b_tree, x_points, effective_b_k)
    nearest_b_distance = _chord_to_km(b_chord[:, 0])
    nearest_b_index = b_indices[:, 0]
    if len(positive_spacing):
        local_b_values = b_spacing[b_indices]
        local_b_values = np.where(local_b_values > 0, local_b_values, np.nan)
        with np.errstate(all="ignore"):
            local_b_spacing = np.nanmedian(local_b_values, axis=1)
        local_b_spacing = np.where(np.isfinite(local_b_spacing), local_b_spacing, global_spacing)
    else:
        local_b_spacing = np.full(len(x_sites), global_spacing, dtype=float)

    if len(x_sites) == 1:
        local_x_spacing = np.array([global_spacing], dtype=float)
    else:
        x_tree = cKDTree(x_points)
        x_self_chord, _ = _query_tree(x_tree, x_points, 2)
        x_nearest_spacing = _chord_to_km(x_self_chord[:, 1])
        effective_x_k = min(x_local_k, len(x_sites))
        _, x_indices = _query_tree(x_tree, x_points, effective_x_k)
        local_x_spacing = np.median(x_nearest_spacing[x_indices], axis=1)

    density_ratio = np.divide(
        nearest_b_distance,
        local_b_spacing,
        out=np.zeros_like(nearest_b_distance),
        where=local_b_spacing > 0,
    )
    x_to_b_ratio = np.divide(
        local_x_spacing,
        local_b_spacing,
        out=np.zeros_like(local_x_spacing),
        where=local_b_spacing > 0,
    )
    candidates = pd.DataFrame({
        "operator_x_siteid": x_sites["siteid"].to_numpy(),
        "latitude": x_sites["latitude"].to_numpy(dtype=float),
        "longitude": x_sites["longitude"].to_numpy(dtype=float),
        "nearest_b_siteid": b_sites["siteid"].to_numpy()[nearest_b_index],
        "nearest_b_distance_km": nearest_b_distance,
        "local_b_spacing_km": local_b_spacing,
        "density_gap_ratio": density_ratio,
        "local_x_spacing_km": local_x_spacing,
        "x_to_b_spacing_ratio": x_to_b_ratio,
    })
    return candidates, global_spacing


def _percentile_score(values: pd.Series) -> pd.Series:
    if len(values) <= 1 or values.nunique(dropna=True) <= 1:
        return pd.Series(np.ones(len(values)), index=values.index, dtype=float)
    return values.rank(method="average", pct=True).astype(float)


def _eligible_mask(candidates: pd.DataFrame, filter_type: Prefilter | Criterion, threshold: float | None) -> pd.Series:
    if filter_type == "none":
        return pd.Series(True, index=candidates.index)
    if threshold is None:
        raise ValueError("threshold is required for an eligibility filter")
    field = "nearest_b_distance_km" if filter_type == "absolute" else "density_gap_ratio"
    return candidates[field] >= threshold


def _portfolio_order(
    candidates: pd.DataFrame,
    eligible_indices: list[int],
    config: AnalysisConfig,
) -> tuple[list[int], dict[int, dict[str, float]], set[int]]:
    if not eligible_indices:
        return [], {}, set()
    points = latlon_to_unit(candidates["latitude"].to_numpy(), candidates["longitude"].to_numpy())
    remaining = np.zeros(len(candidates), dtype=bool)
    remaining[eligible_indices] = True
    selected: list[int] = []
    metrics: dict[int, dict[str, float]] = {}
    redundancy_filtered: set[int] = set()
    nearest_selected = np.full(len(candidates), np.inf, dtype=float)
    selected_neighbor_counts = np.zeros(len(candidates), dtype=int)
    diversity_targets = (
        np.full(len(candidates), config.diversity_km, dtype=float)
        if config.diversity_km is not None
        else candidates["local_b_spacing_km"].to_numpy(dtype=float)
    )
    hard_separation_ratio = (
        config.min_separation_ratio
        if config.mode == "fixed_count" or config.threshold_portfolio == "declustered"
        else 0.0
    )

    base_scores = candidates["base_score"].to_numpy(dtype=float)
    local_b_spacing = np.maximum(candidates["local_b_spacing_km"].to_numpy(dtype=float), 1e-9)
    site_keys = candidates["operator_x_siteid"].astype(str).str.casefold().to_numpy()

    def values(indices: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        targets = diversity_targets[indices]
        if not selected:
            diversity_factors = np.ones(len(indices), dtype=float)
            neighbour_counts = np.zeros(len(indices), dtype=int)
            nearest_values = np.full(len(indices), np.nan, dtype=float)
        else:
            nearest_values = nearest_selected[indices]
            diversity_factors = np.where(
                targets > 0,
                np.minimum(1.0, nearest_values / np.maximum(targets, 1e-9)),
                1.0,
            )
            neighbour_counts = selected_neighbor_counts[indices]
        saturation_factors = 1.0 / (1.0 + neighbour_counts)
        marginal_scores = base_scores[indices] * (
            1.0
            - config.diversity_weight * (1.0 - diversity_factors)
            - config.saturation_weight * (1.0 - saturation_factors)
        )
        return diversity_factors, saturation_factors, marginal_scores

    def record(indices: np.ndarray, force_zero: bool = False) -> None:
        if len(indices) == 0:
            return
        diversity_factors, saturation_factors, marginal_scores = values(indices)
        for position, idx in enumerate(indices):
            nearest_value = nearest_selected[idx] if selected else np.nan
            metrics[int(idx)] = {
                "nearest_selected_x_distance_km": float(nearest_value),
                "selected_x_spacing_ratio": float(nearest_value / local_b_spacing[idx]) if selected else np.nan,
                "selected_x_neighbors": float(selected_neighbor_counts[idx] if selected else 0),
                "diversity_factor": 0.0 if force_zero else float(diversity_factors[position]),
                "saturation_factor": float(saturation_factors[position]),
                "marginal_score": 0.0 if force_zero else float(marginal_scores[position]),
            }

    while bool(remaining.any()):
        if selected and hard_separation_ratio > 0:
            hard_mask = remaining & ((nearest_selected / local_b_spacing) < hard_separation_ratio)
            hard_filtered = np.flatnonzero(hard_mask)
            record(hard_filtered, force_zero=True)
            remaining[hard_filtered] = False
            redundancy_filtered.update(int(idx) for idx in hard_filtered)
        if not bool(remaining.any()):
            break

        active = np.flatnonzero(remaining)
        _, _, marginal_scores = values(active)
        best = active[marginal_scores == np.max(marginal_scores)]
        if len(best) > 1:
            best = best[base_scores[best] == np.max(base_scores[best])]
        chosen = int(min(best, key=lambda idx: site_keys[idx]))
        record(np.array([chosen], dtype=int))
        selected.append(chosen)
        remaining[chosen] = False
        difference = points - points[chosen]
        chord = np.sqrt(np.einsum("ij,ij->i", difference, difference))
        distances = 2.0 * EARTH_RADIUS_KM * np.arcsin(np.clip(chord / 2.0, 0.0, 1.0))
        nearest_selected = np.minimum(nearest_selected, distances)
        selected_neighbor_counts += ((diversity_targets > 0) & (distances <= diversity_targets)).astype(int)
        if config.mode == "fixed_count" and len(selected) >= int(config.count or 0):
            record(np.flatnonzero(remaining))
            break
    return selected, metrics, redundancy_filtered


def _all_eligible_portfolio(
    candidates: pd.DataFrame,
    eligible_indices: list[int],
    config: AnalysisConfig,
) -> tuple[list[int], dict[int, dict[str, float]], set[int]]:
    """Select threshold-qualified sites directly and calculate final density diagnostics in batch."""
    if not eligible_indices:
        return [], {}, set()
    order = sorted(
        eligible_indices,
        key=lambda idx: (-float(candidates.at[idx, "base_score"]), str(candidates.at[idx, "operator_x_siteid"]).casefold()),
    )
    eligible = np.asarray(order, dtype=int)
    points = latlon_to_unit(
        candidates.loc[eligible, "latitude"].to_numpy(),
        candidates.loc[eligible, "longitude"].to_numpy(),
    )
    targets = (
        np.full(len(eligible), config.diversity_km, dtype=float)
        if config.diversity_km is not None
        else candidates.loc[eligible, "local_b_spacing_km"].to_numpy(dtype=float)
    )
    if len(eligible) == 1:
        nearest = np.array([np.nan])
        neighbour_counts = np.array([0], dtype=int)
    else:
        tree = cKDTree(points)
        chord, _ = _query_tree(tree, points, 2)
        nearest = _chord_to_km(chord[:, 1])
        chord_targets = 2.0 * np.sin(np.clip(targets / (2.0 * EARTH_RADIUS_KM), 0.0, np.pi / 2.0))
        neighbour_counts = np.asarray(
            tree.query_ball_point(points, chord_targets, return_length=True, workers=-1),
            dtype=int,
        ) - 1
    diversity_factors = np.where(
        (targets > 0) & np.isfinite(nearest),
        np.minimum(1.0, nearest / np.maximum(targets, 1e-9)),
        1.0,
    )
    saturation_factors = 1.0 / (1.0 + neighbour_counts)
    base_scores = candidates.loc[eligible, "base_score"].to_numpy(dtype=float)
    marginal_scores = base_scores * (
        1.0
        - config.diversity_weight * (1.0 - diversity_factors)
        - config.saturation_weight * (1.0 - saturation_factors)
    )
    local_b_spacing = np.maximum(candidates.loc[eligible, "local_b_spacing_km"].to_numpy(dtype=float), 1e-9)
    metrics: dict[int, dict[str, float]] = {}
    for position, idx in enumerate(eligible):
        metrics[int(idx)] = {
            "nearest_selected_x_distance_km": float(nearest[position]),
            "selected_x_spacing_ratio": float(nearest[position] / local_b_spacing[position]),
            "selected_x_neighbors": float(neighbour_counts[position]),
            "diversity_factor": float(diversity_factors[position]),
            "saturation_factor": float(saturation_factors[position]),
            "marginal_score": float(marginal_scores[position]),
        }
    return order, metrics, set()


def run_analysis(operator_b_csv: str | Path, operator_x_csv: str | Path, config: AnalysisConfig) -> AnalysisResult:
    analysis_started = perf_counter()
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
    candidates, global_spacing = _spatial_context(b_sites, x_sites, config.local_k, config.x_local_k)
    score_field = "nearest_b_distance_km" if config.criterion == "absolute" else "density_gap_ratio"
    candidates["base_score"] = _percentile_score(candidates[score_field])
    spatial_seconds = perf_counter() - spatial_started

    portfolio_started = perf_counter()
    filter_type: Prefilter | Criterion = config.criterion if config.mode == "distance_threshold" else config.prefilter
    eligible = _eligible_mask(candidates, filter_type, config.threshold)
    candidates["eligible"] = eligible
    diversity_km = global_spacing if config.diversity_km is None else config.diversity_km
    select_all_threshold = config.mode == "distance_threshold" and config.threshold_portfolio == "all_eligible"
    portfolio_function = _all_eligible_portfolio if select_all_threshold else _portfolio_order
    order, portfolio_metrics, redundancy_filtered = portfolio_function(candidates, list(candidates.index[eligible]), config)
    limit = len(order) if config.mode == "distance_threshold" else min(int(config.count or 0), len(order))
    selected_order = order[:limit]
    portfolio_seconds = perf_counter() - portfolio_started

    finalization_started = perf_counter()
    candidates["selected"] = False
    candidates.loc[selected_order, "selected"] = True
    candidates["selection_rank"] = pd.Series(pd.NA, index=candidates.index, dtype="Int64")
    for rank, idx in enumerate(selected_order, start=1):
        candidates.at[idx, "selection_rank"] = rank
    candidates["redundancy_filtered"] = candidates.index.isin(redundancy_filtered)
    metric_defaults = {
        "nearest_selected_x_distance_km": np.nan,
        "selected_x_spacing_ratio": np.nan,
        "selected_x_neighbors": 0.0,
        "diversity_factor": 1.0,
        "saturation_factor": 1.0,
        "marginal_score": 0.0,
    }
    for field, default in metric_defaults.items():
        candidates[field] = candidates.index.map(lambda idx, name=field, value=default: portfolio_metrics.get(idx, {}).get(name, value)).astype(float)
    candidates["selected_x_neighbors"] = candidates["selected_x_neighbors"].astype(int)
    candidates["diversity_adjusted_score"] = candidates["marginal_score"]

    def reason(row: pd.Series) -> str:
        if bool(row["selected"]):
            label = "absolute gap" if config.criterion == "absolute" else "density-adjusted gap"
            if select_all_threshold:
                return f"selected by {label} threshold; final portfolio-density diagnostics calculated in batch"
            return f"selected for {label}; adaptive portfolio priority {int(row['selection_rank'])}"
        if not bool(row["eligible"]):
            return f"excluded by {filter_type} threshold"
        if bool(row["redundancy_filtered"]):
            return f"excluded by minimum X-to-X separation ratio {config.min_separation_ratio:g}"
        return "eligible but below the fixed-count portfolio cut"

    candidates["selection_reason"] = candidates.apply(reason, axis=1)
    base_order = candidates.sort_values(["base_score", "operator_x_siteid"], ascending=[False, True]).index
    candidate_rank = pd.Series(np.arange(1, len(candidates) + 1), index=base_order)
    candidates["candidate_rank"] = candidate_rank.reindex(candidates.index).astype(int)
    candidates = candidates.sort_values(
        ["selected", "selection_rank", "base_score", "operator_x_siteid"],
        ascending=[False, True, False, True],
        na_position="last",
    ).reset_index(drop=True)
    candidates = candidates[[
        "candidate_rank", "selection_rank", "selected", "eligible", "operator_x_siteid", "latitude", "longitude",
        "nearest_b_siteid", "nearest_b_distance_km", "local_b_spacing_km", "density_gap_ratio",
        "local_x_spacing_km", "x_to_b_spacing_ratio", "base_score", "nearest_selected_x_distance_km",
        "selected_x_spacing_ratio", "selected_x_neighbors", "diversity_factor", "saturation_factor",
        "marginal_score", "diversity_adjusted_score", "redundancy_filtered", "selection_reason",
    ]]
    for column in [
        "nearest_b_distance_km", "local_b_spacing_km", "density_gap_ratio", "local_x_spacing_km",
        "x_to_b_spacing_ratio", "base_score", "nearest_selected_x_distance_km", "selected_x_spacing_ratio",
        "diversity_factor", "saturation_factor", "marginal_score", "diversity_adjusted_score",
    ]:
        candidates[column] = candidates[column].round(6)

    issues = pd.concat([b_issues, x_issues], ignore_index=True)
    finalization_seconds = perf_counter() - finalization_started
    timings = {
        "load_validation_seconds": round(load_seconds, 6),
        "spatial_scoring_seconds": round(spatial_seconds, 6),
        "portfolio_selection_seconds": round(portfolio_seconds, 6),
        "result_finalization_seconds": round(finalization_seconds, 6),
        "analysis_total_seconds": round(perf_counter() - analysis_started, 6),
    }
    return AnalysisResult(candidates, issues, b_sites, x_sites, config, float(diversity_km), global_spacing, timings)
