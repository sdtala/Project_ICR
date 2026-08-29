"""Command-line interface for ICR site selection."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from time import perf_counter

from .dashboard import write_outputs
from .engine import AnalysisConfig, run_analysis


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prioritize Operator X sites that fill geographic gaps in Operator B's footprint.")
    parser.add_argument("--operator-b", required=True, help="CSV containing Operator B siteid, latitude, and longitude")
    parser.add_argument("--operator-x", required=True, help="CSV containing Operator X siteid, latitude, and longitude")
    parser.add_argument("--mode", required=True, choices=["fixed_count", "distance_threshold"])
    parser.add_argument("--criterion", choices=["absolute", "density"], default="density")
    parser.add_argument("--count", type=int, help="Number of sites to select in fixed_count mode")
    parser.add_argument("--prefilter", choices=["none", "absolute", "density"], default="none")
    parser.add_argument("--threshold", type=float, help="Kilometres for absolute filters or a ratio for density filters")
    parser.add_argument("--diversity-km", type=float, help="Absolute soft-diversity distance; omit for adaptive local B spacing")
    parser.add_argument("--local-k", type=int, default=5, help="Nearby B sites used to estimate local density (default: 5)")
    parser.add_argument("--x-local-k", type=int, default=5, help="Nearby X sites used to estimate candidate density (default: 5)")
    parser.add_argument("--diversity-weight", type=float, default=0.20, help="Penalty weight for closeness to selected X sites (default: 0.20)")
    parser.add_argument("--saturation-weight", type=float, default=0.10, help="Penalty weight for selected-X concentration (default: 0.10)")
    parser.add_argument("--min-separation-ratio", type=float, default=0.0, help="Hard X-to-X spacing divided by local B spacing; 0 disables")
    parser.add_argument("--threshold-portfolio", choices=["all_eligible", "declustered"], default="all_eligible", help="Threshold mode portfolio behavior")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--operator-b-name", default="Operator B")
    parser.add_argument("--operator-x-name", default="Operator X")
    parser.add_argument("--builder-root", help="Data Analytics plugin root containing the canonical portable HTML builder")
    parser.add_argument("--node-executable", help="Node.js executable used by the portable dashboard builder")
    return parser


def main(argv: list[str] | None = None) -> int:
    command_started = perf_counter()
    args = build_parser().parse_args(argv)
    config = AnalysisConfig(
        mode=args.mode,
        criterion=args.criterion,
        count=args.count,
        prefilter=args.prefilter,
        threshold=args.threshold,
        diversity_km=args.diversity_km,
        local_k=args.local_k,
        x_local_k=args.x_local_k,
        diversity_weight=args.diversity_weight,
        saturation_weight=args.saturation_weight,
        min_separation_ratio=args.min_separation_ratio,
        threshold_portfolio=args.threshold_portfolio,
    )
    result = run_analysis(Path(args.operator_b), Path(args.operator_x), config)
    output_started = perf_counter()
    outputs = write_outputs(
        result,
        args.output_dir,
        operator_b_name=args.operator_b_name,
        operator_x_name=args.operator_x_name,
        builder_root=args.builder_root,
        node_executable=args.node_executable,
    )
    timings = {
        **result.timings,
        "output_generation_seconds": round(perf_counter() - output_started, 6),
        "command_total_seconds": round(perf_counter() - command_started, 6),
    }
    print(json.dumps({
        "valid_b_sites": len(result.valid_b_sites),
        "valid_x_sites": len(result.valid_x_sites),
        "eligible_sites": int(result.ranked_sites["eligible"].sum()),
        "selected_sites": result.selected_count,
        "diversity_km": result.diversity_km,
        "timings": timings,
        "outputs": outputs,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
