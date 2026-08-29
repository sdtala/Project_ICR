# ICR Site Selector

This project prioritizes Operator X sites that may fill geographic gaps in Operator B's footprint. It uses only site IDs and WGS84 coordinates, so its result is a **geographic gap proxy**, not an RF coverage prediction.

## Inputs

Provide two CSV files with these case-insensitive columns:

```csv
siteid,latitude,longitude
B001,12.9716,77.5946
```

Invalid rows, duplicate site IDs, and duplicate coordinates are excluded and recorded in `icr_data_issues.csv`.

## Run

Use a Python environment containing NumPy and pandas. From the repository root:

```powershell
$env:PYTHONPATH = "src"
python -m icr_analysis `
  --operator-b examples/operator_b.csv `
  --operator-x examples/operator_x.csv `
  --mode fixed_count `
  --count 5 `
  --criterion density `
  --output-dir output
```

## Local web GUI

On Windows, double-click `start_icr_gui.bat`. It uses the project-local `.venv`, opens the browser at `http://127.0.0.1:5000/`, and keeps the server attached to the command window. Press `Ctrl+C` in that window to stop it.

Install the project and its Flask dependency, then launch the localhost-only interface:

```powershell
python -m pip install -e .
icr-web
```

The browser opens at `http://127.0.0.1:5000/`. Use `icr-web --port 5050` to choose another local port or `icr-web --no-open` to suppress automatic browser launch.

### Separate X footprint replication workflow

Open `http://127.0.0.1:5000/replication` or choose **X footprint replication** in the header. This is intentionally separate from the original B gap-filling workflow.

The replication workflow:

1. Converts the requested X site-retention percentage into an exact overall site target.
2. Creates local geographic zones whose scale is derived from typical X-to-X spacing.
3. Allocates the percentage target across those zones while preserving local X density.
4. Chooses a well-spread set of X sites inside each zone.
5. Removes representative X sites that overlap nearby B sites using an adaptive local-B-spacing ratio or a fixed kilometre rule.
6. Optionally refills from the same zone so geography is preserved. It does not refill from another zone when no suitable local replacement exists.

The default is a 70% X **site-retention target**, five local X and B neighbours, zone scale 4, adaptive B-overlap ratio 0.40, and same-zone refill enabled. Site retention is not RF coverage percentage. Replication runs have their own result page, map layers, full-decision CSV, selected-sites CSV, summary CSV, data-issue CSV, manifest, and ZIP package.

The GUI accepts both CSVs, exposes the same selection parameters as the CLI, displays KPIs, processing-stage timings, the interactive site map, distance distribution, ranked and issue previews, and provides individual downloads plus a complete ZIP. The **Method & parameter help** page explains the adaptive X-to-X diversity, local saturation, hard separation, and recommended starting values. Runs are temporary, expire after one hour, and are deleted when the application stops. The bundled sample button demonstrates the workflow without operational data.

Leaflet JavaScript and CSS are bundled with the application, so the map controls do not depend on an external CDN. OpenStreetMap basemap tiles still require internet access. Site points are rendered on canvas in progressive batches and remain usable over the fallback background if tiles are unavailable.

Fixed-count mode supports an optional eligibility filter:

```powershell
python -m icr_analysis --operator-b b.csv --operator-x x.csv `
  --mode fixed_count --count 100 --criterion density `
  --prefilter absolute --threshold 2.5 --output-dir output
```

Distance-threshold mode selects every eligible candidate:

```powershell
python -m icr_analysis --operator-b b.csv --operator-x x.csv `
  --mode distance_threshold --criterion absolute --threshold 3.0 `
  --output-dir output
```

To convert threshold-qualified candidates into a de-clustered portfolio, add a hard minimum X-to-X spacing expressed relative to local B spacing:

```powershell
python -m icr_analysis --operator-b b.csv --operator-x x.csv `
  --mode distance_threshold --criterion density --threshold 1.0 `
  --threshold-portfolio declustered --min-separation-ratio 0.5 `
  --output-dir output
```

For `absolute`, thresholds are kilometres. For `density`, thresholds are ratios: `1.0` means the X-to-nearest-B gap is at least as large as typical B-site spacing in that locality.

## Outputs

- `icr_ranked_sites.csv`: complete candidate ranking, audit fields, and selection reason.
- `icr_data_issues.csv`: excluded input rows and reasons.
- `icr_dashboard_summary.csv`: reconciled one-row source for the management KPI cards.
- `artifact.json`: canonical, source-backed dashboard snapshot.
- `icr_dashboard_offline.html`: fully self-contained management dashboard.
- `icr_dashboard_online.html`: management dashboard with OpenStreetMap tiles; internet is required when viewing it.

The offline dashboard is packaged with the installed Data Analytics portable builder. If it cannot be discovered automatically, provide its plugin root with `--builder-root` or set `ICR_DATA_ANALYTICS_PLUGIN_ROOT`.

For responsive localhost runs, the Flask GUI packages the offline dashboard with structural verification and records that mode in `run_manifest.json`. The CLI retains strict browser-based verification by default.

## Selection logic

1. Calculate exact great-circle distance from every X site to its nearest B site.
2. Estimate local B density from the median nearest-neighbour spacing of the five B sites nearest each candidate.
3. Calculate `density_gap_ratio = nearest_b_distance_km / local_b_spacing_km`.
4. Estimate local X candidate density from nearby alternative X sites.
5. Rank candidates greedily. After each selection, calculate distance to the nearest selected X site and count selected X neighbours inside the diversity radius.
6. Calculate `marginal_score = base_score × [1 − diversity_weight × (1 − diversity_factor) − saturation_weight × (1 − saturation_factor)]`.
7. By default, the diversity radius adapts to each candidate's local B spacing. `--diversity-km` replaces it with one absolute circle-wide radius.
8. Optionally exclude candidates closer than `--min-separation-ratio × local_b_spacing_km`. In threshold mode this is activated with `--threshold-portfolio declustered`.

Recommended starting values are `--local-k 5`, `--x-local-k 5`, `--diversity-weight 0.20`, `--saturation-weight 0.10`, adaptive diversity distance, and no hard separation. Start a de-clustering review at `--min-separation-ratio 0.5`.

## Performance

Spatial scoring uses SciPy `cKDTree` batch queries on unit-sphere coordinates. This preserves great-circle distance accuracy while avoiding per-site Python neighbour searches. Fixed-count portfolio selection uses vectorized NumPy updates, and select-all threshold mode calculates final portfolio-density diagnostics directly instead of running a full greedy loop.

Run the deterministic operational-scale benchmark with:

```powershell
.\.venv\Scripts\python.exe benchmarks\benchmark_scale.py --b-sites 6000 --x-sites 12000 --count 100
```

On the implementation host, the 6,000-B / 12,000-X fixed-count benchmark completed in approximately 0.5 seconds; the select-all threshold benchmark completed in approximately 0.6 seconds. Actual time depends on hardware, CSV storage, selected mode, and output-dashboard packaging. No spatial database is required for this Phase 1 scale.

No service-area boundary is applied. Remote sites can therefore rank highly. Before commercial selection, validate the shortlist with sector configuration, bands, antenna height and azimuth, terrain/clutter, traffic, population, capacity, cost, and field measurements.

## Tests

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```
