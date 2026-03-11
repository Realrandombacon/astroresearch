"""
AstroResearch Dashboard — Real-time monitoring for the autonomous anomaly detection agent.

Single-file dashboard: Flask backend + embedded HTML/CSS/JS frontend.
Reads the same data files as the orchestrator (memory.json, findings.tsv, research.log).

Usage:
    pip install flask
    python dashboard.py              # starts on http://localhost:5555
    python dashboard.py --port 8080  # custom port
"""

import os
import sys
import json
import csv
import datetime
import argparse
import collections

# Force UTF-8 on Windows (same fix as orchestrator)
if sys.platform == "win32":
    os.system("")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from flask import Flask, jsonify, Response, request

# ---------------------------------------------------------------------------
# Paths — same as orchestrator
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
MEMORY_FILE = os.path.join(BASE_DIR, "memory.json")
FINDINGS_FILE = os.path.join(BASE_DIR, "findings.tsv")
RESEARCH_LOG = os.path.join(BASE_DIR, "research.log")
STATUS_FILE = os.path.join(BASE_DIR, "dashboard_status.json")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Helper readers
# ---------------------------------------------------------------------------

def read_memory():
    """Read and return the current memory.json."""
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"regions": {}, "total_cycles_all_runs": 0, "run_count": 0,
                "best_leads": [], "known_failures": {}, "sky_coverage": {
                    "dec_min": 90, "dec_max": -90, "ra_bins_visited": []}}


def read_findings():
    """Read findings.tsv and return as list of dicts."""
    findings = []
    try:
        with open(FINDINGS_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter="\t")
            for row in reader:
                findings.append(row)
    except Exception:
        pass
    return findings


def read_log_tail(n=100):
    """Read last N lines of research.log."""
    lines = []
    try:
        with open(RESEARCH_LOG, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
            lines = all_lines[-n:]
    except Exception:
        pass
    return [l.rstrip() for l in lines]


def read_live_status():
    """Read live status from dashboard_status.json (written by orchestrator)."""
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"running": False, "cycle": "?", "phase": "unknown",
                "current_tool": None, "last_thought": ""}

# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@app.route("/api/status")
def api_status():
    mem = read_memory()
    findings = read_findings()
    live = read_live_status()

    n_regions = len(mem.get("regions", {}))
    n_findings = len(findings)
    n_high = sum(1 for f in findings if f.get("significance") == "high")
    n_medium = sum(1 for f in findings if f.get("significance") == "medium")
    n_low = sum(1 for f in findings if f.get("significance") == "low")
    total_cycles = mem.get("total_cycles_all_runs", 0)
    run_count = mem.get("run_count", 0)

    cov = mem.get("sky_coverage", {})
    ra_bins = cov.get("ra_bins_visited", [])
    # Honest coverage: each region covers ~0.05 deg radius = ~0.008 sq deg
    # Full sky = 41,253 sq deg. Show actual area sampled.
    FIELD_AREA_SQDEG = 0.008  # ~3 arcmin radius field
    sampled_area = round(n_regions * FIELD_AREA_SQDEG, 2)
    coverage_pct = round(sampled_area / 41253 * 100, 4)
    # Also keep the RA spread metric (more useful)
    ra_spread_pct = round(len(ra_bins) / 36 * 100, 1)

    return jsonify({
        "total_cycles": total_cycles,
        "run_count": run_count,
        "n_regions": n_regions,
        "n_findings": n_findings,
        "n_high": n_high,
        "n_medium": n_medium,
        "n_low": n_low,
        "coverage_pct": coverage_pct,
        "sampled_area_sqdeg": sampled_area,
        "ra_spread_pct": ra_spread_pct,
        "dec_range": [cov.get("dec_min", 90), cov.get("dec_max", -90)],
        "ra_bins_visited": ra_bins,
        "last_updated": mem.get("last_updated", ""),
        "best_leads": mem.get("best_leads", [])[-5:],
        "live": live,
    })


@app.route("/api/regions")
def api_regions():
    mem = read_memory()
    regions = []
    for key, reg in mem.get("regions", {}).items():
        regions.append({
            "ra": reg.get("ra", 0),
            "dec": reg.get("dec", 0),
            "visits": reg.get("visits", 1),
            "tools_used": reg.get("tools_used", []),
            "n_tools": len(reg.get("tools_used", [])),
            "outcomes": reg.get("outcomes", []),
            "n_findings": len(reg.get("findings", [])),
            "findings": reg.get("findings", []),
            "last_cycle": reg.get("last_cycle", 0),
        })
    return jsonify(regions)


@app.route("/api/findings")
def api_findings():
    findings = read_findings()
    # Return in reverse chronological order
    findings.reverse()
    return jsonify(findings)


@app.route("/api/tool-stats")
def api_tool_stats():
    mem = read_memory()

    # Normalize common Qwen typos
    TOOL_ALIASES = {
        "download_cutouts": "download_cutout",
        "simbad": "simbad_check",
        "mast": "search_region",
    }

    tool_counts = collections.Counter()
    for reg in mem.get("regions", {}).values():
        for tool in reg.get("tools_used", []):
            tool_counts[TOOL_ALIASES.get(tool, tool)] += 1

    # Also count from outcomes for more accuracy
    outcome_counts = collections.Counter()
    for reg in mem.get("regions", {}).values():
        for outcome in reg.get("outcomes", []):
            tool_name = outcome.split(":")[0].strip()
            outcome_counts[TOOL_ALIASES.get(tool_name, tool_name)] += 1

    # Failure stats
    failures = mem.get("known_failures", {})
    failure_counts = collections.Counter()
    for key, fail in failures.items():
        tool = key.split("|")[0]
        failure_counts[TOOL_ALIASES.get(tool, tool)] += fail.get("count", 1)

    return jsonify({
        "tool_regions": dict(tool_counts.most_common()),
        "outcome_counts": dict(outcome_counts.most_common()),
        "failure_counts": dict(failure_counts.most_common()),
    })


@app.route("/api/log")
def api_log():
    n = request.args.get("n", 80, type=int)
    lines = read_log_tail(n)
    return jsonify(lines)


@app.route("/api/exploration-pace")
def api_exploration_pace():
    """Return cumulative regions & findings over cycles for pace chart."""
    mem = read_memory()
    regions = mem.get("regions", {})

    # Build (cycle, event_type) pairs
    events = []
    for key, reg in regions.items():
        first_cycle = reg.get("first_cycle", reg.get("last_cycle", 0))
        events.append((first_cycle, "region"))
        for fid in reg.get("findings", []):
            events.append((reg.get("last_cycle", first_cycle), "finding"))

    events.sort(key=lambda x: x[0])

    cycles, cum_regions, cum_findings = [], [], []
    r_count, f_count = 0, 0
    for cycle, etype in events:
        if etype == "region":
            r_count += 1
        else:
            f_count += 1
        cycles.append(cycle)
        cum_regions.append(r_count)
        cum_findings.append(f_count)

    return jsonify({
        "cycles": cycles,
        "cum_regions": cum_regions,
        "cum_findings": cum_findings,
    })


@app.route("/api/region-depth")
def api_region_depth():
    """Return histogram of visits per region."""
    mem = read_memory()
    regions = mem.get("regions", {})

    # Bin visits: 1, 2-5, 6-10, 11-20, 21-50, 50+
    bins = {"1": 0, "2-5": 0, "6-10": 0, "11-20": 0, "21-50": 0, "50+": 0}
    for reg in regions.values():
        v = reg.get("visits", 1)
        if v <= 1:
            bins["1"] += 1
        elif v <= 5:
            bins["2-5"] += 1
        elif v <= 10:
            bins["6-10"] += 1
        elif v <= 20:
            bins["11-20"] += 1
        elif v <= 50:
            bins["21-50"] += 1
        else:
            bins["50+"] += 1

    # Also compute stats
    visit_list = [reg.get("visits", 1) for reg in regions.values()]
    avg_visits = round(sum(visit_list) / len(visit_list), 1) if visit_list else 0
    max_visits = max(visit_list) if visit_list else 0

    return jsonify({
        "bins": bins,
        "avg_visits": avg_visits,
        "max_visits": max_visits,
        "total_regions": len(regions),
    })


@app.route("/api/coverage-grid")
def api_coverage_grid():
    """Return a 36x18 grid (RA bins x Dec bins) for a heatmap."""
    mem = read_memory()
    # Create grid: 36 RA bins (10 deg each) x 18 Dec bins (10 deg each, -90 to +90)
    grid = [[0]*18 for _ in range(36)]
    for reg in mem.get("regions", {}).values():
        ra = reg.get("ra", 0)
        dec = reg.get("dec", 0)
        ra_bin = min(int(ra / 10), 35)
        dec_bin = min(int((dec + 90) / 10), 17)
        grid[ra_bin][dec_bin] += reg.get("visits", 1)
    return jsonify({
        "grid": grid,
        "ra_labels": [f"{i*10}" for i in range(36)],
        "dec_labels": [f"{i*10-90}" for i in range(18)],
    })


# ---------------------------------------------------------------------------
# Main HTML Page
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return Response(HTML_PAGE, mimetype="text/html")


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AstroResearch Dashboard</title>
<script src="https://cdn.plot.ly/plotly-2.35.0.min.js"></script>
<style>
/* ================================================================
   DARK SPACE THEME
   ================================================================ */
:root {
    --bg-deep:     #060a1a;
    --bg-main:     #0b1029;
    --bg-card:     #111738;
    --bg-card-alt: #161d45;
    --border:      #1e2a5a;
    --border-glow: #2d3a80;
    --text:        #c8d6e5;
    --text-dim:    #6b7db3;
    --text-bright: #e8edf5;
    --accent:      #00d4ff;
    --accent-dim:  #0098b8;
    --purple:      #8b5cf6;
    --purple-dim:  #6930c3;
    --green:       #22c55e;
    --amber:       #f59e0b;
    --red:         #ef4444;
    --pink:        #ec4899;
    --cyan:        #06b6d4;
    --gold:        #eab308;
}

* { margin: 0; padding: 0; box-sizing: border-box; }

body {
    background: var(--bg-deep);
    color: var(--text);
    font-family: 'SF Mono', 'Fira Code', 'JetBrains Mono', 'Cascadia Code', monospace;
    font-size: 13px;
    line-height: 1.5;
    min-height: 100vh;
    overflow-x: hidden;
}

/* Starfield background effect */
body::before {
    content: '';
    position: fixed;
    top: 0; left: 0;
    width: 100%; height: 100%;
    background:
        radial-gradient(1px 1px at 10% 20%, rgba(255,255,255,0.15), transparent),
        radial-gradient(1px 1px at 30% 70%, rgba(255,255,255,0.1), transparent),
        radial-gradient(1px 1px at 50% 40%, rgba(255,255,255,0.12), transparent),
        radial-gradient(1px 1px at 70% 80%, rgba(255,255,255,0.08), transparent),
        radial-gradient(1px 1px at 90% 30%, rgba(255,255,255,0.1), transparent),
        radial-gradient(1.5px 1.5px at 15% 85%, rgba(0,212,255,0.2), transparent),
        radial-gradient(1.5px 1.5px at 85% 15%, rgba(139,92,246,0.2), transparent),
        radial-gradient(1px 1px at 25% 55%, rgba(255,255,255,0.1), transparent),
        radial-gradient(1px 1px at 65% 25%, rgba(255,255,255,0.08), transparent),
        radial-gradient(1px 1px at 45% 95%, rgba(255,255,255,0.1), transparent);
    pointer-events: none;
    z-index: 0;
}

/* ================================================================
   LAYOUT
   ================================================================ */
.dashboard {
    position: relative;
    z-index: 1;
    max-width: 1600px;
    margin: 0 auto;
    padding: 16px 20px;
    display: flex;
    flex-direction: column;
    gap: 16px;
}

/* HEADER */
.header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 16px 24px;
    background: linear-gradient(135deg, var(--bg-card) 0%, var(--bg-card-alt) 100%);
    border: 1px solid var(--border);
    border-radius: 12px;
    box-shadow: 0 0 30px rgba(0,212,255,0.05);
}
.header-left {
    display: flex;
    align-items: center;
    gap: 14px;
}
.header-logo {
    font-size: 32px;
    line-height: 1;
}
.header-title {
    font-size: 20px;
    font-weight: 700;
    color: var(--text-bright);
    letter-spacing: -0.5px;
}
.header-subtitle {
    font-size: 11px;
    color: var(--text-dim);
    margin-top: 2px;
}
.header-right {
    display: flex;
    align-items: center;
    gap: 16px;
}
.live-dot {
    display: inline-block;
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--green);
    animation: pulse 2s infinite;
    margin-right: 6px;
}
.live-dot.offline { background: var(--red); animation: none; }
@keyframes pulse {
    0%, 100% { opacity: 1; box-shadow: 0 0 0 0 rgba(34,197,94,0.7); }
    50% { opacity: 0.7; box-shadow: 0 0 0 6px rgba(34,197,94,0); }
}
.status-pill {
    display: inline-flex;
    align-items: center;
    padding: 4px 12px;
    border-radius: 20px;
    font-size: 11px;
    font-weight: 600;
    background: rgba(34,197,94,0.15);
    color: var(--green);
    border: 1px solid rgba(34,197,94,0.3);
}
.status-pill.offline {
    background: rgba(239,68,68,0.15);
    color: var(--red);
    border-color: rgba(239,68,68,0.3);
}
.last-update {
    font-size: 11px;
    color: var(--text-dim);
}

/* STAT CARDS ROW */
.stats-row {
    display: grid;
    grid-template-columns: repeat(6, 1fr);
    gap: 12px;
}
.stat-card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px 16px;
    position: relative;
    overflow: hidden;
    transition: border-color 0.3s, box-shadow 0.3s;
}
.stat-card:hover {
    border-color: var(--border-glow);
    box-shadow: 0 0 20px rgba(0,212,255,0.08);
}
.stat-card::after {
    content: '';
    position: absolute;
    top: 0; left: 0;
    width: 100%; height: 3px;
    border-radius: 10px 10px 0 0;
}
.stat-card.cyan::after    { background: var(--accent); }
.stat-card.purple::after  { background: var(--purple); }
.stat-card.green::after   { background: var(--green); }
.stat-card.amber::after   { background: var(--amber); }
.stat-card.pink::after    { background: var(--pink); }
.stat-card.gold::after    { background: var(--gold); }
.stat-label {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--text-dim);
    margin-bottom: 4px;
}
.stat-value {
    font-size: 26px;
    font-weight: 800;
    color: var(--text-bright);
    line-height: 1.1;
}
.stat-detail {
    font-size: 10px;
    color: var(--text-dim);
    margin-top: 4px;
}

/* MAIN GRID */
.main-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
}

/* CARD */
.card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
}
.card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
}
.card-title {
    font-size: 13px;
    font-weight: 700;
    color: var(--text-bright);
    display: flex;
    align-items: center;
    gap: 8px;
}
.card-title .icon { font-size: 16px; }
.card-badge {
    font-size: 10px;
    padding: 2px 8px;
    border-radius: 10px;
    font-weight: 600;
}
.card-body { padding: 12px 16px; }
.card.full-width { grid-column: 1 / -1; }

/* SKY MAP */
#skymap { width: 100%; height: 420px; }
#skymap-heatmap { width: 100%; height: 280px; }

/* FINDINGS TABLE */
.findings-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 11px;
}
.findings-table th {
    text-align: left;
    padding: 8px 10px;
    border-bottom: 1px solid var(--border);
    color: var(--text-dim);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    position: sticky;
    top: 0;
    background: var(--bg-card);
}
.findings-table td {
    padding: 7px 10px;
    border-bottom: 1px solid rgba(30,42,90,0.5);
    vertical-align: top;
}
.findings-table tr:hover td { background: rgba(0,212,255,0.03); }
.findings-scroll {
    max-height: 380px;
    overflow-y: auto;
}
.sig-badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 8px;
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
}
.sig-high   { background: rgba(239,68,68,0.2);  color: var(--red);   border: 1px solid rgba(239,68,68,0.4); }
.sig-medium { background: rgba(245,158,11,0.2); color: var(--amber); border: 1px solid rgba(245,158,11,0.4); }
.sig-low    { background: rgba(34,197,94,0.15); color: var(--green); border: 1px solid rgba(34,197,94,0.3); }
.coord-mono {
    font-family: 'SF Mono', monospace;
    font-size: 11px;
    color: var(--accent);
}

/* TOOL STATS */
#tool-chart { width: 100%; height: 280px; }

/* LOG PANEL */
.log-panel {
    max-height: 340px;
    overflow-y: auto;
    padding: 8px 0;
    font-size: 11px;
    line-height: 1.6;
}
.log-line {
    padding: 1px 12px;
    white-space: pre-wrap;
    word-break: break-all;
}
.log-line:hover { background: rgba(0,212,255,0.03); }
.log-ts { color: var(--text-dim); }
.log-lvl-info  { color: var(--accent); }
.log-lvl-ok    { color: var(--green); }
.log-lvl-warn  { color: var(--amber); }
.log-lvl-error { color: var(--red); }
.log-lvl-tool  { color: var(--cyan); }
.log-lvl-find  { color: var(--pink); }
.log-lvl-think { color: var(--purple); font-style: italic; }

/* BEST LEADS */
.lead-item {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    padding: 8px 0;
    border-bottom: 1px solid rgba(30,42,90,0.5);
}
.lead-item:last-child { border-bottom: none; }
.lead-coord {
    font-family: 'SF Mono', monospace;
    font-size: 11px;
    color: var(--accent);
    flex-shrink: 0;
    min-width: 120px;
}
.lead-desc {
    font-size: 11px;
    color: var(--text);
}
.lead-cycle {
    font-size: 10px;
    color: var(--text-dim);
    margin-left: auto;
    flex-shrink: 0;
}

/* LIVE STATUS BAR */
.live-bar {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 10px 16px;
    background: linear-gradient(90deg, rgba(0,212,255,0.08) 0%, rgba(139,92,246,0.08) 100%);
    border: 1px solid var(--border);
    border-radius: 10px;
    font-size: 12px;
}
.live-label {
    font-weight: 700;
    color: var(--accent);
    flex-shrink: 0;
}
.live-thought {
    color: var(--purple);
    font-style: italic;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1;
}
.live-tool {
    color: var(--cyan);
    font-weight: 600;
    flex-shrink: 0;
}

/* SCROLLBAR */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: var(--bg-card); }
::-webkit-scrollbar-thumb { background: var(--border-glow); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--accent-dim); }

/* RESPONSIVE */
@media (max-width: 1200px) {
    .stats-row { grid-template-columns: repeat(3, 1fr); }
    .main-grid { grid-template-columns: 1fr; }
}
@media (max-width: 768px) {
    .stats-row { grid-template-columns: repeat(2, 1fr); }
    .dashboard { padding: 10px; }
}
</style>
</head>
<body>

<div class="dashboard">

    <!-- HEADER -->
    <div class="header">
        <div class="header-left">
            <div class="header-logo">&#x1F30C;</div>
            <div>
                <div class="header-title">AstroResearch Dashboard</div>
                <div class="header-subtitle">Autonomous Astronomical Anomaly Detection</div>
            </div>
        </div>
        <div class="header-right">
            <span class="last-update" id="last-update"></span>
            <span class="status-pill" id="status-pill">
                <span class="live-dot" id="live-dot"></span>
                <span id="status-text">Connecting...</span>
            </span>
        </div>
    </div>

    <!-- LIVE STATUS BAR -->
    <div class="live-bar" id="live-bar" style="display:none;">
        <span class="live-label">&#x1F52C; LIVE</span>
        <span class="live-tool" id="live-cycle"></span>
        <span class="live-tool" id="live-tool"></span>
        <span class="live-thought" id="live-thought"></span>
    </div>

    <!-- STAT CARDS -->
    <div class="stats-row">
        <div class="stat-card cyan">
            <div class="stat-label">Total Cycles</div>
            <div class="stat-value" id="stat-cycles">-</div>
            <div class="stat-detail" id="stat-runs">- runs</div>
        </div>
        <div class="stat-card purple">
            <div class="stat-label">Regions Explored</div>
            <div class="stat-value" id="stat-regions">-</div>
            <div class="stat-detail" id="stat-coverage">- sq° sampled</div>
        </div>
        <div class="stat-card green">
            <div class="stat-label">Findings</div>
            <div class="stat-value" id="stat-findings">-</div>
            <div class="stat-detail" id="stat-findings-detail">-</div>
        </div>
        <div class="stat-card pink">
            <div class="stat-label">High Priority</div>
            <div class="stat-value" id="stat-high">-</div>
            <div class="stat-detail">High significance</div>
        </div>
        <div class="stat-card amber">
            <div class="stat-label">Sky Sampled</div>
            <div class="stat-value" id="stat-sky-pct">-</div>
            <div class="stat-detail" id="stat-dec-range">of 41,253 sq°</div>
        </div>
        <div class="stat-card gold">
            <div class="stat-label">Best Leads</div>
            <div class="stat-value" id="stat-leads">-</div>
            <div class="stat-detail">Unresolved</div>
        </div>
    </div>

    <!-- MAIN GRID -->
    <div class="main-grid">

        <!-- SKY MAP (full width) -->
        <div class="card full-width">
            <div class="card-header">
                <div class="card-title">
                    <span class="icon">&#x1F5FA;</span> Sky Coverage Map
                </div>
                <span class="card-badge" style="background:rgba(0,212,255,0.15);color:var(--accent);border:1px solid rgba(0,212,255,0.3);" id="map-badge">0 regions</span>
            </div>
            <div class="card-body">
                <div id="skymap"></div>
            </div>
        </div>

        <!-- FINDINGS TIMELINE (full width) -->
        <div class="card full-width">
            <div class="card-header">
                <div class="card-title">
                    <span class="icon">&#x1F4C8;</span> Discovery Timeline
                </div>
            </div>
            <div class="card-body">
                <div id="timeline-chart" style="width:100%;height:200px;"></div>
            </div>
        </div>

        <!-- COVERAGE HEATMAP + TOOL STATS -->
        <div class="card">
            <div class="card-header">
                <div class="card-title">
                    <span class="icon">&#x1F525;</span> Coverage Heatmap
                </div>
            </div>
            <div class="card-body">
                <div id="skymap-heatmap"></div>
            </div>
        </div>

        <div class="card">
            <div class="card-header">
                <div class="card-title">
                    <span class="icon">&#x1F527;</span> Tool Usage
                </div>
            </div>
            <div class="card-body">
                <div id="tool-chart"></div>
            </div>
        </div>

        <!-- EXPLORATION PACE (full width) -->
        <div class="card full-width">
            <div class="card-header">
                <div class="card-title">
                    <span class="icon">&#x1F680;</span> Exploration Pace
                </div>
                <span class="card-badge" style="background:rgba(0,212,255,0.15);color:var(--accent);border:1px solid rgba(0,212,255,0.3);" id="pace-badge">-</span>
            </div>
            <div class="card-body">
                <div id="pace-chart" style="width:100%;height:220px;"></div>
            </div>
        </div>

        <!-- REGION DEPTH + EXPLORATION REALITY -->
        <div class="card">
            <div class="card-header">
                <div class="card-title">
                    <span class="icon">&#x1F4CA;</span> Investigation Depth
                </div>
            </div>
            <div class="card-body">
                <div id="depth-chart" style="width:100%;height:280px;"></div>
            </div>
        </div>

        <div class="card">
            <div class="card-header">
                <div class="card-title">
                    <span class="icon">&#x1F30D;</span> Sky Reality Check
                </div>
            </div>
            <div class="card-body" id="reality-body" style="padding:16px;">
                <div style="color:var(--text-dim);text-align:center;">Waiting for data...</div>
            </div>
        </div>

        <!-- FINDINGS TABLE -->
        <div class="card full-width">
            <div class="card-header">
                <div class="card-title">
                    <span class="icon">&#x2B50;</span> Findings Log
                </div>
                <span class="card-badge" style="background:rgba(236,72,153,0.15);color:var(--pink);border:1px solid rgba(236,72,153,0.3);" id="findings-badge">0 findings</span>
            </div>
            <div class="findings-scroll">
                <table class="findings-table">
                    <thead>
                        <tr>
                            <th>Significance</th>
                            <th>Time</th>
                            <th>RA / Dec</th>
                            <th>Description</th>
                            <th>ID</th>
                        </tr>
                    </thead>
                    <tbody id="findings-body"></tbody>
                </table>
            </div>
        </div>

        <!-- BEST LEADS -->
        <div class="card">
            <div class="card-header">
                <div class="card-title">
                    <span class="icon">&#x1F3AF;</span> Priority Leads
                </div>
            </div>
            <div class="card-body" id="leads-body">
                <div style="color:var(--text-dim)">No leads yet</div>
            </div>
        </div>

        <!-- LIVE LOG -->
        <div class="card">
            <div class="card-header">
                <div class="card-title">
                    <span class="icon">&#x1F4DC;</span> Research Log
                </div>
                <span class="card-badge" style="background:rgba(139,92,246,0.15);color:var(--purple);border:1px solid rgba(139,92,246,0.3);">Live</span>
            </div>
            <div class="log-panel" id="log-panel"></div>
        </div>

    </div><!-- /main-grid -->

</div><!-- /dashboard -->

<script>
// ================================================================
// DASHBOARD STATE & UPDATE LOGIC
// ================================================================

const REFRESH_INTERVAL = 15000; // ms — 15s is plenty for observation
let lastLogLength = 0;

// ── Change detection: skip re-renders if data hasn't changed ──
const _lastHash = {};
function dataChanged(key, data) {
    const h = JSON.stringify(data);
    if (_lastHash[key] === h) return false;
    _lastHash[key] = h;
    return true;
}

// ── Plotly layout defaults ──
const PLOTLY_BG = '#0b1029';
const PLOTLY_PAPER = '#111738';
const PLOTLY_GRID = '#1e2a5a';
const PLOTLY_TEXT = '#6b7db3';
const PLOTLY_FONT = { family: 'SF Mono, Fira Code, monospace', size: 10, color: '#6b7db3' };

// ── Fetch helpers ──
async function fetchJSON(url) {
    try {
        const r = await fetch(url);
        return await r.json();
    } catch (e) {
        console.error('Fetch error:', url, e);
        return null;
    }
}

// ================================================================
// UPDATE FUNCTIONS
// ================================================================

async function updateStatus() {
    const data = await fetchJSON('/api/status');
    if (!data) return;

    document.getElementById('stat-cycles').textContent = data.total_cycles;
    document.getElementById('stat-runs').textContent = `${data.run_count} run(s)`;
    document.getElementById('stat-regions').textContent = data.n_regions;
    document.getElementById('stat-coverage').textContent = `${data.sampled_area_sqdeg} sq°`;
    document.getElementById('stat-findings').textContent = data.n_findings;
    document.getElementById('stat-findings-detail').textContent =
        `${data.n_high} high / ${data.n_medium} med / ${data.n_low} low`;
    document.getElementById('stat-high').textContent = data.n_high;
    document.getElementById('stat-sky-pct').textContent = `${data.sampled_area_sqdeg} sq°`;
    document.getElementById('stat-dec-range').textContent =
        data.n_regions > 0
            ? `${data.coverage_pct}% of sky · Dec: ${data.dec_range[0].toFixed(0)}° to ${data.dec_range[1].toFixed(0)}°`
            : `of 41,253 sq° total`;
    document.getElementById('stat-leads').textContent = data.best_leads.length;

    // Last updated
    if (data.last_updated) {
        const d = new Date(data.last_updated);
        const ago = Math.floor((Date.now() - d.getTime()) / 1000);
        let agoStr = ago < 60 ? `${ago}s ago` : ago < 3600 ? `${Math.floor(ago/60)}m ago` : `${Math.floor(ago/3600)}h ago`;
        document.getElementById('last-update').textContent = `Updated ${agoStr}`;
    }

    // Live status
    const live = data.live;
    const isRunning = live && live.running;
    const pill = document.getElementById('status-pill');
    const dot = document.getElementById('live-dot');
    const statusText = document.getElementById('status-text');
    const liveBar = document.getElementById('live-bar');

    if (isRunning) {
        pill.className = 'status-pill';
        dot.className = 'live-dot';
        statusText.textContent = 'Running';
        liveBar.style.display = 'flex';
        document.getElementById('live-cycle').textContent = `Cycle ${live.cycle || '?'}`;
        document.getElementById('live-tool').textContent = live.current_tool ? `| ${live.current_tool}` : '';
        document.getElementById('live-thought').textContent = live.last_thought || '';
    } else {
        pill.className = 'status-pill offline';
        dot.className = 'live-dot offline';
        statusText.textContent = 'Idle';
        liveBar.style.display = 'none';
    }

    // Best leads
    const leadsBody = document.getElementById('leads-body');
    if (data.best_leads && data.best_leads.length > 0) {
        leadsBody.innerHTML = data.best_leads.map(lead => `
            <div class="lead-item">
                <span class="lead-coord">RA ${lead.ra.toFixed(2)} / Dec ${lead.dec.toFixed(2)}</span>
                <span class="lead-desc">${escapeHtml(lead.why || '')}</span>
                <span class="lead-cycle">C${lead.cycle || '?'}</span>
            </div>
        `).join('');
    } else {
        leadsBody.innerHTML = '<div style="color:var(--text-dim);padding:20px 0;text-align:center;">No high-significance leads yet</div>';
    }
}


async function updateSkyMap() {
    const regions = await fetchJSON('/api/regions');
    if (!regions || regions.length === 0) {
        document.getElementById('map-badge').textContent = '0 regions';
        return;
    }
    if (!dataChanged('skymap', regions)) return;

    document.getElementById('map-badge').textContent = `${regions.length} regions`;

    const ra = regions.map(r => r.ra);
    const dec = regions.map(r => r.dec);
    const visits = regions.map(r => r.visits);
    const nFindings = regions.map(r => r.n_findings);
    const sizes = regions.map(r => Math.max(6, Math.min(22, r.visits * 3 + r.n_findings * 6)));
    const colors = regions.map(r => r.n_findings > 0 ? r.visits : r.visits);
    const hoverText = regions.map(r =>
        `<b>RA ${r.ra.toFixed(2)}, Dec ${r.dec.toFixed(2)}</b><br>` +
        `Visits: ${r.visits}<br>` +
        `Tools: ${r.tools_used.join(', ')}<br>` +
        `Findings: ${r.n_findings}<br>` +
        `Last outcome: ${r.outcomes.length > 0 ? r.outcomes[r.outcomes.length-1] : 'none'}`
    );

    // Main scatter: explored regions
    const traceRegions = {
        x: ra, y: dec,
        mode: 'markers',
        type: 'scatter',
        name: 'Explored',
        marker: {
            size: sizes,
            color: visits,
            colorscale: [[0,'#1a365d'],[0.3,'#0ea5e9'],[0.6,'#8b5cf6'],[1,'#ec4899']],
            showscale: true,
            colorbar: {
                title: { text: 'Visits', font: PLOTLY_FONT },
                tickfont: PLOTLY_FONT,
                bordercolor: PLOTLY_GRID,
                len: 0.6,
                thickness: 12,
            },
            line: { color: 'rgba(0,212,255,0.4)', width: 1 },
            opacity: 0.85,
        },
        text: hoverText,
        hoverinfo: 'text',
    };

    // Findings overlay
    const findingsRegions = regions.filter(r => r.n_findings > 0);
    const traceFindings = {
        x: findingsRegions.map(r => r.ra),
        y: findingsRegions.map(r => r.dec),
        mode: 'markers',
        type: 'scatter',
        name: 'Has Findings',
        marker: {
            size: findingsRegions.map(r => 10 + r.n_findings * 5),
            color: 'rgba(234,179,8,0)',
            line: { color: '#eab308', width: 2 },
            symbol: 'diamond-open',
        },
        hoverinfo: 'skip',
    };

    const layout = {
        paper_bgcolor: PLOTLY_PAPER,
        plot_bgcolor: PLOTLY_BG,
        font: PLOTLY_FONT,
        margin: { t: 30, b: 50, l: 55, r: 20 },
        xaxis: {
            title: 'Right Ascension (degrees)',
            range: [0, 360],
            dtick: 30,
            gridcolor: PLOTLY_GRID,
            zerolinecolor: PLOTLY_GRID,
            tickfont: PLOTLY_FONT,
        },
        yaxis: {
            title: 'Declination (degrees)',
            range: [-90, 90],
            dtick: 15,
            gridcolor: PLOTLY_GRID,
            zerolinecolor: PLOTLY_GRID,
            tickfont: PLOTLY_FONT,
        },
        showlegend: true,
        legend: {
            x: 0.01, y: 0.99,
            bgcolor: 'rgba(17,23,56,0.8)',
            bordercolor: PLOTLY_GRID,
            font: { ...PLOTLY_FONT, size: 10 },
        },
        // Galactic plane removed — too expensive for periodic re-render
    };

    Plotly.react('skymap', [traceRegions, traceFindings], layout, { responsive: true, displayModeBar: false, staticPlot: true });
}


async function updateHeatmap() {
    const data = await fetchJSON('/api/coverage-grid');
    if (!data) return;
    if (!dataChanged('heatmap', data)) return;

    const trace = {
        z: data.grid,
        x: data.dec_labels,
        y: data.ra_labels,
        type: 'heatmap',
        colorscale: [
            [0, '#0b1029'],
            [0.01, '#111738'],
            [0.1, '#1a365d'],
            [0.3, '#0ea5e9'],
            [0.6, '#8b5cf6'],
            [1, '#ec4899'],
        ],
        showscale: true,
        colorbar: {
            title: { text: 'Visits', font: PLOTLY_FONT },
            tickfont: PLOTLY_FONT,
            bordercolor: PLOTLY_GRID,
            len: 0.8,
            thickness: 12,
        },
        hovertemplate: 'RA: %{y}°<br>Dec: %{x}°<br>Visits: %{z}<extra></extra>',
    };

    const layout = {
        paper_bgcolor: PLOTLY_PAPER,
        plot_bgcolor: PLOTLY_BG,
        font: PLOTLY_FONT,
        margin: { t: 10, b: 40, l: 50, r: 10 },
        xaxis: {
            title: 'Declination (°)',
            tickfont: PLOTLY_FONT,
        },
        yaxis: {
            title: 'RA (°)',
            tickfont: PLOTLY_FONT,
        },
    };

    Plotly.react('skymap-heatmap', [trace], layout, { responsive: true, displayModeBar: false, staticPlot: true });
}


async function updateToolChart() {
    const data = await fetchJSON('/api/tool-stats');
    if (!data) return;
    if (!dataChanged('toolchart', data)) return;

    const tools = data.tool_regions;
    const toolNames = Object.keys(tools);
    const toolCounts = Object.values(tools);

    const toolColors = {
        search_region:   '#4b96d4',
        search_target:   '#4b96d4',
        multi_epoch:     '#4b96d4',
        simbad_check:    '#e6a023',
        ztf_lightcurve:  '#e05577',
        download_cutout: '#5dc77a',
        detect_sources:  '#00d4ff',
        compare_images:  '#b07de8',
        analyze_image:   '#eab308',
        convert_to_png:  '#8899aa',
        list_images:     '#5f9ea0',
        log_finding:     '#ec4899',
        query_memory:    '#06d6a0',
        list_findings:   '#06d6a0',
        list_unexplored: '#06d6a0',
    };

    const colors = toolNames.map(t => toolColors[t] || '#6b7db3');

    const trace = {
        labels: toolNames,
        values: toolCounts,
        type: 'pie',
        hole: 0.55,
        marker: { colors: colors, line: { color: PLOTLY_BG, width: 2 } },
        textfont: { ...PLOTLY_FONT, size: 10, color: '#c8d6e5' },
        textposition: 'inside',
        hovertemplate: '<b>%{label}</b><br>Regions: %{value}<br>%{percent}<extra></extra>',
        sort: false,
    };

    const layout = {
        paper_bgcolor: PLOTLY_PAPER,
        plot_bgcolor: PLOTLY_BG,
        font: PLOTLY_FONT,
        margin: { t: 10, b: 10, l: 10, r: 10 },
        showlegend: true,
        legend: {
            font: { ...PLOTLY_FONT, size: 9 },
            bgcolor: 'rgba(0,0,0,0)',
            x: 1.05,
        },
    };

    Plotly.react('tool-chart', [trace], layout, { responsive: true, displayModeBar: false, staticPlot: true });
}


async function updateTimeline() {
    const findings = await fetchJSON('/api/findings');
    if (!findings || findings.length === 0) return;
    if (!dataChanged('timeline', findings)) return;

    // Group findings by hour for timeline bars
    const sigColors = { high: '#ef4444', medium: '#f59e0b', low: '#22c55e' };

    // Build cumulative count over time
    const sorted = [...findings].reverse(); // chronological
    const times = sorted.map(f => f.timestamp || '');
    const cumHigh = [], cumMed = [], cumLow = [];
    let h = 0, m = 0, l = 0;
    for (const f of sorted) {
        if (f.significance === 'high') h++;
        else if (f.significance === 'low') l++;
        else m++;
        cumHigh.push(h);
        cumMed.push(m);
        cumLow.push(l);
    }

    const traces = [
        { x: times, y: cumHigh, name: 'High', fill: 'tozeroy', line: { color: '#ef4444', width: 2 }, fillcolor: 'rgba(239,68,68,0.15)' },
        { x: times, y: cumMed, name: 'Medium', fill: 'tozeroy', line: { color: '#f59e0b', width: 2 }, fillcolor: 'rgba(245,158,11,0.1)' },
        { x: times, y: cumLow, name: 'Low', fill: 'tozeroy', line: { color: '#22c55e', width: 2 }, fillcolor: 'rgba(34,197,94,0.08)' },
    ];

    const layout = {
        paper_bgcolor: PLOTLY_PAPER,
        plot_bgcolor: PLOTLY_BG,
        font: PLOTLY_FONT,
        margin: { t: 10, b: 35, l: 40, r: 10 },
        xaxis: { gridcolor: PLOTLY_GRID, tickfont: PLOTLY_FONT },
        yaxis: { title: 'Cumulative', gridcolor: PLOTLY_GRID, tickfont: PLOTLY_FONT },
        showlegend: true,
        legend: { x: 0.01, y: 0.99, bgcolor: 'rgba(17,23,56,0.8)', bordercolor: PLOTLY_GRID, font: { ...PLOTLY_FONT, size: 10 } },
    };

    Plotly.react('timeline-chart', traces, layout, { responsive: true, displayModeBar: false, staticPlot: true });
}


async function updateFindings() {
    const findings = await fetchJSON('/api/findings');
    if (!findings) return;

    document.getElementById('findings-badge').textContent = `${findings.length} findings`;

    const tbody = document.getElementById('findings-body');
    tbody.innerHTML = findings.map(f => {
        const sig = f.significance || 'medium';
        const sigClass = sig === 'high' ? 'sig-high' : sig === 'low' ? 'sig-low' : 'sig-medium';
        const ts = f.timestamp || '';
        const timeStr = ts.includes(' ') ? ts.split(' ')[1] : ts;
        const dateStr = ts.includes(' ') ? ts.split(' ')[0] : '';
        return `<tr>
            <td><span class="sig-badge ${sigClass}">${sig}</span></td>
            <td><span style="color:var(--text-dim)">${dateStr}</span> ${timeStr}</td>
            <td><span class="coord-mono">${parseFloat(f.ra || 0).toFixed(2)}, ${parseFloat(f.dec || 0).toFixed(2)}</span></td>
            <td style="max-width:500px">${escapeHtml((f.description || '').slice(0, 200))}</td>
            <td style="color:var(--text-dim);font-size:10px">${f.id || ''}</td>
        </tr>`;
    }).join('');
}


async function updateLog() {
    const lines = await fetchJSON('/api/log?n=80');
    if (!lines) return;

    const panel = document.getElementById('log-panel');
    const wasAtBottom = panel.scrollTop + panel.clientHeight >= panel.scrollHeight - 30;

    panel.innerHTML = lines.map(line => {
        // Parse: [2026-03-10 15:54:29] [LEVEL] message
        const match = line.match(/^\[([^\]]+)\]\s*\[([^\]]+)\]\s*(.*)/);
        if (!match) return `<div class="log-line">${escapeHtml(line)}</div>`;

        const ts = match[1];
        const level = match[2];
        const msg = match[3];

        const levelClass = {
            'INFO': 'log-lvl-info',
            'OK': 'log-lvl-ok',
            'WARN': 'log-lvl-warn',
            'ERROR': 'log-lvl-error',
            'TOOL': 'log-lvl-tool',
            'FIND': 'log-lvl-find',
            'THINK': 'log-lvl-think',
        }[level] || 'log-lvl-info';

        const time = ts.includes(' ') ? ts.split(' ')[1] : ts;

        return `<div class="log-line"><span class="log-ts">${time}</span> <span class="${levelClass}">[${level}]</span> ${escapeHtml(msg)}</div>`;
    }).join('');

    // Auto-scroll to bottom if user was already there
    if (wasAtBottom) {
        panel.scrollTop = panel.scrollHeight;
    }
}


async function updateExplorationPace() {
    const data = await fetchJSON('/api/exploration-pace');
    if (!data || data.cycles.length === 0) {
        document.getElementById('pace-badge').textContent = 'No data yet';
        return;
    }
    if (!dataChanged('pace', data)) return;

    const lastRegions = data.cum_regions[data.cum_regions.length - 1] || 0;
    const lastFindings = data.cum_findings[data.cum_findings.length - 1] || 0;
    document.getElementById('pace-badge').textContent =
        `${lastRegions} regions · ${lastFindings} findings`;

    const traces = [
        {
            x: data.cycles, y: data.cum_regions,
            name: 'Regions',
            fill: 'tozeroy',
            line: { color: '#00d4ff', width: 2 },
            fillcolor: 'rgba(0,212,255,0.1)',
        },
        {
            x: data.cycles, y: data.cum_findings,
            name: 'Findings',
            fill: 'tozeroy',
            line: { color: '#ec4899', width: 2 },
            fillcolor: 'rgba(236,72,153,0.1)',
            yaxis: 'y2',
        },
    ];

    const layout = {
        paper_bgcolor: PLOTLY_PAPER,
        plot_bgcolor: PLOTLY_BG,
        font: PLOTLY_FONT,
        margin: { t: 10, b: 40, l: 50, r: 50 },
        xaxis: {
            title: 'Cycle',
            gridcolor: PLOTLY_GRID,
            tickfont: PLOTLY_FONT,
        },
        yaxis: {
            title: 'Regions',
            titlefont: { color: '#00d4ff', ...PLOTLY_FONT },
            gridcolor: PLOTLY_GRID,
            tickfont: { ...PLOTLY_FONT, color: '#00d4ff' },
        },
        yaxis2: {
            title: 'Findings',
            titlefont: { color: '#ec4899', ...PLOTLY_FONT },
            tickfont: { ...PLOTLY_FONT, color: '#ec4899' },
            overlaying: 'y',
            side: 'right',
            gridcolor: 'transparent',
        },
        showlegend: true,
        legend: {
            x: 0.01, y: 0.99,
            bgcolor: 'rgba(17,23,56,0.8)',
            bordercolor: PLOTLY_GRID,
            font: { ...PLOTLY_FONT, size: 10 },
        },
    };

    Plotly.react('pace-chart', traces, layout, { responsive: true, displayModeBar: false, staticPlot: true });
}


async function updateRegionDepth() {
    const data = await fetchJSON('/api/region-depth');
    if (!data || data.total_regions === 0) return;
    if (!dataChanged('depth', data)) return;

    const binNames = Object.keys(data.bins);
    const binValues = Object.values(data.bins);
    const barColors = ['#1a365d', '#0ea5e9', '#06b6d4', '#8b5cf6', '#a855f7', '#ec4899'];

    const trace = {
        x: binNames,
        y: binValues,
        type: 'bar',
        marker: {
            color: barColors,
            line: { color: PLOTLY_BG, width: 1 },
        },
        hovertemplate: '%{x} visits: <b>%{y} regions</b><extra></extra>',
    };

    const layout = {
        paper_bgcolor: PLOTLY_PAPER,
        plot_bgcolor: PLOTLY_BG,
        font: PLOTLY_FONT,
        margin: { t: 10, b: 40, l: 40, r: 10 },
        xaxis: {
            title: 'Visits per Region',
            tickfont: PLOTLY_FONT,
            gridcolor: 'transparent',
        },
        yaxis: {
            title: 'Regions',
            gridcolor: PLOTLY_GRID,
            tickfont: PLOTLY_FONT,
        },
        annotations: [{
            x: 0.98, y: 0.95,
            xref: 'paper', yref: 'paper',
            text: `Avg: ${data.avg_visits} · Max: ${data.max_visits}`,
            showarrow: false,
            font: { ...PLOTLY_FONT, size: 11, color: '#c8d6e5' },
            bgcolor: 'rgba(17,23,56,0.8)',
            bordercolor: PLOTLY_GRID,
            borderwidth: 1,
            borderpad: 4,
        }],
    };

    Plotly.react('depth-chart', [trace], layout, { responsive: true, displayModeBar: false, staticPlot: true });
}


async function updateRealityCheck() {
    const data = await fetchJSON('/api/status');
    if (!data) return;
    if (!dataChanged('reality', data)) return;

    const regions = data.n_regions;
    const area = data.sampled_area_sqdeg;
    const pct = data.coverage_pct;
    const fullSky = 41253;

    // Fun comparisons
    const fullMoonArea = 0.2;  // sq deg
    const moonsEquiv = (area / fullMoonArea).toFixed(1);
    const yearsToFullSky = regions > 0
        ? Math.round(fullSky / (area || 0.001) * (data.total_cycles / Math.max(regions, 1)) * 5 / 3600 / 24 / 365)
        : '?';

    const body = document.getElementById('reality-body');
    body.innerHTML = `
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">
            <div style="text-align:center;padding:12px;background:var(--bg-card-alt);border-radius:8px;border:1px solid var(--border);">
                <div style="font-size:22px;font-weight:800;color:var(--accent);">${area}</div>
                <div style="font-size:10px;color:var(--text-dim);margin-top:2px;">sq° sampled</div>
            </div>
            <div style="text-align:center;padding:12px;background:var(--bg-card-alt);border-radius:8px;border:1px solid var(--border);">
                <div style="font-size:22px;font-weight:800;color:var(--purple);">${pct}%</div>
                <div style="font-size:10px;color:var(--text-dim);margin-top:2px;">of full sky</div>
            </div>
            <div style="text-align:center;padding:12px;background:var(--bg-card-alt);border-radius:8px;border:1px solid var(--border);">
                <div style="font-size:22px;font-weight:800;color:var(--amber);">${moonsEquiv}</div>
                <div style="font-size:10px;color:var(--text-dim);margin-top:2px;">full moons of sky</div>
            </div>
            <div style="text-align:center;padding:12px;background:var(--bg-card-alt);border-radius:8px;border:1px solid var(--border);">
                <div style="font-size:22px;font-weight:800;color:var(--pink);">${regions}</div>
                <div style="font-size:10px;color:var(--text-dim);margin-top:2px;">unique pointings</div>
            </div>
        </div>
        <div style="margin-top:12px;padding:10px;background:rgba(139,92,246,0.08);border:1px solid rgba(139,92,246,0.2);border-radius:8px;font-size:11px;color:var(--text);">
            <span style="color:var(--purple);font-weight:700;">Reality:</span>
            The observable sky is ${fullSky.toLocaleString()} sq°. Each Pan-STARRS pointing covers ~0.008 sq°.
            Even professional surveys like ZTF take months to cover the sky once.
            Quality > coverage — deep investigation of fewer regions finds more real transients.
        </div>
    `;
}


// ── HTML escaping ──
function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}


// ================================================================
// REFRESH LOOP
// ================================================================

async function refreshAll() {
    await Promise.all([
        updateStatus(),
        updateSkyMap(),
        updateHeatmap(),
        updateToolChart(),
        updateExplorationPace(),
        updateRegionDepth(),
        updateRealityCheck(),
        updateFindings(),
        updateTimeline(),
        updateLog(),
    ]);
}

// Initial load
refreshAll();

// Periodic refresh
setInterval(refreshAll, REFRESH_INTERVAL);

</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AstroResearch Dashboard")
    parser.add_argument("--port", type=int, default=5555, help="Port to run on (default: 5555)")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)")
    parser.add_argument("--debug", action="store_true", help="Run in debug mode")
    args = parser.parse_args()

    print(f"""
    ╔══════════════════════════════════════════════════════════╗
    ║   🌌  AstroResearch Dashboard                           ║
    ║   Open: http://{args.host}:{args.port}                        ║
    ╚══════════════════════════════════════════════════════════╝
    """)

    app.run(host=args.host, port=args.port, debug=args.debug)
