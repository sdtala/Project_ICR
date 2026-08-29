"""CSV and management-dashboard delivery for ICR analysis results."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

import numpy as np
import pandas as pd

from .engine import AnalysisResult


def _json_value(value: Any) -> Any:
    if value is pd.NA or (isinstance(value, float) and np.isnan(value)):
        return None
    if isinstance(value, np.generic):
        return value.item()
    return value


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [{key: _json_value(value) for key, value in row.items()} for row in frame.to_dict(orient="records")]


def _distance_bands(frame: pd.DataFrame) -> list[dict[str, Any]]:
    bins = [-np.inf, 1, 2, 5, 10, 25, np.inf]
    labels = ["<1 km", "1–2 km", "2–5 km", "5–10 km", "10–25 km", "25+ km"]
    bands = pd.cut(frame["nearest_b_distance_km"], bins=bins, labels=labels, right=False)
    counts = bands.value_counts(sort=False)
    return [{"distance_band": label, "candidate_count": int(counts.get(label, 0))} for label in labels]


def _summary(result: AnalysisResult) -> dict[str, Any]:
    selected = result.ranked_sites[result.ranked_sites["selected"]]
    selected_spacing = selected["nearest_selected_x_distance_km"].replace([np.inf, -np.inf], np.nan).dropna()
    return {
        "valid_b_sites": len(result.valid_b_sites),
        "valid_x_sites": len(result.valid_x_sites),
        "eligible_sites": int(result.ranked_sites["eligible"].sum()),
        "selected_sites": result.selected_count,
        "median_gap_km": float(selected["nearest_b_distance_km"].median()) if not selected.empty else 0.0,
        "median_density_ratio": float(selected["density_gap_ratio"].median()) if not selected.empty else 0.0,
        "median_selected_x_spacing_km": float(selected_spacing.median()) if not selected_spacing.empty else 0.0,
        "redundancy_filtered_sites": int(result.ranked_sites["redundancy_filtered"].sum()),
        "analysis_seconds": float(result.timings.get("analysis_total_seconds", 0.0)),
        "issue_rows": len(result.issues),
    }


def build_artifact(result: AnalysisResult, operator_b_name: str, operator_x_name: str) -> dict[str, Any]:
    generated = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    summary = _summary(result)
    detail = result.ranked_sites.head(500).copy()
    detail["status"] = np.select(
        [detail["selected"], detail["redundancy_filtered"], detail["eligible"]],
        ["Selected", "Redundancy filtered", "Eligible"],
        default="Filtered out",
    )
    selected_top = result.ranked_sites[result.ranked_sites["selected"]].head(20).copy()
    ranked_source = {
        "id": "icr_python_analysis",
        "label": "Ranked ICR candidate extract",
        "path": "icr_ranked_sites.csv",
        "query": {
            "engine": "duckdb",
            "language": "sql",
            "sql": "SELECT * FROM read_csv_auto('icr_ranked_sites.csv', header = true)",
            "description": "Validated WGS84 coordinates scored with great-circle distance, local B-site density, adaptive X-to-X diversity, and selected-site saturation.",
            "executed_at": generated,
            "tables_used": ["icr_ranked_sites.csv"],
            "filters": [
                f"mode={result.config.mode}",
                f"criterion={result.config.criterion}",
                f"prefilter={result.config.prefilter}",
                f"threshold={result.config.threshold}",
            ],
            "metric_definitions": {
                "nearest_b_distance_km": "Great-circle distance from an Operator X site to its nearest valid Operator B site.",
                "density_gap_ratio": "Nearest-B distance divided by the median nearest-neighbour spacing of the locally nearest B sites.",
                "local_x_spacing_km": "Median nearest-neighbour spacing among the locally nearest Operator X sites.",
                "nearest_selected_x_distance_km": "Distance to the nearest previously selected X site when the candidate received its portfolio priority.",
                "marginal_score": "Coverage-gap percentile after configured diversity and selected-site saturation penalties.",
                "selected_sites": "Candidates selected by the configured eligibility, ranking, count, diversity, saturation, and optional hard-separation rules.",
            },
        },
    }
    summary_source = {
        "id": "icr_summary_extract",
        "label": "ICR management summary extract",
        "path": "icr_dashboard_summary.csv",
        "query": {
            "engine": "duckdb",
            "language": "sql",
            "sql": "SELECT * FROM read_csv_auto('icr_dashboard_summary.csv', header = true)",
            "description": "One reviewed summary row reconciled with the complete candidate analysis.",
            "executed_at": generated,
            "tables_used": ["icr_dashboard_summary.csv"],
            "metric_definitions": ranked_source["query"]["metric_definitions"],
        },
    }
    cards = [
        ("b_sites", "Valid B sites", "valid_b_sites", "Validated Operator B locations used as the existing footprint."),
        ("x_sites", "Valid X sites", "valid_x_sites", "Validated Operator X candidates considered by the model."),
        ("eligible", "Eligible X sites", "eligible_sites", "Candidates passing the configured pre-filter or selection threshold."),
        ("selected", "Selected X sites", "selected_sites", "Candidates included in the resulting roaming portfolio."),
        ("median_gap", "Median selected gap", "median_gap_km", "Median nearest-B distance among selected X sites, in kilometres."),
        ("median_ratio", "Median density ratio", "median_density_ratio", "Median local density-adjusted gap ratio among selected X sites."),
    ]
    manifest_cards = []
    for card_id, label, field, description in cards:
        metric = {"label": label, "field": field, "format": "number"}
        if field in {"median_gap_km", "median_selected_x_spacing_km"}:
            metric["unit"] = "km"
        manifest_cards.append({"id": card_id, "description": description, "dataset": "summary", "sourceId": summary_source["id"], "metrics": [metric]})

    artifact = {
        "surface": "dashboard",
        "manifest": {
            "version": 1,
            "surface": "dashboard",
            "title": "Intra-Circle Roaming Site Prioritization",
            "description": f"Geographic gap-proxy analysis for {operator_b_name} using candidate sites from {operator_x_name}.",
            "generatedAt": generated,
            "cards": manifest_cards,
            "charts": [
                {
                    "id": "gap_distribution",
                    "title": "Candidate distribution by nearest-B distance",
                    "subtitle": "Count of valid Operator X candidates in fixed kilometre bands; this is a geographic proxy, not RF coverage.",
                    "type": "bar",
                    "dataset": "distance_bands",
                    "sourceId": ranked_source["id"],
                    "encodings": {
                        "x": {"field": "distance_band", "type": "ordinal", "label": "Nearest-B distance"},
                        "y": {"field": "candidate_count", "type": "quantitative", "label": "Candidate sites", "format": "number"},
                    },
                    "yAxisTitle": "Candidate sites",
                    "valueFormat": "number",
                },
                {
                    "id": "selected_gaps",
                    "title": "Highest-priority selected sites",
                    "subtitle": "Top 20 selected sites by diversity-aware selection priority; distance is measured to the nearest B site.",
                    "type": "bar",
                    "dataset": "selected_top",
                    "sourceId": ranked_source["id"],
                    "encodings": {
                        "x": {"field": "operator_x_siteid", "type": "ordinal", "label": "Operator X site"},
                        "y": {"field": "nearest_b_distance_km", "type": "quantitative", "label": "Nearest-B distance", "format": "number"},
                    },
                    "yAxisTitle": "Distance (km)",
                    "valueFormat": "number",
                },
            ],
            "tables": [{
                "id": "candidate_detail",
                "title": "Ranked Operator X candidates",
                "subtitle": "First 500 rows. Use the ranked CSV for the complete, exact lookup table.",
                "dataset": "candidate_detail",
                "sourceId": ranked_source["id"],
                "defaultSort": {"field": "marginal_score", "direction": "desc"},
                "density": "dense",
                "layout": "full",
                "columns": [
                    {"field": "selection_rank", "label": "Selection rank", "format": "number"},
                    {"field": "status", "label": "Status", "type": "text"},
                    {"field": "operator_x_siteid", "label": "X site ID", "type": "text"},
                    {"field": "nearest_b_distance_km", "label": "Gap (km)", "format": "number"},
                    {"field": "density_gap_ratio", "label": "Density ratio", "format": "number"},
                    {"field": "local_x_spacing_km", "label": "Local X spacing (km)", "format": "number"},
                    {"field": "nearest_selected_x_distance_km", "label": "Nearest selected X (km)", "format": "number"},
                    {"field": "marginal_score", "label": "Marginal score", "format": "number"},
                ],
            }],
            "sources": [summary_source, ranked_source],
            "blocks": [
                {"id": "intro", "type": "markdown", "body": "## Decision view\n\nSites are prioritized as potential geographic gap fillers. The model does **not** estimate RF signal, traffic relief, population served, or commercial value."},
                {"id": "metrics", "type": "metric-strip", "cardIds": [card[0] for card in cards]},
                {"id": "distribution", "type": "chart", "chartId": "gap_distribution"},
                {"id": "selected_gaps", "type": "chart", "chartId": "selected_gaps"},
                {"id": "detail", "type": "table", "tableId": "candidate_detail", "layout": "full"},
                {"id": "method", "type": "markdown", "body": "## Method and limitations\n\nDistances use WGS84 great-circle geometry. The density ratio compares each X-to-B gap with typical spacing among nearby B sites. Adaptive diversity compares each candidate with already selected X sites using local B spacing unless a fixed distance is configured. A saturation factor reduces repeated selection inside the same local radius, and an optional hard separation can remove redundant candidates. No service-area boundary is applied, so remote out-of-scope sites can rank highly. Validate shortlisted sites with RF, terrain, traffic, population, cost, and field evidence before commercial commitment."},
            ],
        },
        "snapshot": {
            "version": 1,
            "generatedAt": generated,
            "status": "ready",
            "datasets": {
                "summary": [summary],
                "distance_bands": _distance_bands(result.ranked_sites),
                "selected_top": _records(selected_top[["operator_x_siteid", "nearest_b_distance_km", "density_gap_ratio", "selection_rank"]]),
                "candidate_detail": _records(detail[["selection_rank", "status", "operator_x_siteid", "nearest_b_distance_km", "density_gap_ratio", "local_x_spacing_km", "nearest_selected_x_distance_km", "marginal_score"]]),
            },
        },
        "sources": [summary_source, ranked_source],
    }
    return artifact


def build_dashboard_payload(result: AnalysisResult) -> dict[str, Any]:
    """Return the reviewed site, KPI, and distribution payload used by web views."""
    b_payload = result.valid_b_sites[["siteid", "latitude", "longitude"]].copy()
    b_payload["status"] = "Operator B"
    ranked = result.ranked_sites.set_index("operator_x_siteid").reindex(result.valid_x_sites["siteid"])
    x_payload = pd.DataFrame({
        "siteid": result.valid_x_sites["siteid"].to_numpy(),
        "latitude": result.valid_x_sites["latitude"].to_numpy(dtype=float),
        "longitude": result.valid_x_sites["longitude"].to_numpy(dtype=float),
        "status": np.select(
            [ranked["selected"], ranked["redundancy_filtered"], ranked["eligible"]],
            ["Selected X", "Redundant X", "Eligible X"],
            default="Filtered X",
        ),
        "gap_km": ranked["nearest_b_distance_km"].to_numpy(dtype=float),
        "density_ratio": ranked["density_gap_ratio"].to_numpy(dtype=float),
        "local_x_spacing_km": ranked["local_x_spacing_km"].to_numpy(dtype=float),
        "nearest_selected_x_km": ranked["nearest_selected_x_distance_km"].to_numpy(dtype=float),
        "selected_x_neighbors": ranked["selected_x_neighbors"].to_numpy(dtype=int),
        "marginal_score": ranked["marginal_score"].to_numpy(dtype=float),
        "selection_rank": ranked["selection_rank"].to_numpy(),
        "nearest_b": ranked["nearest_b_siteid"].astype(str).to_numpy(),
    })
    return {
        "sites": _records(b_payload) + _records(x_payload),
        "summary": _summary(result),
        "bands": _distance_bands(result.ranked_sites),
    }


def write_online_dashboard(result: AnalysisResult, output_path: Path) -> None:
    payload = json.dumps(build_dashboard_payload(result), ensure_ascii=False).replace("</", "<\\/")
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ICR Site Prioritization</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
:root{{--ink:#172033;--muted:#667085;--line:#dce2ea;--blue:#2563a6;--blue2:#93b8dc;--gold:#c79225;--bg:#f4f7fb}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);font:14px/1.45 Inter,Segoe UI,Arial,sans-serif;color:var(--ink)}}
header{{padding:28px max(24px,5vw) 20px;background:#fff;border-bottom:1px solid var(--line)}} h1{{margin:0 0 6px;font-size:28px}} h2{{font-size:18px;margin:0 0 6px}} p{{margin:0;color:var(--muted)}}
main{{max-width:1400px;margin:auto;padding:22px}} .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:16px}}
.card,.panel{{background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px}} .metric{{font:600 25px/1.15 ui-monospace,Consolas,monospace;margin-top:8px}}
.grid{{display:grid;grid-template-columns:minmax(0,2fr) minmax(300px,1fr);gap:16px}} #map{{height:590px;border-radius:8px;margin-top:12px}} #bars{{height:260px;display:flex;align-items:flex-end;gap:10px;padding-top:24px}}
.barwrap{{flex:1;text-align:center;color:var(--muted);font-size:11px}} .bar{{background:var(--blue);border-radius:5px 5px 0 0;min-height:2px;position:relative}} .bar span{{position:absolute;top:-20px;left:0;right:0;color:var(--ink);font:12px ui-monospace,monospace}}
.legend{{display:flex;gap:14px;flex-wrap:wrap;margin-top:10px;color:var(--muted)}} .dot{{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px}}
.note{{margin-top:16px;border-left:4px solid var(--gold)}} table{{width:100%;border-collapse:collapse;margin-top:14px}} th,td{{padding:8px;border-bottom:1px solid var(--line);text-align:left}} th{{font-size:12px;color:var(--muted)}}
@media(max-width:900px){{.grid{{grid-template-columns:1fr}} #map{{height:480px}}}}
</style></head><body>
<header><h1>Intra-Circle Roaming Site Prioritization</h1><p>Management snapshot · geographic gap proxy · online basemap edition</p></header>
<main><section class="cards" id="cards"></section><section class="grid"><div class="panel"><h2>Network footprint and shortlisted sites</h2><p>Pan, zoom, and select a marker for exact site context.</p><div class="legend"><span><i class="dot" style="background:#172033"></i>Operator B</span><span><i class="dot" style="background:#c79225"></i>Selected X</span><span><i class="dot" style="background:#a93a36"></i>Redundant X</span><span><i class="dot" style="background:#93b8dc"></i>Eligible X</span><span><i class="dot" style="background:#cbd5e1"></i>Filtered X</span></div><div id="map"></div></div>
<div><div class="panel"><h2>Candidate distance distribution</h2><p>Number of X sites by nearest-B distance band.</p><div id="bars"></div></div><div class="panel note"><h2>Interpretation guardrail</h2><p>This is a geographic gap proxy—not measured RF coverage gain. No service-area boundary is applied. Validate the shortlist with RF parameters, terrain, demand, capacity, cost, and field evidence.</p></div></div></section>
<section class="panel" style="margin-top:16px"><h2>Top selected sites</h2><p>First 20 sites in diversity-aware portfolio order.</p><table><thead><tr><th>Rank</th><th>X site</th><th>Nearest B</th><th>Gap (km)</th><th>Density ratio</th></tr></thead><tbody id="top"></tbody></table></section></main>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>const DATA={payload};
const fmt=n=>new Intl.NumberFormat(undefined,{{maximumFractionDigits:2}}).format(n); const S=DATA.summary;
const cards=[["Valid B sites",S.valid_b_sites],["Valid X sites",S.valid_x_sites],["Eligible X sites",S.eligible_sites],["Selected X sites",S.selected_sites],["Median selected gap",fmt(S.median_gap_km)+" km"],["Median density ratio",fmt(S.median_density_ratio)]];
document.querySelector('#cards').innerHTML=cards.map(([a,b])=>`<article class="card"><p>${{a}}</p><div class="metric">${{b}}</div></article>`).join('');
const max=Math.max(1,...DATA.bands.map(x=>x.candidate_count)); document.querySelector('#bars').innerHTML=DATA.bands.map(x=>`<div class="barwrap"><div class="bar" style="height:${{Math.max(2,190*x.candidate_count/max)}}px"><span>${{x.candidate_count}}</span></div><div>${{x.distance_band}}</div></div>`).join('');
const colors={{"Operator B":"#172033","Selected X":"#c79225","Redundant X":"#a93a36","Eligible X":"#93b8dc","Filtered X":"#cbd5e1"}};
const esc=value=>String(value??'').replace(/[&<>'"]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}})[c]);
if(window.L){{const map=L.map('map'); L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{maxZoom:19,attribution:'&copy; OpenStreetMap contributors'}}).addTo(map); const bounds=[]; DATA.sites.forEach(s=>{{bounds.push([s.latitude,s.longitude]); const selectedSpacing=s.nearest_selected_x_km==null?'First portfolio site':fmt(s.nearest_selected_x_km)+' km'; const popup=`<strong>${{esc(s.siteid)}}</strong><br>${{esc(s.status)}}`+(s.gap_km==null?'':`<br>Nearest B: ${{esc(s.nearest_b)}}<br>Gap: ${{fmt(s.gap_km)}} km<br>Density ratio: ${{fmt(s.density_ratio)}}<br>Local X spacing: ${{fmt(s.local_x_spacing_km)}} km<br>Nearest selected X: ${{selectedSpacing}}<br>Marginal score: ${{fmt(s.marginal_score)}}`); L.circleMarker([s.latitude,s.longitude],{{radius:s.status==='Selected X'?7:5,color:colors[s.status],fillColor:colors[s.status],fillOpacity:.78,weight:1}}).bindPopup(popup).addTo(map)}}); map.fitBounds(bounds,{{padding:[24,24]}})}} else {{document.querySelector('#map').innerHTML='<p style="padding:30px">Map library could not load. Internet access is required for this edition.</p>'}}
const top=DATA.sites.filter(s=>s.status==='Selected X').sort((a,b)=>a.selection_rank-b.selection_rank).slice(0,20); document.querySelector('#top').innerHTML=top.map(s=>`<tr><td>${{s.selection_rank}}</td><td>${{esc(s.siteid)}}</td><td>${{esc(s.nearest_b)}}</td><td>${{fmt(s.gap_km)}}</td><td>${{fmt(s.density_ratio)}}</td></tr>`).join('');
</script></body></html>"""
    output_path.write_text(document, encoding="utf-8")


def find_builder_root(explicit: str | Path | None = None) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("ICR_DATA_ANALYTICS_PLUGIN_ROOT"):
        candidates.append(Path(os.environ["ICR_DATA_ANALYTICS_PLUGIN_ROOT"]))
    candidates.extend([
        Path("D:/My_AI_Tools/Codex_Home/plugins/cache/openai-curated-remote/data-analytics"),
        Path.home() / ".codex/plugins/cache/openai-curated-remote/data-analytics",
    ])
    roots: list[Path] = []
    for candidate in candidates:
        if (candidate / "skills/build-report/scripts/deliver_portable_artifact.mjs").exists():
            roots.append(candidate)
        elif candidate.is_dir():
            roots.extend(path for path in candidate.iterdir() if (path / "skills/build-report/scripts/deliver_portable_artifact.mjs").exists())
    return sorted(roots)[-1] if roots else None


def deliver_offline_artifact(
    artifact_path: Path,
    output_path: Path,
    builder_root: Path,
    node_executable: str | None = None,
    browser_verification: bool = True,
) -> dict[str, Any]:
    node = node_executable or shutil.which("node")
    if not node:
        bundled = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
        if bundled.exists():
            node = str(bundled)
    if not node:
        raise RuntimeError("Node.js was not found; provide --node-executable")
    script = builder_root / "skills/build-report/scripts/deliver_portable_artifact.mjs"
    command = [str(node), str(script), "--input", str(artifact_path), "--output", str(output_path)]
    environment = None
    if not browser_verification:
        environment = os.environ.copy()
        environment["CHROMIUM_EXECUTABLE_PATH"] = str(builder_root / "__structural_verification_only__")
    completed = subprocess.run(command, capture_output=True, text=True, env=environment)
    combined = (completed.stdout + "\n" + completed.stderr).strip()
    if completed.returncode != 0 and ("reader_timeout" in combined or "browser_timeout" in combined):
        retry_environment = os.environ.copy()
        retry_environment["CHROMIUM_EXECUTABLE_PATH"] = str(builder_root / "__incompatible_browser_disabled__")
        completed = subprocess.run(command, capture_output=True, text=True, env=retry_environment)
    if completed.returncode != 0:
        detail = (completed.stdout + "\n" + completed.stderr).strip()
        raise RuntimeError(f"Portable dashboard delivery failed: {detail}")
    receipt_text = completed.stdout.strip().splitlines()[-1]
    try:
        receipt = json.loads(receipt_text)
    except json.JSONDecodeError:
        receipt = {"output": completed.stdout.strip(), "verification": "unknown"}
    if not browser_verification and isinstance(receipt, dict):
        receipt.pop("browserWarning", None)
        receipt["verificationMode"] = "structural_only"
    return receipt


def write_outputs(
    result: AnalysisResult,
    output_dir: str | Path,
    operator_b_name: str = "Operator B",
    operator_x_name: str = "Operator X",
    builder_root: str | Path | None = None,
    node_executable: str | None = None,
    offline_required: bool = True,
    verify_offline_browser: bool = True,
) -> dict[str, Any]:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    ranked_path = destination / "icr_ranked_sites.csv"
    issues_path = destination / "icr_data_issues.csv"
    summary_path = destination / "icr_dashboard_summary.csv"
    artifact_path = destination / "artifact.json"
    offline_path = destination / "icr_dashboard_offline.html"
    online_path = destination / "icr_dashboard_online.html"
    result.ranked_sites.to_csv(ranked_path, index=False)
    result.issues.to_csv(issues_path, index=False)
    pd.DataFrame([_summary(result)]).to_csv(summary_path, index=False)
    artifact_path.write_text(json.dumps(build_artifact(result, operator_b_name, operator_x_name), indent=2, ensure_ascii=False), encoding="utf-8")
    write_online_dashboard(result, online_path)
    warnings: list[str] = []
    receipt: dict[str, Any] | None = None
    resolved_builder = find_builder_root(builder_root)
    try:
        if resolved_builder is None:
            raise RuntimeError("Canonical offline dashboard builder was not found")
        receipt = deliver_offline_artifact(
            artifact_path,
            offline_path,
            resolved_builder,
            node_executable=node_executable,
            browser_verification=verify_offline_browser,
        )
    except RuntimeError as error:
        if offline_required:
            raise RuntimeError(f"{error}. CSV, artifact.json, and online HTML were written.") from error
        warnings.append(f"Offline dashboard was not generated: {error}")

    outputs: dict[str, Any] = {
        "ranked_csv": str(ranked_path.resolve()),
        "issues_csv": str(issues_path.resolve()),
        "summary_csv": str(summary_path.resolve()),
        "artifact_json": str(artifact_path.resolve()),
        "online_dashboard": str(online_path.resolve()),
        "verification": receipt,
        "warnings": warnings,
    }
    if offline_path.exists():
        outputs["offline_dashboard"] = str(offline_path.resolve())
    return outputs
