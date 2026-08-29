"""Generate deterministic site inventories and time the in-memory ICR engine."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
from time import perf_counter

import numpy as np
import pandas as pd

from icr_analysis.dashboard import build_dashboard_payload
from icr_analysis.engine import AnalysisConfig, run_analysis


def generated_sites(prefix: str, count: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return pd.DataFrame({
        "siteid": [f"{prefix}{number:05d}" for number in range(1, count + 1)],
        "latitude": rng.uniform(8.0, 36.0, count),
        "longitude": rng.uniform(68.0, 97.0, count),
    })


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark ICR selection at operational site volumes.")
    parser.add_argument("--b-sites", type=int, default=6000)
    parser.add_argument("--x-sites", type=int, default=12000)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--mode", choices=["fixed_count", "distance_threshold"], default="fixed_count")
    parser.add_argument("--threshold-portfolio", choices=["all_eligible", "declustered"], default="all_eligible")
    parser.add_argument("--min-separation-ratio", type=float, default=0.5)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix=".icr-benchmark-", dir=Path.cwd()) as temporary:
        root = Path(temporary)
        b_path = root / "operator_b.csv"
        x_path = root / "operator_x.csv"
        generated_sites("B", args.b_sites, 20260828).to_csv(b_path, index=False)
        generated_sites("X", args.x_sites, 20260829).to_csv(x_path, index=False)
        config = (
            AnalysisConfig(mode="fixed_count", count=args.count, criterion="density")
            if args.mode == "fixed_count"
            else AnalysisConfig(
                mode="distance_threshold", threshold=1.0, criterion="density",
                threshold_portfolio=args.threshold_portfolio,
                min_separation_ratio=args.min_separation_ratio if args.threshold_portfolio == "declustered" else 0.0,
            )
        )
        started = perf_counter()
        result = run_analysis(b_path, x_path, config)
        elapsed = perf_counter() - started
        payload_started = perf_counter()
        payload = build_dashboard_payload(result)
        payload_json = json.dumps(payload, separators=(",", ":"))
        payload_elapsed = perf_counter() - payload_started

    print(json.dumps({
        "b_sites": args.b_sites,
        "x_sites": args.x_sites,
        "mode": args.mode,
        "requested_count": args.count if args.mode == "fixed_count" else None,
        "threshold_portfolio": args.threshold_portfolio if args.mode == "distance_threshold" else None,
        "selected_sites": result.selected_count,
        "elapsed_seconds": round(elapsed, 3),
        "engine_timings": getattr(result, "timings", None),
        "dashboard_payload_seconds": round(payload_elapsed, 3),
        "dashboard_payload_megabytes": round(len(payload_json.encode("utf-8")) / (1024 * 1024), 3),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
