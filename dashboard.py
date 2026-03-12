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

from config import (
    DATA_DIR, FINDINGS_FILE, RESEARCH_LOG,
    DASHBOARD_STATUS, MEMORY_FILE,
)

from flask import Flask, jsonify, Response, request

# Alias for backward compat (dashboard used STATUS_FILE)
STATUS_FILE = DASHBOARD_STATUS

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
    EXPECTED_FIELDS = ["id", "timestamp", "ra", "dec", "significance", "description"]
    findings = []
    try:
        with open(FINDINGS_FILE, "r", encoding="utf-8") as f:
            # Skip blank lines at start
            lines = [l for l in f if l.strip()]
            if not lines:
                return findings
            # Check if first line is a header
            first = lines[0].strip().split("\t")
            if first[0].lower() in ("id", "finding_id"):
                # Has header — use DictReader
                import io
                reader = csv.DictReader(io.StringIO("".join(lines)), delimiter="\t")
            else:
                # No header — assign field names manually
                import io
                reader = csv.DictReader(
                    io.StringIO("".join(lines)), delimiter="\t",
                    fieldnames=EXPECTED_FIELDS
                )
            for row in reader:
                # Skip rows with missing essential fields
                if row.get("timestamp") and row.get("ra"):
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
            "exhausted": reg.get("exhausted", False),
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


@app.route("/api/recent-images")
def api_recent_images():
    """Return the 5 most recently modified PNGs (by mtime)."""
    png_dir = os.path.join(DATA_DIR, "png")
    if not os.path.isdir(png_dir):
        return jsonify({"images": []})
    files = []
    for f in os.listdir(png_dir):
        if f.lower().endswith(".png"):
            full = os.path.join(png_dir, f)
            sz = os.path.getsize(full)
            if sz > 500:  # skip tiny placeholder files
                files.append((f, os.path.getmtime(full)))
    files.sort(key=lambda x: x[1], reverse=True)
    return jsonify({"images": [f[0] for f in files[:5]]})


@app.route("/png/<path:filename>")
def serve_png(filename):
    """Serve a PNG image from data/png/."""
    png_dir = os.path.join(DATA_DIR, "png")
    fpath = os.path.join(png_dir, filename)
    if not os.path.isfile(fpath):
        return "Not found", 404
    return Response(open(fpath, "rb").read(), mimetype="image/png")


# ---------------------------------------------------------------------------
# Main HTML Page
# ---------------------------------------------------------------------------

# Load HTML template from file
_TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "dashboard.html")
with open(_TEMPLATE_PATH, "r", encoding="utf-8") as _f:
    HTML_PAGE = _f.read()

@app.route("/")
def index():
    return Response(HTML_PAGE, mimetype="text/html")


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
