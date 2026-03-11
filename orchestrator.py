"""
AstroResearch Orchestrator — Autonomous Astronomical Anomaly Detection

Uses Qwen 3.5 (via Ollama) as a research agent that:
  1. Proposes sky regions and analysis strategies
  2. Calls tools to query archives, download images, analyze data
  3. Cross-references findings with known catalogs
  4. Logs potential discoveries for human review

Inspired by nanobot/OpenClaw tool-calling pattern:
  LLM outputs TOOL: function_name(params)
  Orchestrator parses and executes Python scripts
  Results feed back to LLM for next decision

Usage:
    python orchestrator.py --target "Crab Nebula" --max-cycles 5
    python orchestrator.py --model qwen3.5:4b --max-cycles 0
"""

import os
import sys
import json
import re
import time
import datetime
import shlex
import subprocess
import argparse
import random
import base64
import requests

# Force UTF-8 output on Windows (for emoji & ANSI box-drawing)
if sys.platform == "win32":
    os.system("")   # Enable ANSI escape sequences on Windows 10+
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OLLAMA_URL = "http://localhost:11434/api/chat"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
FINDINGS_DIR = os.path.join(BASE_DIR, "findings")
FINDINGS_FILE = os.path.join(BASE_DIR, "findings.tsv")
RESEARCH_LOG = os.path.join(BASE_DIR, "research.log")
DASHBOARD_STATUS = os.path.join(BASE_DIR, "dashboard_status.json")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(FINDINGS_DIR, exist_ok=True)


def _write_dashboard_status(running=True, cycle=0, phase="idle", current_tool=None, last_thought=""):
    """Write a small status file for the dashboard to read."""
    try:
        status = {
            "running": running,
            "cycle": cycle,
            "phase": phase,
            "current_tool": current_tool,
            "last_thought": last_thought[:200] if last_thought else "",
            "timestamp": datetime.datetime.now().isoformat(),
        }
        tmp = DASHBOARD_STATUS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(status, f)
        os.replace(tmp, DASHBOARD_STATUS)
    except Exception:
        pass  # Never crash for dashboard

MEMORY_FILE = os.path.join(BASE_DIR, "memory.json")

# ---------------------------------------------------------------------------
# Persistent Memory — survives across runs
# ---------------------------------------------------------------------------

def _empty_memory():
    """Return a blank memory structure."""
    return {
        "version": 1,
        "created": datetime.datetime.now().isoformat(),
        "last_updated": datetime.datetime.now().isoformat(),
        "total_cycles_all_runs": 0,
        "run_count": 0,
        "regions": {},          # key = "ra,dec" → region dict
        "known_failures": {},   # key = "tool|ra,dec" → {error, count, last_cycle}
        "best_leads": [],       # top unresolved findings worth revisiting
        "sky_coverage": {       # rough coverage tracking
            "dec_min": 90.0,
            "dec_max": -90.0,
            "ra_bins_visited": [],  # list of 10-degree RA bins touched
        },
    }


def load_memory():
    """Load persistent memory from disk, or create fresh if missing."""
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                mem = json.load(f)
            # Migrate old versions if needed
            if "version" not in mem:
                mem["version"] = 1
            if "sky_coverage" not in mem:
                mem["sky_coverage"] = {"dec_min": 90.0, "dec_max": -90.0, "ra_bins_visited": []}
            # v2 migration: add notes and exhausted fields to existing regions
            for key, reg in mem.get("regions", {}).items():
                if "notes" not in reg:
                    reg["notes"] = []
                if "exhausted" not in reg:
                    reg["exhausted"] = False
            return mem
        except Exception as e:
            print(f"[WARN] Could not load memory.json: {e} — starting fresh")
    return _empty_memory()


def save_memory(mem):
    """Persist memory to disk."""
    mem["last_updated"] = datetime.datetime.now().isoformat()
    try:
        tmp = MEMORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(mem, f, indent=2, default=str)
        # Atomic-ish rename (Windows: replaces if exists on Python 3.3+)
        os.replace(tmp, MEMORY_FILE)
    except Exception as e:
        print(f"[ERROR] Could not save memory.json: {e}")


def update_memory(mem, cycle_num, tool_calls_executed, visited_regions):
    """Update memory after a cycle completes.

    Parameters
    ----------
    mem : dict                   The memory object (mutated in place).
    cycle_num : int              Current cycle number within this run.
    tool_calls_executed : list   List of {"tool": str, "params": dict, "result": dict}.
    visited_regions : dict       The orchestrator's {(ra,dec): count} map.
    """
    mem["total_cycles_all_runs"] += 1

    for item in tool_calls_executed:
        tool_name = item.get("tool", "")
        params = item.get("params", {})
        result = item.get("result", {})
        ra = params.get("ra")
        dec = params.get("dec")

        if ra is None or dec is None:
            continue

        region_key = f"{round(float(ra),1)},{round(float(dec),1)}"

        # ------ Update region entry ------
        if region_key not in mem["regions"]:
            mem["regions"][region_key] = {
                "ra": round(float(ra), 2),
                "dec": round(float(dec), 2),
                "visits": 0,
                "tools_used": [],
                "outcomes": [],
                "findings": [],
                "notes": [],
                "exhausted": False,
                "first_cycle": cycle_num,
                "last_cycle": cycle_num,
            }
        reg = mem["regions"][region_key]
        reg["visits"] += 1
        reg["last_cycle"] = cycle_num
        if tool_name not in reg["tools_used"]:
            reg["tools_used"].append(tool_name)

        # Record compact outcome
        if "error" in result:
            err_short = str(result["error"])[:80]
            reg["outcomes"].append(f"{tool_name}: ERROR {err_short}")
            # Track failure
            fail_key = f"{tool_name}|{region_key}"
            if fail_key not in mem["known_failures"]:
                mem["known_failures"][fail_key] = {"error": err_short, "count": 0, "last_cycle": cycle_num}
            mem["known_failures"][fail_key]["count"] += 1
            mem["known_failures"][fail_key]["last_cycle"] = cycle_num
        elif tool_name == "simbad_check":
            n = result.get("n_known_objects", 0)
            reg["outcomes"].append(f"simbad: {n} known objects")
        elif tool_name == "search_region":
            n = result.get("total_results", 0)
            reg["outcomes"].append(f"mast: {n} observations")
        elif tool_name == "multi_epoch":
            n = result.get("n_groups", 0)
            reg["outcomes"].append(f"multi_epoch: {n} groups")
        elif tool_name == "download_multiepoch":
            n = result.get("n_images", 0)
            baseline = result.get("time_baseline_days", "?")
            reg["outcomes"].append(f"multiepoch: {n} epochs, {baseline}d baseline")
        elif tool_name == "download_legacy":
            n = result.get("n_images", 0)
            reg["outcomes"].append(f"legacy: {n} bands downloaded")
        elif tool_name == "query_gaia":
            n = result.get("n_sources", 0)
            reg["outcomes"].append(f"gaia: {n} sources")
        elif tool_name == "check_transients":
            n = result.get("n_matches", 0)
            reg["outcomes"].append(f"alerce: {n} known transients")
        elif tool_name == "measure_photometry":
            mag = result.get("magnitude")
            snr = result.get("snr")
            if mag is not None:
                reg["outcomes"].append(f"photometry: mag={mag}, SNR={snr}")
            else:
                reg["outcomes"].append("photometry: source not detected")
        elif tool_name == "log_finding":
            fid = result.get("finding_id", "?")
            sig = params.get("significance", "medium")
            desc = params.get("description", "")[:100]
            reg["findings"].append(fid)
            reg["outcomes"].append(f"finding {fid} ({sig})")
            # If high significance, add to best_leads
            if sig == "high":
                lead = {"ra": round(float(ra), 2), "dec": round(float(dec), 2),
                        "finding_id": fid, "why": desc, "cycle": cycle_num}
                # Avoid duplicate leads for same region
                existing_ras = [l["ra"] for l in mem["best_leads"]]
                existing_decs = [l["dec"] for l in mem["best_leads"]]
                if not any(abs(l["ra"] - lead["ra"]) < 1 and abs(l["dec"] - lead["dec"]) < 1
                           for l in mem["best_leads"]):
                    mem["best_leads"].append(lead)
                # Keep list manageable
                if len(mem["best_leads"]) > 20:
                    mem["best_leads"] = mem["best_leads"][-20:]

        # Keep outcomes list from growing forever (last 5 per region)
        if len(reg["outcomes"]) > 5:
            reg["outcomes"] = reg["outcomes"][-5:]

        # ------ Sky coverage ------
        try:
            dec_f = float(dec)
            ra_f = float(ra)
            if dec_f < mem["sky_coverage"]["dec_min"]:
                mem["sky_coverage"]["dec_min"] = dec_f
            if dec_f > mem["sky_coverage"]["dec_max"]:
                mem["sky_coverage"]["dec_max"] = dec_f
            ra_bin = int(ra_f / 10) * 10  # 0, 10, 20, ... 350
            if ra_bin not in mem["sky_coverage"]["ra_bins_visited"]:
                mem["sky_coverage"]["ra_bins_visited"].append(ra_bin)
                mem["sky_coverage"]["ra_bins_visited"].sort()
        except (ValueError, TypeError):
            pass

    return mem


def summarize_memory(mem, max_tokens=400):
    """Generate a compact text summary of memory for prompt injection.

    Aims for ~200-400 tokens: enough context without eating Qwen's budget.
    """
    lines = []
    n_regions = len(mem["regions"])
    total_cycles = mem["total_cycles_all_runs"]
    n_runs = mem["run_count"]

    lines.append(f"=== PERSISTENT MEMORY ({n_regions} regions explored across {total_cycles} cycles, {n_runs} run(s)) ===")

    # Best leads (most valuable — show first), skip dismissed/exhausted
    if mem["best_leads"]:
        lines.append("\n## Priority Leads (high-significance, unresolved):")
        for lead in mem["best_leads"][-5:]:  # last 5
            lines.append(f"  - RA={lead['ra']}, Dec={lead['dec']}: {lead['why'][:80]}")
        if not mem["best_leads"]:
            lines.append("  (none — all leads dismissed or exhausted)")

    # Exhausted regions (so Qwen knows to skip them)
    exhausted_regions = [r for r in mem.get("regions", {}).values() if r.get("exhausted")]
    if exhausted_regions:
        lines.append(f"\n## Exhausted regions (fully investigated — DO NOT revisit):")
        for reg in exhausted_regions[:8]:
            last_note = ""
            if reg.get("notes"):
                # Find the EXHAUSTED note
                for n in reversed(reg["notes"]):
                    if n["text"].startswith("[EXHAUSTED]"):
                        last_note = n["text"][12:60]
                        break
            lines.append(f"  - RA={reg['ra']}, Dec={reg['dec']}: {last_note}")

    # Most-visited regions (potential stuck points) — exclude exhausted
    if mem["regions"]:
        active_regions = [r for r in mem["regions"].values() if not r.get("exhausted")]
        sorted_regions = sorted(active_regions, key=lambda r: r["visits"], reverse=True)

        # Top 5 most visited (active only)
        if sorted_regions:
            lines.append(f"\n## Most investigated regions (active):")
            for reg in sorted_regions[:5]:
                tools_str = ", ".join(reg["tools_used"][:4])
                last_outcome = reg["outcomes"][-1] if reg["outcomes"] else "no data"
                note_hint = ""
                if reg.get("notes"):
                    last_note_text = reg["notes"][-1]["text"][:50]
                    note_hint = f" 📝 {last_note_text}"
                lines.append(f"  - RA={reg['ra']}, Dec={reg['dec']} ({reg['visits']}x) — {last_outcome}{note_hint}")

        # Regions with findings (include exhausted here for reference)
        all_sorted = sorted(mem["regions"].values(), key=lambda r: r["visits"], reverse=True)
        regions_with_findings = [r for r in all_sorted if r["findings"]]
        if regions_with_findings:
            lines.append(f"\n## Regions with logged findings ({len(regions_with_findings)}):")
            for reg in regions_with_findings[:5]:
                tag = " [EXHAUSTED]" if reg.get("exhausted") else ""
                lines.append(f"  - RA={reg['ra']}, Dec={reg['dec']}: {len(reg['findings'])} finding(s){tag}")

    # Known failures (so Qwen doesn't retry broken things — but they expire)
    FAILURE_TTL = 50  # failures expire after 50 cycles — APIs recover, worth retrying
    current_cycle = mem["total_cycles_all_runs"]
    active_failures = {
        k: v for k, v in mem["known_failures"].items()
        if v["count"] >= 2 and (current_cycle - v["last_cycle"]) < FAILURE_TTL
    }
    if active_failures:
        lines.append(f"\n## Known failures (don't retry yet — will expire after {FAILURE_TTL} cycles):")
        for key, fail in list(active_failures.items())[:8]:
            age = current_cycle - fail["last_cycle"]
            lines.append(f"  - {key}: {fail['error'][:60]} ({fail['count']}x, {age} cycles ago)")

    # Sky coverage gaps
    cov = mem["sky_coverage"]
    ra_bins = cov.get("ra_bins_visited", [])
    all_bins = set(range(0, 360, 10))
    missing_bins = sorted(all_bins - set(ra_bins))
    if missing_bins and len(ra_bins) > 3:
        # Group consecutive missing bins into ranges
        gap_strs = []
        i = 0
        while i < len(missing_bins) and len(gap_strs) < 4:
            start = missing_bins[i]
            end = start
            while i + 1 < len(missing_bins) and missing_bins[i + 1] == end + 10:
                i += 1
                end = missing_bins[i]
            gap_strs.append(f"RA {start}°–{end + 10}°")
            i += 1
        lines.append(f"\n## Sky coverage gaps (unexplored RA ranges):")
        lines.append(f"  Dec range covered: {cov['dec_min']:.0f}° to {cov['dec_max']:.0f}°")
        lines.append(f"  Unexplored RA bands: {', '.join(gap_strs)}")
        if cov["dec_min"] > -20:
            lines.append(f"  ⚠ Southern sky (Dec < -20°) barely explored!")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Anti-loop: exploration state
# ---------------------------------------------------------------------------

# Seed targets to force diversity when Qwen gets stuck
SEED_TARGETS = [
    {"name": "Crab Nebula", "ra": 83.63, "dec": 22.01},
    {"name": "Orion Nebula", "ra": 83.82, "dec": -5.39},
    {"name": "M31 Andromeda", "ra": 10.68, "dec": 41.27},
    {"name": "M87 Virgo A", "ra": 187.71, "dec": 12.39},
    {"name": "Cassiopeia A", "ra": 350.85, "dec": 58.81},
    {"name": "Vela Pulsar region", "ra": 128.84, "dec": -45.18},
    {"name": "Cygnus X-1", "ra": 299.59, "dec": 35.20},
    {"name": "Kepler SNR", "ra": 262.67, "dec": -21.49},
    {"name": "NGC 6397 (globular)", "ra": 265.17, "dec": -53.67},
    {"name": "Tycho SNR", "ra": 6.34, "dec": 64.14},
    {"name": "M1 (Taurus)", "ra": 83.63, "dec": 22.01},
    {"name": "NGC 1275 (Perseus A)", "ra": 49.95, "dec": 41.51},
    {"name": "Eta Carinae", "ra": 161.27, "dec": -59.68},
    {"name": "Geminga Pulsar", "ra": 98.48, "dec": 17.77},
    {"name": "3C 273 Quasar", "ra": 187.28, "dec": 2.05},
    {"name": "Bootes Void edge", "ra": 218.0, "dec": 46.0},
    {"name": "M51 Whirlpool", "ra": 202.47, "dec": 47.20},
    {"name": "NGC 4993 (GW170817)", "ra": 197.45, "dec": -23.38},
    {"name": "Sgr A* vicinity", "ra": 266.42, "dec": -29.01},
    {"name": "LMC center", "ra": 80.89, "dec": -69.76},
]

def _region_key(ra, dec, precision=1):
    """Round coords to create a region bucket key."""
    return (round(float(ra), precision), round(float(dec), precision))

def _tool_cache_key(tool_name, params):
    """Create a hashable key for a tool call based on name + numeric params."""
    ra = params.get("ra", None)
    dec = params.get("dec", None)
    radius = params.get("radius", params.get("size", None))
    return (tool_name, round(float(ra), 2) if ra else None,
            round(float(dec), 2) if dec else None,
            round(float(radius), 2) if radius else None)

AVAILABLE_TOOLS = {
    "search_region": {
        "description": "Search MAST archives (JWST, Hubble) for observations at coordinates",
        "usage": "search_region(ra=<degrees>, dec=<degrees>, radius=<degrees>)",
        "script": "tools/astro_query.py search-region --ra {ra} --dec {dec} --radius {radius}",
    },
    "search_target": {
        "description": "Search MAST archives by target name (resolves name to coordinates)",
        "usage": "search_target(name='<target name>')",
        "script": "tools/astro_query.py search-target '{name}'",
    },
    "multi_epoch": {
        "description": "Find multi-epoch observations of the same region (for temporal comparison)",
        "usage": "multi_epoch(ra=<degrees>, dec=<degrees>, radius=<degrees>)",
        "script": "tools/astro_query.py multi-epoch --ra {ra} --dec {dec} --radius {radius}",
    },
    # "ztf_lightcurve": {  # DISABLED — endpoint consistently times out, wastes Qwen cycles
    #     "description": "Get ZTF light curve data. SLOW (may take 2-3 min). Use radius=5 arcsec. If it times out, skip ZTF for this target.",
    #     "usage": "ztf_lightcurve(ra=<degrees>, dec=<degrees>, radius=5)",
    #     "script": "tools/astro_query.py ztf-lightcurve --ra {ra} --dec {dec} --radius {radius}",
    # },
    "simbad_check": {
        "description": "Check SIMBAD catalog for known objects at a position",
        "usage": "simbad_check(ra=<degrees>, dec=<degrees>, radius=<arcsec>)",
        "script": "tools/astro_query.py simbad-check --ra {ra} --dec {dec} --radius {radius}",
    },
    "detect_sources": {
        "description": "Detect point sources in a FITS image. Use list_images first to get valid file paths!",
        "usage": "detect_sources(image='data/images/<file>.jpg', sigma=<threshold>)",
        "script": "tools/image_analysis.py detect-sources --image {image} --sigma {sigma}",
    },
    "compare_images": {
        "description": "Compare two images to find pixel-level differences. NOTE: Comparing different bands (g vs r) reveals COLOR anomalies (unusual spectral shape), NOT temporal changes. To find real transients, compare same-band images from different epochs. Use list_images first to get file paths!",
        "usage": "compare_images(img1='data/images/<file1>.fits', img2='data/images/<file2>.fits')",
        "script": "tools/image_analysis.py compare --img1 {img1} --img2 {img2}",
    },
    "download_cutout": {
        "description": "Download a real sky image cutout from Pan-STARRS survey (STACKED — single epoch per band). Saves FITS files to data/images/",
        "usage": "download_cutout(ra=<degrees>, dec=<degrees>, size=<arcmin>)",
        "script": "tools/astro_query.py download-cutout --ra {ra} --dec {dec} --size {size}",
    },
    "download_multiepoch": {
        "description": "Download multi-epoch images from Pan-STARRS warps (individual exposures at DIFFERENT DATES). Use this to get REAL temporal data for transient detection! Returns same-band images from different epochs — compare them with compare_images.",
        "usage": "download_multiepoch(ra=<degrees>, dec=<degrees>, filter='g', epochs=3)",
        "script": "tools/astro_query.py download-multiepoch --ra {ra} --dec {dec} --filter {filter} --epochs {epochs} --size {size}",
    },
    "download_legacy": {
        "description": "Download cutout from DESI Legacy Survey DR10 (independent survey, ~5yr baseline from Pan-STARRS). Compare same-band Legacy vs Pan-STARRS images to cross-check transient candidates. Coverage: Dec roughly -18 to +84.",
        "usage": "download_legacy(ra=<degrees>, dec=<degrees>, bands='grz')",
        "script": "tools/astro_query.py download-legacy --ra {ra} --dec {dec} --bands {bands} --size {size}",
    },
    "list_images": {
        "description": "List all downloaded FITS images in data/images/. Call this BEFORE using detect_sources, compare_images, or analyze_image to get real file paths!",
        "usage": "list_images()",
        "script": "tools/image_analysis.py list-images",
    },
    "analyze_image": {
        "description": "Visually analyze a sky image (FITS or JPEG). The image will be shown to YOU directly in the next cycle so you can see it with full context of your research. Use list_images first to get valid paths!",
        "usage": "analyze_image(image='data/images/<file>.fits', prompt='<optional analysis focus>')",
        "script": "__internal__",
    },
    "convert_to_png": {
        "description": "Convert any sky image (FITS or JPEG) to PNG for visual inspection. Saved to data/png/",
        "usage": "convert_to_png(image='data/images/<file>.fits')",
        "script": "tools/image_analysis.py to-png --image {image}",
    },
    "log_finding": {
        "description": "Log a potential discovery or interesting finding",
        "usage": "log_finding(ra=<deg>, dec=<deg>, description='<text>', significance='<low|medium|high>')",
        "script": "__internal__",
    },
    "query_memory": {
        "description": "Look up your past exploration data for a region. Returns full history: tools used, outcomes, findings, visit count. Use this BEFORE revisiting a region to see what you already did there!",
        "usage": "query_memory(ra=<degrees>, dec=<degrees>, radius=5.0)",
        "script": "__internal__",
    },
    "list_findings": {
        "description": "List all logged findings from memory. Filter by significance level. Use this to review your best discoveries and decide what to follow up on.",
        "usage": "list_findings(significance='all')",
        "script": "__internal__",
    },
    "list_unexplored": {
        "description": "Show unexplored sky regions and coverage gaps. Returns RA bands you haven't visited yet plus suggested coordinates. Use this to pick truly new regions instead of revisiting old ones.",
        "usage": "list_unexplored()",
        "script": "__internal__",
    },
    "my_stats": {
        "description": "See your own global performance dashboard: total findings by significance, regions explored vs exhausted, sky coverage, tool usage breakdown, top discoveries, and strategic recommendations. Call this periodically to reflect on your progress and adjust your strategy!",
        "usage": "my_stats()",
        "script": "__internal__",
    },
    # --- Memory WRITE tools: Qwen manages its own knowledge base ---
    "dismiss_lead": {
        "description": "Mark a priority lead as RESOLVED/INVESTIGATED so you stop going back to it. Use this when you've thoroughly analyzed a region and there's nothing left to do. The lead is removed from your priority list.",
        "usage": "dismiss_lead(ra=<degrees>, dec=<degrees>, reason='<why you are closing this lead>')",
        "script": "__internal__",
    },
    "add_note": {
        "description": "Write a note to your future self about a region. Use this to record conclusions, hypotheses, or plans. Notes persist across runs and appear when you query_memory for that region.",
        "usage": "add_note(ra=<degrees>, dec=<degrees>, note='<your note>')",
        "script": "__internal__",
    },
    "mark_exhausted": {
        "description": "Flag a region as EXHAUSTED — all available analysis has been done, move on. Exhausted regions are deprioritized in memory summaries so you focus on fresh targets.",
        "usage": "mark_exhausted(ra=<degrees>, dec=<degrees>, reason='<summary of what was done>')",
        "script": "__internal__",
    },
    # --- Validation tools: verify candidates before logging ---
    "query_gaia": {
        "description": "Query Gaia DR3 catalog for sources at a position. Returns parallax (distance), proper motion, magnitude, and variability classification. Use to check if a transient candidate is a known variable star or high-proper-motion object.",
        "usage": "query_gaia(ra=<degrees>, dec=<degrees>, radius=5)",
        "script": "tools/astro_query.py query-gaia --ra {ra} --dec {dec} --radius {radius}",
    },
    "check_transients": {
        "description": "Check ALeRCE/ZTF broker for known transients at a position. Returns ML classification (SN, AGN, variable star, etc.) and observation history. Use BEFORE logging a finding to check if it's already known!",
        "usage": "check_transients(ra=<degrees>, dec=<degrees>, radius=5)",
        "script": "tools/astro_query.py check-transients --ra {ra} --dec {dec} --radius {radius}",
    },
    "measure_photometry": {
        "description": "Measure calibrated aperture photometry of a source in a FITS image. Returns magnitude, flux, SNR. IMPORTANT: The RA/Dec must be INSIDE the image — use an image centered on that position (check the RA/Dec in the filename matches your target). If you get 'outside image' errors, download_cutout at the target coordinates first.",
        "usage": "measure_photometry(image='data/images/<file>.fits', ra=<degrees>, dec=<degrees>)",
        "script": "tools/image_analysis.py measure-photometry --image {image} --ra {ra} --dec {dec} --aperture {aperture} --inner {inner} --outer {outer}",
    },
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Terminal colors & styling
# ---------------------------------------------------------------------------

class C:
    """ANSI color codes for rich terminal output."""
    RESET       = "\033[0m"
    BOLD        = "\033[1m"
    DIM         = "\033[2m"
    ITALIC      = "\033[3m"
    UNDERLINE   = "\033[4m"

    # Base log levels
    INFO        = "\033[94m"        # blue
    OK          = "\033[92m"        # green
    WARN        = "\033[93m"        # yellow
    ERROR       = "\033[91m"        # red
    FIND        = "\033[95m"        # magenta

    # Per-tool colors — each tool gets a unique hue
    SEARCH      = "\033[38;5;75m"   # steel blue      — MAST queries
    SIMBAD      = "\033[38;5;214m"  # orange           — catalog checks
    ZTF         = "\033[38;5;204m"  # pink/salmon      — light curves
    DOWNLOAD    = "\033[38;5;114m"  # soft green       — cutout downloads
    DETECT      = "\033[38;5;51m"   # cyan             — source detection
    COMPARE     = "\033[38;5;177m"  # lavender         — image comparison
    ANALYZE     = "\033[38;5;220m"  # gold             — vision analysis
    CONVERT     = "\033[38;5;145m"  # grey-blue        — PNG conversion
    LIST_IMG    = "\033[38;5;109m"  # teal             — list images
    MEMORY      = "\033[38;5;123m"  # aqua             — memory tools
    LOG_FIND    = "\033[38;5;205m"  # hot pink         — log_finding

    # Thought styling
    THOUGHT     = "\033[38;5;183m"  # light purple     — Qwen's reasoning
    THOUGHT_BG  = "\033[48;5;236m"  # dark grey bg

    # Banners & separators
    BANNER      = "\033[38;5;39m"   # bright blue
    CYCLE_HDR   = "\033[38;5;45m"   # sky blue
    SEPARATOR   = "\033[38;5;240m"  # dark grey


# Map tool names → (color, emoji)
TOOL_STYLE = {
    "search_region":   (C.SEARCH,   "🔭"),
    "search_target":   (C.SEARCH,   "🔭"),
    "multi_epoch":     (C.SEARCH,   "📅"),
    "simbad_check":    (C.SIMBAD,   "📖"),
    "ztf_lightcurve":  (C.ZTF,      "📈"),
    "download_cutout":     (C.DOWNLOAD, "📥"),
    "download_multiepoch": (C.DOWNLOAD, "📅"),
    "download_legacy":     (C.DOWNLOAD, "🌐"),
    "detect_sources":  (C.DETECT,   "🔍"),
    "compare_images":  (C.COMPARE,  "🔀"),
    "analyze_image":   (C.ANALYZE,  "👁️"),
    "convert_to_png":  (C.CONVERT,  "🖼️"),
    "list_images":     (C.LIST_IMG, "📂"),
    "log_finding":     (C.LOG_FIND, "⭐"),
    "query_memory":    (C.MEMORY,   "🧠"),
    "list_findings":   (C.MEMORY,   "📋"),
    "list_unexplored": (C.MEMORY,   "🗺️"),
    "query_gaia":      (C.SIMBAD,   "🌟"),
    "check_transients":(C.ZTF,      "🚨"),
    "measure_photometry":(C.DETECT, "📐"),
}

def _tool_color(tool_name):
    """Get (color, emoji) for a tool, with fallback."""
    return TOOL_STYLE.get(tool_name, ("\033[96m", "🔧"))


def log(level, message, **extra):
    """Write a log entry to file and console with rich colors."""
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    ts_str = f"{C.DIM}{timestamp}{C.RESET}"

    level_colors = {
        "INFO":  C.INFO,
        "OK":    C.OK,
        "WARN":  C.WARN,
        "ERROR": C.ERROR,
        "TOOL":  "\033[96m",
        "RESULT": "\033[38;5;141m",   # soft purple for tool results
        "FIND":  C.FIND,
        "THINK": C.THOUGHT,
    }
    lc = level_colors.get(level, "")

    # Special rendering for thoughts
    if level == "THINK":
        prefix = f"{C.THOUGHT}{C.ITALIC}💭 THOUGHT{C.RESET}"
        console_msg = f"  {ts_str} {prefix} {C.THOUGHT_BG}{C.THOUGHT}{C.ITALIC} {message} {C.RESET}"
    # Special rendering for tool calls
    elif level == "TOOL" and "|" in message:
        # Format: "tool_name|rest of message"
        parts = message.split("|", 1)
        tool_name = parts[0].strip()
        rest = parts[1].strip() if len(parts) > 1 else ""
        color, emoji = _tool_color(tool_name)
        prefix = f"{color}{C.BOLD}{emoji} {tool_name}{C.RESET}"
        console_msg = f"  {ts_str} {prefix} {C.DIM}{rest}{C.RESET}"
    elif level == "TOOL":
        console_msg = f"  {ts_str} {lc}{C.BOLD}🔧 {level}{C.RESET} {message}"
    # Special rendering for tool results — show tool name colored + dimmed result
    elif level == "RESULT" and "|" in message:
        parts = message.split("|", 1)
        tool_name = parts[0].strip()
        rest = parts[1].strip() if len(parts) > 1 else ""
        color, emoji = _tool_color(tool_name)
        prefix = f"{color}{emoji} {tool_name}{C.RESET}"
        console_msg = f"  {ts_str} {prefix} {C.DIM}→ {rest}{C.RESET}"
    elif level == "OK":
        console_msg = f"  {ts_str} {C.OK}{C.BOLD}✅ {level}{C.RESET} {C.OK}{message}{C.RESET}"
    elif level == "WARN":
        console_msg = f"  {ts_str} {C.WARN}{C.BOLD}⚠️  {level}{C.RESET} {C.WARN}{message}{C.RESET}"
    elif level == "ERROR":
        console_msg = f"  {ts_str} {C.ERROR}{C.BOLD}❌ {level}{C.RESET} {C.ERROR}{message}{C.RESET}"
    elif level == "FIND":
        console_msg = f"  {ts_str} {C.FIND}{C.BOLD}🌟 DISCOVERY{C.RESET} {C.FIND}{message}{C.RESET}"
    else:
        console_msg = f"  {ts_str} {lc}{level}{C.RESET} {message}"

    if extra:
        extra_str = ", ".join(f"{C.DIM}{k}={v}{C.RESET}" for k, v in extra.items())
        console_msg += f" {extra_str}"
    print(console_msg)

    # File log (plain text, no ANSI)
    full_ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(RESEARCH_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{full_ts}] [{level}] {message}")
            if extra:
                f.write(f" | {json.dumps(extra, default=str)}")
            f.write("\n")
    except Exception:
        pass


def print_banner(model, memory, target=None):
    """Print a colorful startup banner."""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n_regions = len(memory.get("regions", {}))
    n_runs = memory.get("run_count", 0)
    total_cycles = memory.get("total_cycles_all_runs", 0)

    print(f"""
{C.BANNER}{C.BOLD}╔══════════════════════════════════════════════════════════╗
║  🌌  AstroResearch — Autonomous Anomaly Detection  🌌   ║
╠══════════════════════════════════════════════════════════╣{C.RESET}
{C.BANNER}║{C.RESET}  Model     : {C.BOLD}{model}{C.RESET}
{C.BANNER}║{C.RESET}  Started   : {now}
{C.BANNER}║{C.RESET}  Memory    : {C.BOLD}{n_regions}{C.RESET} regions │ {C.BOLD}{total_cycles}{C.RESET} cycles │ {C.BOLD}{n_runs}{C.RESET} run(s)""")
    if target:
        print(f"{C.BANNER}║{C.RESET}  Target    : {C.BOLD}{C.OK}{target}{C.RESET}")
    print(f"""{C.BANNER}{C.BOLD}╚══════════════════════════════════════════════════════════╝{C.RESET}
""")


def print_cycle_header(cycle_num):
    """Print a colorful cycle separator."""
    now = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"""
{C.SEPARATOR}{'─' * 60}{C.RESET}
  {C.CYCLE_HDR}{C.BOLD}🔬 Research Cycle {cycle_num}{C.RESET}  {C.DIM}@ {now}{C.RESET}
{C.SEPARATOR}{'─' * 60}{C.RESET}""")


def print_cycle_summary(cycle_num, summary_parts):
    """Print a colorful end-of-cycle summary."""
    if not summary_parts:
        return
    print(f"\n  {C.DIM}{'─' * 50}{C.RESET}")
    print(f"  {C.OK}{C.BOLD}📊 Cycle {cycle_num} Summary:{C.RESET}")
    for part in summary_parts:
        # Try to extract tool name from the summary part
        tool_name = part.split(":")[0].strip() if ":" in part else ""
        # Map common summary prefixes to tool names
        tool_map = {
            "search_region": "search_region", "search_target": "search_target",
            "multi_epoch": "multi_epoch", "SIMBAD": "simbad_check",
            "ZTF": "ztf_lightcurve", "Finding logged": "log_finding",
            "download_cutout": "download_cutout", "download_multiepoch": "download_multiepoch",
            "download_legacy": "download_legacy", "detect_sources": "detect_sources",
            "compare_images": "compare_images", "analyze_image": "analyze_image",
            "list_images": "list_images", "query_memory": "query_memory",
            "list_findings": "list_findings", "list_unexplored": "list_unexplored",
            "convert_to_png": "convert_to_png",
            "query_gaia": "query_gaia", "check_transients": "check_transients",
            "measure_photometry": "measure_photometry",
        }
        matched_tool = None
        for prefix, tname in tool_map.items():
            if part.startswith(prefix):
                matched_tool = tname
                break

        if "ERROR" in part:
            print(f"    ❌ {C.ERROR}{part}{C.RESET}")
        elif "COOLDOWN" in part:
            print(f"    ⏳ {C.WARN}{part}{C.RESET}")
        elif "BLOCKED" in part:
            print(f"    🚫 {C.ERROR}{part}{C.RESET}")
        elif matched_tool:
            color, emoji = _tool_color(matched_tool)
            print(f"    {emoji} {color}{part}{C.RESET}")
        else:
            print(f"    • {C.DIM}{part}{C.RESET}")
    print(f"  {C.DIM}{'─' * 50}{C.RESET}")

# ---------------------------------------------------------------------------
# Ollama API
# ---------------------------------------------------------------------------

def call_ollama(model_name, system_prompt, user_prompt, temperature=0.3, images=None):
    """Call Ollama with think:false for direct structured output.

    If images is provided (list of base64 strings), they are included in
    the user message so Qwen can see them inline with full research context.
    """
    import requests

    user_message = {"role": "user", "content": user_prompt}
    if images:
        user_message["images"] = images
        log("INFO", f"Including {len(images)} image(s) in prompt for inline vision")

    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            user_message,
        ],
        "stream": False,
        "think": False,
        "options": {
            "temperature": temperature,
            "num_predict": 4000,
            "num_ctx": 16000,
        },
    }
    
    try:
        t0 = time.time()
        resp = requests.post(OLLAMA_URL, json=payload, timeout=300)
        resp.raise_for_status()
        elapsed = time.time() - t0
        
        data = resp.json()
        content = data.get("message", {}).get("content", "").strip()
        
        if not content:
            log("WARN", f"Empty response from Qwen ({elapsed:.1f}s)")
            return None, elapsed
        
        log("INFO", f"Qwen responded in {elapsed:.1f}s ({len(content)} chars)")
        return content, elapsed
    
    except Exception as e:
        log("ERROR", f"Ollama API error: {e}")
        return None, 0


def recovery_reprompt(model_name, thought_summary, temperature=0.5):
    """When Qwen loops on THOUGHT blocks without emitting tools, re-prompt
    with the extracted thought and demand ONLY tool calls.

    Uses a small num_predict (500) so it can't spiral again.
    Returns parsed tool_calls list, or empty list on failure.
    """
    # Try to extract coordinates from the thought text
    ra_match = re.search(r'RA[=:\s]*([0-9]+\.?[0-9]*)', thought_summary)
    dec_match = re.search(r'Dec[=:\s]*([+-]?[0-9]+\.?[0-9]*)', thought_summary)

    coord_hint = ""
    if ra_match and dec_match:
        ra_val = ra_match.group(1)
        dec_val = dec_match.group(1)
        coord_hint = f"\nCoordinates from your analysis: ra={ra_val}, dec={dec_val}"

    recovery_prompt = f"""You just analyzed data and found this:
{thought_summary[:800]}
{coord_hint}

NOW respond with ONLY TOOL: lines. No THOUGHT blocks. No explanations.
Pick the most logical next steps based on your finding above.

Examples:
TOOL: simbad_check(ra=180.0, dec=45.0, radius=60)
TOOL: multi_epoch(ra=180.0, dec=45.0, radius=0.02)
TOOL: log_finding(ra=180.0, dec=45.0, description='Color anomaly: bright in r/i, faint in g', significance='medium')
"""

    system_prompt = "You are an astronomy research agent. Respond with ONLY TOOL: lines. No other text."

    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": recovery_prompt},
        ],
        "stream": False,
        "think": False,
        "options": {
            "temperature": temperature,
            "num_predict": 500,
            "num_ctx": 4000,
        },
    }

    try:
        log("INFO", "Recovery re-prompt: demanding TOOL: lines from Qwen...")
        t0 = time.time()
        resp = requests.post(OLLAMA_URL, json=payload, timeout=60)
        resp.raise_for_status()
        elapsed = time.time() - t0

        content = resp.json().get("message", {}).get("content", "").strip()
        if not content:
            log("WARN", f"Recovery re-prompt returned empty ({elapsed:.1f}s)")
            return []

        log("INFO", f"Recovery response in {elapsed:.1f}s ({len(content)} chars)")
        parsed = parse_tool_calls(content)
        return parsed["tool_calls"]

    except Exception as e:
        log("ERROR", f"Recovery re-prompt failed: {e}")
        return []


def autolog_from_thought(thought_summary):
    """Last resort: extract coordinates from a lost thought and auto-log
    the finding so it isn't silently dropped."""
    ra_match = re.search(r'RA[=:\s]*([0-9]+\.?[0-9]*)', thought_summary)
    dec_match = re.search(r'Dec[=:\s]*([+-]?[0-9]+\.?[0-9]*)', thought_summary)

    if not ra_match or not dec_match:
        return None

    ra = float(ra_match.group(1))
    dec = float(dec_match.group(1))

    desc = thought_summary[:300].strip()
    desc = f"[AUTO-RECOVERED] {desc}"

    log("WARN", f"Auto-logging lost finding from repetition loop at RA={ra}, Dec={dec}")
    return log_finding(ra=ra, dec=dec, description=desc, significance="medium")


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

TOOL_TIMEOUTS = {
    "analyze_image": 90,      # Ollama vision needs time (was 30s → timeout)
    "compare_images": 45,     # Image comparison can be slow on large cutouts
    "download_cutout": 60,    # Network downloads can be slow
    "download_multiepoch": 120, # Multiple sequential warp downloads (3-6 HTTP requests)
    "download_legacy": 45,      # Single network download from Legacy Survey
    "ztf_lightcurve": 30,     # ZTF API (already has internal 20s timeout)
    "search_region": 30,      # MAST API
    "search_target": 30,      # MAST API
    "multi_epoch": 30,        # MAST API
    "simbad_check": 20,       # SIMBAD TAP
    "detect_sources": 30,     # CPU-bound, usually fast
    "list_images": 10,        # Just listing files
    "convert_to_png": 20,     # FITS conversion
    "log_finding": 5,         # Internal, instant
    "query_gaia": 20,         # Single Gaia TAP query
    "check_transients": 30,   # ALeRCE API (up to 2 HTTP requests)
    "measure_photometry": 15, # CPU-only aperture photometry
}
DEFAULT_TOOL_TIMEOUT = 30

def prepare_image_for_vision(image_path):
    """Convert an image (FITS, JPEG, PNG) to base64 for inline vision.

    Handles FITS→PNG conversion with percentile stretching.
    Returns (b64_string, png_path) or (None, error_msg).
    """
    from PIL import Image
    import numpy as np

    image_path = image_path.replace("\\\\", "/").replace("\\", "/")

    # Resolve relative paths
    if not os.path.isabs(image_path):
        image_path = os.path.join(BASE_DIR, image_path)

    if not os.path.exists(image_path):
        return None, f"File not found: {image_path}"

    ext = os.path.splitext(image_path)[1].lower()
    png_dir = os.path.join(DATA_DIR, "png")
    os.makedirs(png_dir, exist_ok=True)

    if ext in (".jpg", ".jpeg", ".png"):
        png_path = image_path
    elif ext in (".fits", ".fit", ".fz"):
        # Convert FITS to PNG with percentile stretching
        try:
            from astropy.io import fits
            with fits.open(image_path) as hdul:
                data = hdul[0].data
                if data is None and len(hdul) > 1:
                    data = hdul[1].data
            if data is None:
                return None, "FITS file has no image data"
            data = data.astype(float)
            vmin = np.percentile(data, 1)
            vmax = np.percentile(data, 99)
            if vmax <= vmin:
                vmax = vmin + 1
            stretched = np.clip((data - vmin) / (vmax - vmin) * 255, 0, 255).astype(np.uint8)
            stretched = np.flipud(stretched)
            img = Image.fromarray(stretched, mode="L")
            basename = os.path.splitext(os.path.basename(image_path))[0]
            png_path = os.path.join(png_dir, f"{basename}.png")
            img.save(png_path)
        except Exception as e:
            return None, f"FITS conversion failed: {e}"
    else:
        return None, f"Unsupported image format: {ext}"

    # Encode to base64
    try:
        with open(png_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
        return b64, png_path
    except Exception as e:
        return None, f"Base64 encoding failed: {e}"


def _prepare_vision_request(image="", prompt=None, **kwargs):
    """Prepare an image for inline vision analysis.

    Instead of spawning a separate Ollama call, this converts the image
    to base64 and returns a special result dict. The main loop detects
    this and includes the image in the NEXT call_ollama prompt, so Qwen
    sees it with full research context.
    """
    b64, result_or_path = prepare_image_for_vision(image)
    if b64 is None:
        return {"error": f"analyze_image failed: {result_or_path}"}

    vision_prompt = prompt or (
        "Analyze this sky image cutout. Describe: 1) Point sources (stars) count and brightness, "
        "2) Extended objects (galaxies, nebulae), 3) Artifacts or unusual features, "
        "4) Any sources that look anomalous or potentially transient."
    )

    return {
        "__vision__": True,
        "image_b64": b64,
        "image_path": image,
        "png_path": result_or_path,
        "prompt": vision_prompt,
        "status": "Image queued for YOUR visual analysis in the next cycle. "
                  "You will see it directly — analyze it with full context of your research.",
    }


# ---------------------------------------------------------------------------
# Memory query tools — let the LLM look up its own past data
# ---------------------------------------------------------------------------

def _query_memory(memory, ra=0, dec=0, radius=5.0, **kwargs):
    """Look up past exploration data for a region.

    Searches memory for all regions within `radius` degrees of (ra, dec).
    Returns the full history: tools used, outcomes, findings, visit count.
    """
    try:
        ra, dec, radius = float(ra), float(dec), float(radius)
    except (ValueError, TypeError):
        return {"error": "ra, dec, and radius must be numbers"}

    matches = []
    for key, reg in memory.get("regions", {}).items():
        try:
            d_ra = abs(reg["ra"] - ra)
            if d_ra > 180:
                d_ra = 360 - d_ra  # RA wrap-around
            d_dec = abs(reg["dec"] - dec)
            dist = (d_ra**2 + d_dec**2) ** 0.5
            if dist <= radius:
                entry = {
                    "ra": reg["ra"],
                    "dec": reg["dec"],
                    "distance_deg": round(dist, 2),
                    "visits": reg["visits"],
                    "tools_used": reg["tools_used"],
                    "outcomes": reg["outcomes"],
                    "findings": reg["findings"],
                    "first_cycle": reg.get("first_cycle"),
                    "last_cycle": reg.get("last_cycle"),
                }
                # Include notes and exhausted status if present
                if reg.get("notes"):
                    entry["notes"] = [n["text"] for n in reg["notes"][-5:]]  # Last 5 notes
                if reg.get("exhausted"):
                    entry["exhausted"] = True
                matches.append(entry)
        except (KeyError, TypeError):
            continue

    # Sort by distance
    matches.sort(key=lambda m: m["distance_deg"])

    # Also find relevant failures
    relevant_failures = []
    for fail_key, fail_info in memory.get("known_failures", {}).items():
        parts = fail_key.split("|")
        if len(parts) == 2:
            try:
                fcoords = parts[1].split(",")
                f_ra, f_dec = float(fcoords[0]), float(fcoords[1])
                d_ra = abs(f_ra - ra)
                if d_ra > 180:
                    d_ra = 360 - d_ra
                d_dec = abs(f_dec - dec)
                dist = (d_ra**2 + d_dec**2) ** 0.5
                if dist <= radius:
                    relevant_failures.append({
                        "tool": parts[0],
                        "error": fail_info["error"],
                        "count": fail_info["count"],
                        "last_cycle": fail_info["last_cycle"],
                    })
            except (ValueError, IndexError):
                continue

    return {
        "query": {"ra": ra, "dec": dec, "radius_deg": radius},
        "n_regions_found": len(matches),
        "regions": matches[:10],  # Cap at 10 to save tokens
        "relevant_failures": relevant_failures[:5],
        "tip": "No past data — this is a fresh region!" if not matches else None,
    }


def _list_findings(memory, significance="all", **kwargs):
    """List all logged findings from memory, optionally filtered by significance.

    Reads from best_leads and region findings in memory.
    """
    sig_filter = str(significance).lower().strip()

    # Gather all findings from regions
    all_findings = []
    for key, reg in memory.get("regions", {}).items():
        for fid in reg.get("findings", []):
            all_findings.append({
                "finding_id": fid,
                "ra": reg["ra"],
                "dec": reg["dec"],
                "region_visits": reg["visits"],
            })

    # Best leads (high-significance)
    best_leads = memory.get("best_leads", [])

    # Also try to read the findings.tsv for full details
    findings_detail = []
    if os.path.exists(FINDINGS_FILE):
        try:
            with open(FINDINGS_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > 1:
                headers = lines[0].strip().split("\t")
                for line in lines[1:]:
                    cols = line.strip().split("\t")
                    if len(cols) >= len(headers):
                        row = dict(zip(headers, cols))
                        # Apply filter
                        if sig_filter != "all" and row.get("significance", "").lower() != sig_filter:
                            continue
                        findings_detail.append({
                            "id": row.get("id", "?"),
                            "ra": row.get("ra", "?"),
                            "dec": row.get("dec", "?"),
                            "significance": row.get("significance", "?"),
                            "description": row.get("description", "")[:120],
                            "timestamp": row.get("timestamp", "?"),
                        })
        except Exception:
            pass

    return {
        "total_findings": len(findings_detail) if findings_detail else len(all_findings),
        "filter": sig_filter,
        "findings": findings_detail[:15] if findings_detail else all_findings[:15],
        "best_leads": best_leads[-10:],
        "tip": "Use query_memory(ra, dec) to get detailed history for a specific finding's region.",
    }


def _list_unexplored(memory, **kwargs):
    """Show unexplored sky regions and coverage gaps with suggested coordinates.

    Uses adaptive bin resolution: starts at 10° RA bins, switches to 2° when
    all coarse bins are covered. Always generates fresh random coordinates
    far from existing regions — the sky has ~41,253 sq degrees but each
    observation covers ~0.0003 sq deg, so there's always something new.
    """
    import math

    cov = memory.get("sky_coverage", {})
    ra_bins_coarse = set(cov.get("ra_bins_visited", []))
    regions = memory.get("regions", {})

    # Collect all explored coordinates for distance checks
    explored_coords = []
    for key, reg in regions.items():
        explored_coords.append((float(reg.get("ra", 0)), float(reg.get("dec", 0))))

    # --- Adaptive bin resolution ---
    # Start with 10° bins; if all covered, switch to 2° bins
    all_coarse = set(range(0, 360, 10))
    coarse_missing = sorted(all_coarse - ra_bins_coarse)

    if coarse_missing:
        # Still have 10° gaps — use coarse bins
        bin_size = 10
        missing_bins = coarse_missing
        coverage_label = f"{len(ra_bins_coarse)}/36 (10° bins)"
    else:
        # All 10° bins covered — switch to finer 2° bins
        bin_size = 2
        fine_bins_visited = set()
        for ra_val, _ in explored_coords:
            fine_bins_visited.add(int(ra_val / bin_size) * bin_size)
        all_fine = set(range(0, 360, bin_size))
        missing_bins = sorted(all_fine - fine_bins_visited)
        coverage_label = f"{len(fine_bins_visited)}/{len(all_fine)} (2° bins)"

    # Group consecutive missing bins into gap ranges
    gap_ranges = []
    i = 0
    while i < len(missing_bins):
        start = missing_bins[i]
        end = start
        while i + 1 < len(missing_bins) and missing_bins[i + 1] == end + bin_size:
            i += 1
            end = missing_bins[i]
        center_ra = (start + end + bin_size) / 2.0
        gap_ranges.append({
            "ra_range": f"{start}°–{end + bin_size}°",
            "suggested_ra": round(center_ra, 1),
            "width_deg": end + bin_size - start,
        })
        i += 1

    # --- Declination suggestions ---
    dec_min = cov.get("dec_min", 90.0)
    dec_max = cov.get("dec_max", -90.0)
    dec_suggestions = []
    if dec_min > -30:
        dec_suggestions.append({"dec": -45.0, "reason": "Southern sky barely explored"})
    if dec_max < 60:
        dec_suggestions.append({"dec": 70.0, "reason": "High northern declinations unexplored"})
    if dec_min > 0:
        dec_suggestions.append({"dec": -15.0, "reason": "Negative declinations not yet visited"})

    # --- Generate suggested coordinates ---
    # Strategy: combine gap-based suggestions with random "far from anything" coords
    gap_ranges.sort(key=lambda g: g["width_deg"], reverse=True)
    suggested_coords = []

    # 1) Gap-based suggestions (from RA gaps)
    for gap in gap_ranges[:3]:
        suggested_dec = round(random.uniform(-30, 60), 1)
        suggested_coords.append({
            "ra": gap["suggested_ra"],
            "dec": suggested_dec,
            "reason": f"Center of unexplored RA gap {gap['ra_range']}",
        })

    # 2) Random coordinates far from any explored region
    # This ensures Qwen ALWAYS gets fresh targets even at "100% coverage"
    def _min_distance(ra, dec):
        """Min angular distance (degrees) to any explored region."""
        if not explored_coords:
            return 999
        best = 999
        cos_dec = math.cos(math.radians(dec))
        for era, edec in explored_coords:
            dra = abs(ra - era) * cos_dec
            if dra > 180 * cos_dec:
                dra = 360 * cos_dec - dra
            ddec = abs(dec - edec)
            dist = math.sqrt(dra ** 2 + ddec ** 2)
            if dist < best:
                best = dist
        return best

    # Generate candidates and keep the ones farthest from anything explored
    candidates = []
    for _ in range(200):
        rand_ra = round(random.uniform(0, 360), 4)
        # Favor Dec -30 to +70 for best survey coverage
        rand_dec = round(random.uniform(-30, 70), 4)
        dist = _min_distance(rand_ra, rand_dec)
        candidates.append((rand_ra, rand_dec, dist))

    # Sort by distance (farthest first) and pick top N
    candidates.sort(key=lambda c: c[2], reverse=True)
    n_random = max(2, 5 - len(suggested_coords))
    for rand_ra, rand_dec, dist in candidates[:n_random]:
        suggested_coords.append({
            "ra": rand_ra,
            "dec": rand_dec,
            "reason": f"Random unexplored position ({dist:.1f}° from nearest explored region)",
        })

    n_explored = len(regions)
    sky_area_explored = n_explored * 0.0003  # ~1 arcmin² per region ≈ 0.0003 sq deg
    sky_fraction = sky_area_explored / 41253 * 100

    return {
        "ra_bins_explored": coverage_label,
        "dec_range_explored": f"{dec_min:.0f}° to {dec_max:.0f}°",
        "total_regions_explored": n_explored,
        "actual_sky_fraction": f"{sky_fraction:.4f}% (the sky is VAST — always room for new discoveries!)",
        "unexplored_ra_gaps": gap_ranges[:8],
        "dec_suggestions": dec_suggestions,
        "suggested_coordinates": suggested_coords,
        "tip": "The sky has 41,253 square degrees — you've barely scratched the surface! Pick any suggested coordinate and explore it.",
    }


def _my_stats(memory, **kwargs):
    """Strategic self-awareness dashboard for the agent.

    Returns a high-level overview of the agent's performance: findings
    breakdown, region stats, tool usage, coverage, and recommendations.
    """
    regions = memory.get("regions", {})
    n_regions = len(regions)
    n_exhausted = sum(1 for r in regions.values() if r.get("exhausted"))
    n_active = n_regions - n_exhausted
    n_with_findings = sum(1 for r in regions.values() if r.get("findings"))
    n_with_notes = sum(1 for r in regions.values() if r.get("notes"))
    total_cycles = memory.get("total_cycles_all_runs", 0)
    total_visits = sum(r.get("visits", 0) for r in regions.values())

    # --- Findings breakdown ---
    findings_by_sig = {"high": 0, "medium": 0, "low": 0}
    if os.path.exists(FINDINGS_FILE):
        try:
            with open(FINDINGS_FILE, "r", encoding="utf-8") as f:
                for line in f.readlines()[1:]:
                    cols = line.strip().split("\t")
                    if len(cols) >= 5:
                        sig = cols[4].lower()
                        findings_by_sig[sig] = findings_by_sig.get(sig, 0) + 1
        except Exception:
            pass
    total_findings = sum(findings_by_sig.values())

    # --- Tool usage breakdown ---
    tool_counts = {}
    for reg in regions.values():
        for tool in reg.get("tools_used", []):
            tool_counts[tool] = tool_counts.get(tool, 0) + 1
    # Sort by usage
    top_tools = sorted(tool_counts.items(), key=lambda x: x[1], reverse=True)[:8]

    # --- Visit distribution ---
    visit_buckets = {"1 visit": 0, "2-5 visits": 0, "6-15 visits": 0, "16+ visits": 0}
    for reg in regions.values():
        v = reg.get("visits", 0)
        if v <= 1:
            visit_buckets["1 visit"] += 1
        elif v <= 5:
            visit_buckets["2-5 visits"] += 1
        elif v <= 15:
            visit_buckets["6-15 visits"] += 1
        else:
            visit_buckets["16+ visits"] += 1

    # --- Coverage ---
    cov = memory.get("sky_coverage", {})
    ra_bins = len(cov.get("ra_bins_visited", []))
    dec_min = cov.get("dec_min", 90)
    dec_max = cov.get("dec_max", -90)
    sampled_area = round(n_regions * 0.008, 2)  # ~3 arcmin field per region

    # --- Finding rate ---
    finding_rate = round(total_findings / max(total_cycles, 1) * 100, 1)

    # --- Strategic recommendations ---
    recommendations = []
    if n_active > 0 and n_with_findings / max(n_regions, 1) < 0.3:
        recommendations.append("Low finding rate — try denser fields (galaxy clusters, galactic plane)")
    if findings_by_sig.get("high", 0) < 5:
        recommendations.append("Few high-significance finds — look for sources absent in one band, or with ZTF variability")
    if visit_buckets.get("16+ visits", 0) > 10:
        recommendations.append("Many heavily-visited regions — make sure you're completing investigations and marking them exhausted when truly done")
    shallow = visit_buckets.get("1 visit", 0)
    if shallow > n_regions * 0.4:
        recommendations.append(f"{shallow} regions visited only once — revisit promising ones for deeper analysis")
    if ra_bins >= 36 and n_regions < 100:
        recommendations.append("Good RA spread but sparse — fill in with more regions per RA band")
    if dec_min > -30:
        recommendations.append("Southern sky (Dec < -30) unexplored — expand Dec range")
    if not recommendations:
        recommendations.append("Looking good! Keep exploring fresh regions and logging noteworthy findings.")

    return {
        "total_cycles": total_cycles,
        "total_visits": total_visits,
        "regions": {
            "total": n_regions,
            "active": n_active,
            "exhausted": n_exhausted,
            "with_findings": n_with_findings,
            "with_notes": n_with_notes,
        },
        "findings": {
            "total": total_findings,
            "high": findings_by_sig.get("high", 0),
            "medium": findings_by_sig.get("medium", 0),
            "low": findings_by_sig.get("low", 0),
            "finding_rate": f"{finding_rate}% of cycles produce a finding",
        },
        "tool_usage": {t: c for t, c in top_tools},
        "visit_distribution": visit_buckets,
        "coverage": {
            "ra_spread": f"{ra_bins}/36 RA bins",
            "dec_range": f"{dec_min:.0f}° to {dec_max:.0f}°",
            "sampled_area_sqdeg": sampled_area,
        },
        "best_leads_count": len(memory.get("best_leads", [])),
        "recommendations": recommendations,
    }


# ---------------------------------------------------------------------------
# Memory WRITE tools — Qwen manages its own knowledge base
# ---------------------------------------------------------------------------

def _dismiss_lead(memory, ra=0, dec=0, reason="", **kwargs):
    """Remove a lead from best_leads so Qwen stops revisiting it.

    Also records the dismissal as a note on the region for future reference.
    """
    try:
        ra, dec = float(ra), float(dec)
    except (ValueError, TypeError):
        return {"error": "ra and dec must be numbers"}

    if not reason:
        return {"error": "Please provide a reason for dismissing this lead."}

    # Remove matching leads (within 1.5 degrees)
    before = len(memory.get("best_leads", []))
    memory["best_leads"] = [
        l for l in memory.get("best_leads", [])
        if not (abs(l["ra"] - ra) < 1.5 and abs(l["dec"] - dec) < 1.5)
    ]
    removed = before - len(memory["best_leads"])

    # Record the dismissal as a note on the region
    region_key = f"{round(ra, 1)},{round(dec, 1)}"
    if region_key in memory.get("regions", {}):
        reg = memory["regions"][region_key]
        if "notes" not in reg:
            reg["notes"] = []
        reg["notes"].append({
            "text": f"[DISMISSED] {reason}",
            "cycle": memory.get("total_cycles_all_runs", 0),
            "timestamp": datetime.datetime.now().isoformat(),
        })

    return {
        "status": "ok",
        "leads_removed": removed,
        "reason": reason,
        "message": f"Dismissed {removed} lead(s) near RA={ra}, Dec={dec}. "
                   f"This region will no longer appear in your priority list.",
    }


def _add_note(memory, ra=0, dec=0, note="", **kwargs):
    """Write a persistent note on a region. Notes survive across runs.

    If the region doesn't exist in memory yet, it is created with a
    minimal entry so the note has somewhere to live.
    """
    try:
        ra, dec = float(ra), float(dec)
    except (ValueError, TypeError):
        return {"error": "ra and dec must be numbers"}

    if not note:
        return {"error": "Please provide a note to record."}

    region_key = f"{round(ra, 1)},{round(dec, 1)}"

    # Create region entry if it doesn't exist
    if region_key not in memory.get("regions", {}):
        if "regions" not in memory:
            memory["regions"] = {}
        memory["regions"][region_key] = {
            "ra": round(ra, 2),
            "dec": round(dec, 2),
            "visits": 0,
            "tools_used": [],
            "outcomes": [],
            "findings": [],
            "notes": [],
            "exhausted": False,
            "first_cycle": memory.get("total_cycles_all_runs", 0),
            "last_cycle": memory.get("total_cycles_all_runs", 0),
        }

    reg = memory["regions"][region_key]
    if "notes" not in reg:
        reg["notes"] = []

    reg["notes"].append({
        "text": note,
        "cycle": memory.get("total_cycles_all_runs", 0),
        "timestamp": datetime.datetime.now().isoformat(),
    })

    # Cap at 20 notes per region to prevent bloat
    if len(reg["notes"]) > 20:
        reg["notes"] = reg["notes"][-20:]

    return {
        "status": "ok",
        "region": region_key,
        "total_notes": len(reg["notes"]),
        "message": f"Note recorded for RA={ra}, Dec={dec}. "
                   f"It will appear when you query_memory for this region.",
    }


def _mark_exhausted(memory, ra=0, dec=0, reason="", **kwargs):
    """Flag a region as exhausted — fully investigated, move on.

    Exhausted regions are deprioritized in memory summaries and
    their leads are automatically dismissed.
    """
    try:
        ra, dec = float(ra), float(dec)
    except (ValueError, TypeError):
        return {"error": "ra and dec must be numbers"}

    if not reason:
        return {"error": "Please provide a summary of what was done in this region."}

    region_key = f"{round(ra, 1)},{round(dec, 1)}"

    # Create region entry if it doesn't exist
    if region_key not in memory.get("regions", {}):
        if "regions" not in memory:
            memory["regions"] = {}
        memory["regions"][region_key] = {
            "ra": round(ra, 2),
            "dec": round(dec, 2),
            "visits": 0,
            "tools_used": [],
            "outcomes": [],
            "findings": [],
            "notes": [],
            "exhausted": False,
            "first_cycle": memory.get("total_cycles_all_runs", 0),
            "last_cycle": memory.get("total_cycles_all_runs", 0),
        }

    reg = memory["regions"][region_key]
    reg["exhausted"] = True
    if "notes" not in reg:
        reg["notes"] = []
    reg["notes"].append({
        "text": f"[EXHAUSTED] {reason}",
        "cycle": memory.get("total_cycles_all_runs", 0),
        "timestamp": datetime.datetime.now().isoformat(),
    })

    # Also dismiss any leads near this region
    before = len(memory.get("best_leads", []))
    memory["best_leads"] = [
        l for l in memory.get("best_leads", [])
        if not (abs(l["ra"] - ra) < 1.5 and abs(l["dec"] - dec) < 1.5)
    ]
    leads_removed = before - len(memory["best_leads"])

    return {
        "status": "ok",
        "region": region_key,
        "exhausted": True,
        "leads_removed": leads_removed,
        "reason": reason,
        "message": f"Region RA={ra}, Dec={dec} marked as EXHAUSTED. "
                   f"{leads_removed} lead(s) also dismissed. "
                   f"This region will be deprioritized in future summaries. Move on to fresh targets!",
    }


def _summarize_result(tool_name, result, max_len=300):
    """Create a concise log-friendly summary of a tool result.

    Returns a string ≤ max_len chars showing the most useful fields.
    """
    if not isinstance(result, dict):
        s = str(result)
        return s[:max_len] + "…" if len(s) > max_len else s

    # Errors — always show in full (they're short)
    if "error" in result:
        return f"ERROR: {result['error'][:max_len]}"

    # Per-tool summaries: pick the most informative fields
    parts = []

    # Gaia
    if tool_name == "query_gaia":
        n = result.get("n_sources", "?")
        parts.append(f"n_sources={n}")
        for src in result.get("sources", [])[:2]:
            mag = src.get("phot_g_mean_mag", "?")
            pm = ""
            pmra = src.get("pmra")
            pmdec = src.get("pmdec")
            if pmra is not None and pmdec is not None:
                try:
                    pm_total = (float(pmra)**2 + float(pmdec)**2)**0.5
                    pm = f" pm={pm_total:.1f}mas/yr"
                except (ValueError, TypeError):
                    pass
            vc = src.get("variable_class", "")
            vc_str = f" var={vc}" if vc else ""
            parts.append(f"G={mag}{pm}{vc_str}")

    # ALeRCE
    elif tool_name == "check_transients":
        n = result.get("n_matches", 0)
        parts.append(f"n_matches={n}")
        for obj in result.get("objects", [])[:2]:
            cn = obj.get("class_name", "?")
            cp = obj.get("class_probability", "?")
            parts.append(f"{obj.get('oid','?')}={cn}({cp})")

    # Photometry
    elif tool_name == "measure_photometry":
        mag = result.get("magnitude")
        snr = result.get("snr")
        flux = result.get("net_flux")
        if mag is not None:
            parts.append(f"mag={mag:.3f}" if isinstance(mag, float) else f"mag={mag}")
        if snr is not None:
            parts.append(f"SNR={snr:.1f}" if isinstance(snr, float) else f"SNR={snr}")
        if flux is not None:
            parts.append(f"flux={flux:.1f}" if isinstance(flux, float) else f"flux={flux}")

    # detect_sources
    elif tool_name == "detect_sources":
        n = result.get("n_sources", "?")
        parts.append(f"n_sources={n}")
        brightest = result.get("brightest_flux") or result.get("sources", [{}])[0].get("flux") if result.get("sources") else None
        if brightest:
            parts.append(f"brightest_flux={brightest}")

    # compare_images
    elif tool_name == "compare_images":
        n = result.get("n_significant", result.get("n_changed", "?"))
        parts.append(f"n_significant={n}")
        max_sig = result.get("max_sigma", "?")
        parts.append(f"max_sigma={max_sig}")

    # search_region / search_target
    elif tool_name in ("search_region", "search_target"):
        n = result.get("total_results", result.get("n_results", "?"))
        parts.append(f"results={n}")
        name = result.get("resolved_name") or result.get("target")
        if name:
            parts.append(f"name={name}")

    # multi_epoch
    elif tool_name == "multi_epoch":
        n = result.get("n_groups", "?")
        parts.append(f"groups={n}")

    # SIMBAD
    elif tool_name == "simbad_check":
        n = result.get("n_known_objects", "?")
        parts.append(f"n_objects={n}")
        for obj in result.get("objects", [])[:2]:
            parts.append(f"{obj.get('name','?')}({obj.get('type','?')})")

    # ZTF
    elif tool_name == "ztf_lightcurve":
        n = result.get("total_points", 0)
        var = result.get("variability_flag", False)
        parts.append(f"points={n} variable={var}")

    # Downloads
    elif tool_name in ("download_cutout", "download_multiepoch", "download_legacy"):
        n = result.get("n_images", result.get("n_downloaded", "?"))
        parts.append(f"n_images={n}")
        files = result.get("files", result.get("images", []))
        if files and isinstance(files, list):
            fnames = [os.path.basename(f) if isinstance(f, str) else f.get("filename", "?") for f in files[:3]]
            parts.append(f"files=[{', '.join(fnames)}]")

    # list_images
    elif tool_name == "list_images":
        n = result.get("n_images", "?")
        parts.append(f"n_images={n}")

    # Generic fallback — show top-level keys and short values
    if not parts:
        for k, v in result.items():
            if k.startswith("_"):
                continue
            sv = str(v)
            if len(sv) > 60:
                sv = sv[:57] + "…"
            parts.append(f"{k}={sv}")
            if len(", ".join(parts)) > max_len:
                break

    summary = ", ".join(parts)
    if len(summary) > max_len:
        summary = summary[:max_len - 1] + "…"
    return summary


def execute_tool(tool_name, params, memory=None):
    """Execute a tool by running the appropriate Python script."""
    if tool_name not in AVAILABLE_TOOLS:
        return {"error": f"Unknown tool: {tool_name}"}

    tool = AVAILABLE_TOOLS[tool_name]

    # Internal tools
    if tool["script"] == "__internal__":
        if tool_name == "log_finding":
            return log_finding(**params)
        if tool_name == "analyze_image":
            return _prepare_vision_request(**params)
        # Memory query tools — read from the live memory dict
        if tool_name == "query_memory":
            if memory is None:
                return {"error": "Memory not available"}
            return _query_memory(memory, **params)
        if tool_name == "list_findings":
            if memory is None:
                return {"error": "Memory not available"}
            return _list_findings(memory, **params)
        if tool_name == "list_unexplored":
            if memory is None:
                return {"error": "Memory not available"}
            return _list_unexplored(memory, **params)
        if tool_name == "my_stats":
            if memory is None:
                return {"error": "Memory not available"}
            return _my_stats(memory, **params)
        # Memory WRITE tools — Qwen manages its own knowledge
        if tool_name == "dismiss_lead":
            if memory is None:
                return {"error": "Memory not available"}
            return _dismiss_lead(memory, **params)
        if tool_name == "add_note":
            if memory is None:
                return {"error": "Memory not available"}
            return _add_note(memory, **params)
        if tool_name == "mark_exhausted":
            if memory is None:
                return {"error": "Memory not available"}
            return _mark_exhausted(memory, **params)
        return {"error": f"Unknown internal tool: {tool_name}"}
    
    # Normalize common parameter aliases (Qwen sometimes uses alternate names)
    aliases = {
        "radius_arcsec": "radius",
        "radius_deg": "radius",
        "target_name": "name",
        "target": "name",
        "img_path": "image",
        "image_path": "image",
        "size_arcmin": "size",
        "filter_name": "filter",
        "band": "filter",          # Qwen might say band='g' instead of filter='g'
        "num_epochs": "epochs",
        "n_epochs": "epochs",
        "aperture_radius": "aperture",
        "sky_inner": "inner",
        "sky_outer": "outer",
        "radius_arcsec_gaia": "radius",
    }
    for alt, canonical in aliases.items():
        if alt in params and canonical not in params:
            params[canonical] = params.pop(alt)

    # Set defaults for optional parameters (Qwen often omits them)
    if tool_name == "download_multiepoch":
        params.setdefault("filter", "g")
        params.setdefault("epochs", 3)
        params.setdefault("size", 1.0)
    elif tool_name == "download_legacy":
        params.setdefault("bands", "grz")
        params.setdefault("size", 256)
    elif tool_name == "download_cutout":
        params.setdefault("size", 1.0)
    elif tool_name == "query_gaia":
        params.setdefault("radius", 5)
    elif tool_name == "check_transients":
        params.setdefault("radius", 5)
    elif tool_name == "measure_photometry":
        params.setdefault("aperture", 5)
        params.setdefault("inner", 10)
        params.setdefault("outer", 15)

    # Normalize image file paths (Qwen sometimes outputs broken paths)
    for path_key in ("image", "img1", "img2"):
        if path_key in params and isinstance(params[path_key], str):
            p = params[path_key]
            # Fix double backslashes: data\\images\\file -> data/images/file
            p = p.replace("\\\\", "/").replace("\\", "/")
            # Strip absolute path prefix, keep relative from data/
            if "data/images/" in p:
                p = "data/images/" + p.split("data/images/")[-1]
            elif "data\\images\\" in p:
                p = "data/images/" + p.split("data\\images\\")[-1]
            # Normalize any remaining backslashes
            p = p.replace("\\", "/")
            params[path_key] = p
    
    # Note: fuzzy coordinate matching is handled in image_analysis.py's
    # read_image_data() via fuzzy_find_image() — no need to duplicate here

    # Build command
    try:
        cmd = tool["script"].format(**params)
    except KeyError as e:
        return {"error": f"Missing parameter: {e}"}
    
    log("TOOL", f"{tool_name}|Executing: python {cmd}")
    
    timeout = TOOL_TIMEOUTS.get(tool_name, DEFAULT_TOOL_TIMEOUT)

    try:
        # Use shlex.split to properly handle quoted arguments (e.g. "Crab Nebula")
        cmd_parts = shlex.split(cmd)
        result = subprocess.run(
            ["python"] + cmd_parts,
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )

        if result.returncode != 0:
            return {"error": f"Tool failed: {result.stderr[:500]}"}

        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"raw_output": result.stdout[:2000]}

    except subprocess.TimeoutExpired:
        return {"error": f"Tool timed out ({timeout}s)"}
    except Exception as e:
        return {"error": str(e)}

def log_finding(ra=0, dec=0, description="", significance="medium", **kwargs):
    """Log a potential astronomical finding."""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    finding_id = f"F{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    
    finding_path = os.path.join(FINDINGS_DIR, f"{finding_id}.json")
    
    # Append to findings.tsv
    if not os.path.exists(FINDINGS_FILE):
        with open(FINDINGS_FILE, "w") as f:
            f.write("id\ttimestamp\tra\tdec\tsignificance\tdescription\n")
    
    with open(FINDINGS_FILE, "a", encoding="utf-8") as f:
        f.write(f"{finding_id}\t{timestamp}\t{ra}\t{dec}\t{significance}\t{description}\n")
    
    # Save detailed JSON
    with open(finding_path, "w", encoding="utf-8") as f:
        json.dump({
            "id": finding_id,
            "timestamp": timestamp,
            "ra": ra,
            "dec": dec,
            "significance": significance,
            "description": description,
            "extra": kwargs,
        }, f, indent=2)
    
    log("FIND", f"Logged finding {finding_id}: {description[:80]}", ra=ra, dec=dec)
    
    return {"status": "logged", "finding_id": finding_id}


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------


def deduplicate_response(response):
    """Detect and remove repeated blocks from Qwen output.
    
    When Qwen gets 'excited' about a finding, it sometimes repeats its
    THOUGHT block many times, consuming all num_predict tokens without
    ever emitting TOOL: lines. This detects that pattern and truncates.
    
    Also detects repeated non-THOUGHT lines (Qwen sometimes repeats
    entire paragraphs without the THOUGHT: prefix).
    """
    lines = response.split("\n")
    
    # Count all non-empty line occurrences (not just THOUGHT: lines)
    line_counts = {}
    for line in lines:
        stripped = line.strip()
        if len(stripped) > 20:  # Only count substantial lines
            line_counts[stripped] = line_counts.get(stripped, 0) + 1
    
    # If any substantial line appears 3+ times, we have a repetition loop
    max_repeats = max(line_counts.values()) if line_counts else 0
    if max_repeats < 3:
        return response, False  # No repetition detected
    
    # Deduplicate: keep first occurrence of each line, always keep TOOL lines
    seen_lines = set()
    deduped_lines = []
    repeated = False
    
    for line in lines:
        stripped = line.strip()
        # Always keep TOOL: lines (never deduplicate actions)
        if stripped.upper().startswith("TOOL:"):
            deduped_lines.append(line)
            continue
        # Keep short lines (empty, separators, etc.)
        if len(stripped) <= 20:
            deduped_lines.append(line)
            continue
        # Deduplicate substantial repeated lines
        if stripped in seen_lines:
            repeated = True
            continue  # Skip duplicate
        seen_lines.add(stripped)
        deduped_lines.append(line)
    
    deduped = "\n".join(deduped_lines)
    return deduped, repeated

def parse_tool_calls(response):
    """Parse Qwen's response for THOUGHT and TOOL lines.
    
    Handles multiple formats Qwen uses:
      TOOL: tool_name(param1=value1, param2=value2)    # standard
      TOOL: tool_name param1=value1 param2=value2       # no parens
      TOOL: tool_name RA=83.63 Dec=22.01                # coordinate shorthand
      TOOL: tool_name({"param": "value"})               # JSON-like
      TOOL_ARGS: {"param": "value"}                     # separate args line
    """
    thoughts = []
    tool_calls = []
    pending_tool_name = None  # for TOOL_ARGS on next line
    
    for line in response.split("\n"):
        line = line.strip()
        
        if line.upper().startswith("THOUGHT:"):
            thoughts.append(line[8:].strip())
            continue
        
        # Handle TOOL_ARGS: on a separate line
        if line.upper().startswith("TOOL_ARGS:") and pending_tool_name:
            args_str = line[10:].strip()
            try:
                params = json.loads(args_str)
                # Normalize keys to lowercase
                params = {k.lower(): v for k, v in params.items()}
                tool_calls.append({"tool": pending_tool_name, "params": params})
            except (json.JSONDecodeError, Exception):
                pass
            pending_tool_name = None
            continue
        
        if not line.upper().startswith("TOOL:"):
            pending_tool_name = None
            continue
        
        call_str = line[5:].strip()
        
        # Format 1: tool_name(param1=val1, param2=val2) — standard
        match = re.match(r"(\w+)\((.*)\)", call_str)
        if match:
            tool_name = match.group(1)
            params_str = match.group(2)
            
            # Try JSON parse first (handles {"key": "val"} format)
            params = {}
            if params_str.strip().startswith("{"):
                try:
                    params = json.loads(params_str)
                    params = {k.lower(): v for k, v in params.items()}
                except (json.JSONDecodeError, Exception):
                    pass
            
            if not params:
                # Parse key=value pairs
                for param_match in re.finditer(r"(\w+)\s*=\s*(?:'([^']*)'|\"([^\"]*)\"|([\'\"]?[^,\)]+))", params_str):
                    key = param_match.group(1).lower()
                    val = param_match.group(2) or param_match.group(3) or param_match.group(4)
                    if val:
                        val = val.strip().strip("\'\"\'")
                    try:
                        val = float(val)
                        if val == int(val):
                            val = int(val)
                    except (ValueError, TypeError):
                        pass
                    params[key] = val
            
            tool_calls.append({"tool": tool_name, "params": params})
            continue
        
        # Format 2: tool_name key=val key=val (no parentheses)
        parts = call_str.split()
        if len(parts) >= 1 and re.match(r"^\w+$", parts[0]):
            tool_name = parts[0]
            
            if len(parts) == 1:
                # Just a tool name with no args — might have TOOL_ARGS on next line
                pending_tool_name = tool_name
                # Also accept it as a no-arg call for tools like list_images
                tool_calls.append({"tool": tool_name, "params": {}})
                continue
            
            # Parse remaining as key=value pairs or "RA=val Dec=val" shorthand
            params = {}
            rest = " ".join(parts[1:])
            
            # Handle comma-separated or space-separated key=value
            for m in re.finditer(r"(\w+)\s*=\s*(?:'([^']*)'|\"([^\"]*)\"|(\S+))", rest):
                key = m.group(1).lower()
                val = m.group(2) or m.group(3) or m.group(4)
                if val:
                    val = val.strip(",").strip()
                try:
                    val = float(val)
                    if val == int(val):
                        val = int(val)
                except (ValueError, TypeError):
                    pass
                params[key] = val
            
            if params:
                # Convert RA/Dec shorthand to search_region if needed
                if tool_name == "search_target" and "ra" in params and "dec" in params and "name" not in params:
                    # Qwen is using search_target with coords — redirect to search_region
                    tool_name = "search_region"
                    if "radius" not in params:
                        params["radius"] = 0.05  # default
                
                # Remove the bare tool_name call we might have added
                if tool_calls and tool_calls[-1]["tool"] == parts[0] and not tool_calls[-1]["params"]:
                    tool_calls.pop()
                
                tool_calls.append({"tool": tool_name, "params": params})
    
    # Post-process: fix search_target called with coordinate-like names
    for call in tool_calls:
        if call["tool"] == "search_target":
            name = call["params"].get("name", "")
            if isinstance(name, str) and re.match(r"RA\s*=\s*[\d.]+", name):
                # Extract RA and Dec from the name string
                ra_match = re.search(r"RA\s*=?\s*([\d.]+)", name)
                dec_match = re.search(r"Dec\s*=?\s*([-\d.]+)", name)
                if ra_match and dec_match:
                    call["tool"] = "search_region"
                    call["params"] = {
                        "ra": float(ra_match.group(1)),
                        "dec": float(dec_match.group(1)),
                        "radius": 0.05,
                    }
    
    return {
        "thoughts": thoughts,
        "tool_calls": tool_calls,
    }

# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def build_system_prompt():
    """Build the system prompt with available tools."""
    tools_desc = "\n".join(
        f"  - {name}: {info['description']}\n    Usage: {info['usage']}"
        for name, info in AVAILABLE_TOOLS.items()
    )
    
    return f"""You are an astronomical research agent. Your mission is to discover 
unreported transients, variable objects, or anomalies in the sky by querying 
real astronomical archives.

Available tools:
{tools_desc}

Your approach — think like an astronomer:
1. Pick a region (galaxy cluster, nebula, galactic plane, or random unexplored patch)
2. Check what's already known: search_region for archival observations, simbad_check for cataloged objects
3. Download images: download_cutout for multi-band Pan-STARRS stacked images
4. Get TEMPORAL data: download_multiepoch to get same-band images from DIFFERENT DATES — this is ESSENTIAL for transient detection!
5. Analyze quantitatively: detect_sources gives exact source positions, fluxes, and SNR
6. Compare SAME-BAND different-epoch images: compare_images on warp_..._ep1 vs warp_..._ep3 finds what CHANGED over time — this is how you find transients!
7. Cross-check with independent survey: download_legacy for Legacy Survey images (~5yr baseline from Pan-STARRS)
8. Log discoveries: log_finding ONLY when you found something noteworthy

How to investigate a region thoroughly (call multiple tools per cycle!):

  Cycle 1 — Discovery:
    TOOL: search_region(ra=..., dec=..., radius=0.05)
    TOOL: simbad_check(ra=..., dec=..., radius=60)
    TOOL: download_cutout(ra=..., dec=..., size=1.0)

  Cycle 2 — Multi-epoch temporal data (CRITICAL for transient detection):
    TOOL: download_multiepoch(ra=..., dec=..., filter='g', epochs=3)
    TOOL: download_multiepoch(ra=..., dec=..., filter='r', epochs=3)

  Cycle 3 — Temporal comparison (THIS finds real transients):
    TOOL: list_images()
    TOOL: compare_images(img1='data/images/warp_..._g_ep1_mjd....fits', img2='data/images/warp_..._g_ep3_mjd....fits')

  Cycle 4 — Independent cross-check with different survey:
    TOOL: download_legacy(ra=..., dec=..., bands='grz')
    TOOL: compare_images(img1='data/images/cutout_..._g.fits', img2='data/images/legacy_..._g.fits')

  Cycle 5 — VALIDATE before logging (CRITICAL! Eliminate false positives):
    TOOL: simbad_check(ra=..., dec=..., radius=30)
    TOOL: query_gaia(ra=..., dec=..., radius=5)
    TOOL: check_transients(ra=..., dec=..., radius=5)
    TOOL: measure_photometry(image='data/images/warp_..._g_ep1_mjd....fits', ra=..., dec=...)
    TOOL: measure_photometry(image='data/images/warp_..._g_ep3_mjd....fits', ra=..., dec=...)

  Cycle 6 — Conclusion (only if validation passed!):
    TOOL: log_finding(ra=..., dec=..., description='Brightening transient: source increased <MEASURED_MAG_CHANGE> mag between MJD <EARLIER_EPOCH> and MJD <LATER_EPOCH> in <BAND>-band. NOT in SIMBAD/Gaia. No ALeRCE match — potentially novel! Photometry: mag=<EPOCH1_MAG>→<EPOCH2_MAG>.', significance='high')

VALIDATION WORKFLOW — BEFORE logging ANY finding with significance='high':
  0. simbad_check(ra, dec, radius=30) → If a known object is found, it's NOT novel. Downgrade to 'medium' or skip.
  1. query_gaia(ra, dec) → If variable_class is set, it's a KNOWN variable star (not novel)
     High proper motion (pm_total > 50 mas/yr) means it's a nearby moving star, not a transient
  2. check_transients(ra, dec) → If ALeRCE returns matches, the transient is ALREADY KNOWN
     Check class_name: SN=supernova, AGN=active galaxy, VS=variable star, bogus=artifact
  3. measure_photometry(image, ra, dec) → Get precise magnitude in EACH epoch
     Compare magnitudes to quantify the brightness change
     Include these numbers in your log_finding description!
  4. Only log_finding with significance='high' if the candidate SURVIVES all checks

detect_sources is your most powerful tool — it gives you hard numbers:
  - Exact pixel positions and RA/Dec coordinates of every source
  - Peak flux and SNR for each source
  - Number of sources per image
  Use it on downloaded images. Compare source counts across bands.

*** CRITICAL: compare_images rules ***
  - NEVER compare different bands (g vs r, g vs i) for transient detection!
    Cross-band differences are just NORMAL STELLAR COLORS. Stars are naturally brighter in redder bands.
    If compare_images reports "CROSS-BAND COMPARISON", the result is INVALID for transients!
  - ALWAYS compare SAME-BAND, DIFFERENT-EPOCH images to find real transients:
    * warp_..._g_ep1 vs warp_..._g_ep3 (Pan-STARRS temporal comparison)
    * cutout_..._g vs legacy_..._g (cross-survey same-band comparison)
  - The ONLY way to detect transients is with same-band, different-time images!

File naming guide:
  - cutout_RA_DEC_g.fits     = Pan-STARRS STACKED image (single epoch per band)
  - warp_RA_DEC_g_ep1_mjdXXXXX.X.fits = Pan-STARRS WARP (individual epoch, DATE in filename!)
  - legacy_RA_DEC_g.fits     = Legacy Survey DR10 image (independent survey, different epoch)
  For transient detection, compare files with SAME band letter but DIFFERENT epochs or surveys.

When to use log_finding:
  - compare_images on same-band different-epoch images found brightening/fading → significance='high'
  - Source detected in one epoch but missing in another → significance='high'
  - Sources NOT in SIMBAD → log with significance='medium'
  - Nothing interesting found → do NOT log. Just move to the next region.
  - Include NUMBERS in your description: source count, SNR, flux, magnitude, coordinates, MJD dates
  - NEVER copy numbers from examples — use YOUR OWN measurements from detect_sources, compare_images, measure_photometry
  - If you haven't measured the exact magnitude change, say "flux ratio X:Y" instead of making up a mag value
  - Every number in your description MUST come from a tool result you received this session

Always respond with:
THOUGHT: Your scientific reasoning — what you expect to find and why
TOOL: tool_name(param1=value1, param2=value2)

You can call 2-4 tools per response (one TOOL: per line).

Memory READ tools — use these to avoid repeating work or guessing:
- query_memory(ra=150.0, dec=30.0, radius=5.0) — check what you already did in a region before revisiting
- list_findings(significance='high') — review your best past discoveries
- list_unexplored() — find sky gaps you haven't visited yet and get suggested coordinates
- my_stats() — see YOUR OWN performance dashboard: findings breakdown, regions explored vs exhausted, tool usage, coverage stats, and strategic recommendations. Call this every ~20 cycles to reflect and adjust your strategy!

Memory WRITE tools — YOU manage your own knowledge base:
- dismiss_lead(ra=143.0, dec=60.0, reason='Thoroughly analyzed, all sources cataloged in SIMBAD') — remove a lead from your priority list so you stop going back to it
- add_note(ra=143.0, dec=60.0, note='All 15 sources matched SIMBAD. No transients. Only normal field stars.') — write a note to your future self about a region
- mark_exhausted(ra=143.0, dec=60.0, reason='Downloaded g/r/i bands, detected sources, checked SIMBAD, no anomalies') — flag a region as fully investigated

TAKE YOUR TIME investigating each region thoroughly! A proper investigation needs multiple cycles:
  - Download multi-band cutouts + multi-epoch warps (2-3 cycles)
  - Detect sources, compare images, look for transients (2-3 cycles)
  - Validate candidates with SIMBAD, Gaia, ALeRCE, photometry (1-2 cycles)
  - Log findings or conclude (1 cycle)
Do NOT rush to mark_exhausted — only use it when you've genuinely completed ALL steps above.
Use add_note to record partial progress so you remember where you left off.

When you're truly DONE with a region (nothing left to check):
  TOOL: add_note(ra=..., dec=..., note='Summary: 12 sources detected, all match SIMBAD. No transients.')
  TOOL: mark_exhausted(ra=..., dec=..., reason='All bands analyzed, sources cataloged, nothing anomalous')

Tool rules:
- search_target(name='NGC 1234') — NAMED objects only
- search_region(ra=150, dec=30, radius=0.05) — COORDINATES
- detect_sources and analyze_image work on any image format (FITS or JPEG)
- For images: call list_images() first to get exact file paths
- You CAN call the same tool again with different parameters (e.g. different sigma, different image)
- Before revisiting a region, use query_memory to check what you already did there

Validation tools — use these to verify candidates BEFORE logging findings:
- query_gaia(ra=150.0, dec=30.0, radius=5) — check Gaia DR3 for known variable stars, proper motion, distance
- check_transients(ra=150.0, dec=30.0, radius=5) — check ALeRCE/ZTF broker for known transients
- measure_photometry(image='data/images/warp_...fits', ra=150.0, dec=30.0) — precise aperture photometry (magnitude, flux, SNR)

Coordinates:
- RA in degrees (0-360), Dec in degrees (-90 to +90)
- MAST radius in degrees (0.01-1.0), SIMBAD radius in arcsec (1-300)
- Gaia/ALeRCE radius in arcsec (default 5)
- Northern sky (Dec > -30) has best ZTF coverage

Format (MUST use parentheses):
  TOOL: search_region(ra=150.0, dec=30.0, radius=0.05)
  TOOL: download_multiepoch(ra=150.0, dec=30.0, filter='g', epochs=3)
  TOOL: download_legacy(ra=150.0, dec=30.0, bands='grz')
  TOOL: detect_sources(image='data/images/cutout_150.0000_30.0000_g.fits', sigma=3.0)
  TOOL: compare_images(img1='data/images/warp_150.0000_30.0000_g_ep1_mjdXXXXX.X.fits', img2='data/images/warp_150.0000_30.0000_g_ep3_mjdXXXXX.X.fits')
  TOOL: query_gaia(ra=150.0, dec=30.0, radius=5)
  TOOL: check_transients(ra=150.0, dec=30.0, radius=5)
  TOOL: measure_photometry(image='data/images/warp_150.0000_30.0000_g_ep1_mjdXXXXX.X.fits', ra=150.0, dec=30.0)
"""


def build_reflection_prompt(cycle_num, turn_num, tool_results):
    """Build a lightweight prompt showing tool results for mid-cycle reflection.

    Unlike build_user_prompt (which carries full history/memory), this is
    small and focused: just the results from the tools that just ran, plus
    a clear instruction to either act on them or signal completion.
    """
    results_text = ""
    for r in tool_results:
        results_text += f"\n### {r['tool']}({r.get('params', {})}):\n```json\n"
        result_str = json.dumps(r["result"], indent=2, default=str)
        if len(result_str) > 1500:
            result_str = result_str[:1500] + "\n... (truncated)"
        results_text += result_str + "\n```\n"

    return f"""Cycle {cycle_num}, Turn {turn_num} — REFLECTION

Here are the results from the tools you just called:
{results_text}

Based on these results, what is your NEXT ACTION?

Options:
- Call MORE tools to dig deeper (e.g., download more bands, validate a detection, measure photometry, check catalogs)
- Call log_finding() if you've confirmed a real discovery
- Call add_note() to record partial progress or observations
- If you've completed ALL investigation steps (multi-band + multi-epoch + validation), use mark_exhausted() to close the region
- If you're DONE with this region and ready to move on, just write THOUGHT: explaining your conclusion.

Use THOUGHT: and TOOL: format as always.
"""


def build_user_prompt(cycle_num, previous_results, research_history,
                      initial_target=None, visited_regions=None,
                      current_region_cycles=0, ztf_blacklist=None,
                      next_seed_target=None, memory_summary=None):
    """Build the user prompt with context from previous cycles."""

    # Persistent memory (cross-run knowledge)
    memory_text = ""
    if memory_summary:
        memory_text = f"\n{memory_summary}\n"

    # Build history summary (expanded window)
    history_text = ""
    if research_history:
        history_text = "\n## Research History (last 10 cycles)\n"
        for entry in research_history[-10:]:
            history_text += f"- Cycle {entry['cycle']}: {entry['summary']}\n"

    # Build previous results
    results_text = ""
    if previous_results:
        results_text = "\n## Results from Previous Cycle\n"
        for r in previous_results:
            results_text += f"\n### {r['tool']}:\n```json\n"
            result_str = json.dumps(r['result'], indent=2, default=str)
            if len(result_str) > 1500:
                result_str = result_str[:1500] + "\n... (truncated)"
            results_text += result_str + "\n```\n"

    # Already explored regions (show top 30 most-visited to save context)
    explored_text = ""
    if visited_regions:
        sorted_regions = sorted(visited_regions.items(), key=lambda x: x[1], reverse=True)[:30]
        explored_text = f"\n## Explored regions ({len(visited_regions)} total, showing top 30):\n"
        for (ra, dec), count in sorted_regions:
            explored_text += f"- RA={ra}, Dec={dec} ({count}x)\n"

    # ZTF blacklist
    ztf_text = ""
    if ztf_blacklist:
        ztf_text = "\n## 🚫 ZTF BLACKLISTED REGIONS (timed out — do NOT retry ZTF here):\n"
        for (ra, dec) in ztf_blacklist:
            ztf_text += f"- RA={ra}, Dec={dec}\n"

    target_hint = ""
    if initial_target and cycle_num == 0:
        target_hint = f"\n## Starting Point\nBegin your research with: {initial_target}\n"

    return f"""Research Cycle {cycle_num}
{target_hint}
{memory_text}
{history_text}
{explored_text}
{ztf_text}
{results_text}

What would you like to investigate next? Remember to use THOUGHT: and TOOL: format.
"""


# ---------------------------------------------------------------------------
# Main research loop
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="AstroResearch Orchestrator")
    parser.add_argument("--model", default="qwen3.5:4b", help="Ollama model (default: qwen3.5:4b)")
    parser.add_argument("--max-cycles", type=int, default=0, help="Max research cycles (0=infinite)")
    parser.add_argument("--target", type=str, default=None, help="Initial target to investigate")
    parser.add_argument("--temperature", type=float, default=0.3, help="LLM temperature")
    parser.add_argument("--dry-run", action="store_true", help="Show prompts without calling Ollama")
    args = parser.parse_args()
    
    # --- Load persistent memory ---
    memory = load_memory()
    memory["run_count"] += 1
    log("INFO", f"Memory loaded: {len(memory['regions'])} regions, "
                f"{memory['total_cycles_all_runs']} total cycles across {memory['run_count']} run(s)")

    print_banner(args.model, memory, args.target)

    research_history = []
    previous_results = []
    cycle_num = 0
    
    consecutive_failures = 0
    max_failures = 5
    
    # Anti-loop state
    visited_regions = {}          # {(ra, dec): query_count}
    tool_call_history = {}        # {(tool, ra, dec, radius): last_cycle_called}
    TOOL_COOLDOWN = 0             # Cooldown disabled — Qwen can freely re-query any region
    EXEMPT_FROM_COOLDOWN = {"log_finding", "download_cutout", "download_multiepoch",
                            "download_legacy", "list_images",
                            "analyze_image", "detect_sources", "compare_images",
                            "convert_to_png",
                            "query_memory", "list_findings", "list_unexplored", "my_stats",
                            "dismiss_lead", "add_note", "mark_exhausted",
                            "query_gaia", "check_transients", "measure_photometry"}  # Image + memory + validation tools
    ztf_blacklist = set()         # {(ra, dec)} where ZTF timed out 2+ times
    ztf_fail_count = {}           # {(ra, dec): timeout_count}
    current_region = None         # (ra, dec) of current focus
    current_region_cycles = 0    # how many cycles spent on current region
    recent_regions = []          # last 15 regions visited (for alternating loop detection)
    seed_index = 0               # next seed target to suggest
    no_progress_count = 0         # consecutive cycles with no useful work
    pending_images = []           # base64 images queued for next call_ollama
    
    while True:
        if args.max_cycles > 0 and cycle_num >= args.max_cycles:
            log("INFO", f"Reached max cycles ({args.max_cycles}). Stopping.")
            break
        
        if consecutive_failures >= 15:
            log("ERROR", f"Too many consecutive failures ({consecutive_failures}). Stopping.")
            break
        
        print_cycle_header(cycle_num)
        
        # Pick next seed target to suggest if stuck
        next_seed = None
        if seed_index < len(SEED_TARGETS):
            next_seed = SEED_TARGETS[seed_index]
        
        system_prompt = build_system_prompt()
        mem_summary = summarize_memory(memory) if memory["regions"] else None
        user_prompt = build_user_prompt(
            cycle_num, previous_results, research_history, args.target,
            visited_regions=visited_regions,
            current_region_cycles=current_region_cycles,
            ztf_blacklist=ztf_blacklist,
            next_seed_target=next_seed,
            memory_summary=mem_summary,
        )
        
        if args.dry_run:
            print("SYSTEM PROMPT:")
            print(system_prompt[:500] + "...")
            print("\nUSER PROMPT:")
            print(user_prompt)
            cycle_num += 1
            continue
        
        log("INFO", "Asking Qwen for research direction...")
        _write_dashboard_status(running=True, cycle=cycle_num, phase="thinking", current_tool=None)
        images_for_this_call = pending_images if pending_images else None
        pending_images = []  # Clear after use
        response, elapsed = call_ollama(args.model, system_prompt, user_prompt, args.temperature, images=images_for_this_call)

        if response is None:
            log("WARN", "No response from Qwen. Retrying in 15s...")
            time.sleep(15)
            consecutive_failures += 1
            continue
        
        # Deduplicate repeated THOUGHT blocks (Qwen repetition bug)
        was_repeated = False
        try:
            response, was_repeated = deduplicate_response(response)
            if was_repeated:
                log("WARN", "Detected repeated THOUGHT blocks — deduplicated")
        except Exception:
            pass  # Never crash on deduplication
        
        # Parse tool calls
        parsed = parse_tool_calls(response)
        
        if parsed["thoughts"]:
            for thought in parsed["thoughts"]:
                log("THINK", thought)
                _write_dashboard_status(running=True, cycle=cycle_num, phase="thought", last_thought=thought)
        
        if not parsed["tool_calls"]:
            log("WARN", "No tool calls found in response")

            # --- Recovery: if repetition loop, re-prompt for tool calls ---
            if was_repeated and parsed["thoughts"]:
                log("WARN", "Repetition loop consumed all tokens — attempting recovery re-prompt")
                thought_text = parsed["thoughts"][0]
                recovered_calls = recovery_reprompt(args.model, thought_text, args.temperature)

                if recovered_calls:
                    log("OK", f"Recovery re-prompt yielded {len(recovered_calls)} tool call(s)")
                    parsed["tool_calls"] = recovered_calls
                    # Fall through to normal tool execution below
                else:
                    # Last resort: auto-log the finding so it isn't lost
                    log("WARN", "Recovery re-prompt failed — auto-logging finding from thought")
                    result = autolog_from_thought(thought_text)
                    if result:
                        previous_results = [{"tool": "log_finding", "result": result}]
                    else:
                        log("WARN", "Could not extract coordinates from thought — finding lost")
                        previous_results = [{
                            "tool": "system",
                            "result": {"message": "Please use the TOOL: format. Example: TOOL: search_region(ra=150.0, dec=30.0, radius=0.05)"}
                        }]
                    no_progress_count += 1
                    consecutive_failures += 1
                    cycle_num += 1
                    continue

            if not parsed["tool_calls"]:
                log("INFO", f"Raw response (first 500): {response[:500]}")
                no_progress_count += 1
                consecutive_failures += 1

                # If stuck for 5+ cycles, inject a random target to break the loop
                if no_progress_count >= 5:
                    rand_ra = round(random.uniform(0, 360), 2)
                    rand_dec = round(random.uniform(-30, 80), 2)
                    log("INFO", f"Injecting random coordinates to break loop: RA={rand_ra}, Dec={rand_dec}")
                    previous_results = [{
                        "tool": "system",
                        "result": {
                            "message": f"You seem stuck. Here are FRESH random coordinates to explore: RA={rand_ra}, Dec={rand_dec}. "
                                       f"Use: TOOL: search_region(ra={rand_ra}, dec={rand_dec}, radius=0.05)"
                        }
                    }]
                    no_progress_count = 0
                else:
                    previous_results = [{
                        "tool": "system",
                        "result": {"message": "Please use the TOOL: format. Example: TOOL: search_region(ra=150.0, dec=30.0, radius=0.05)"}
                    }]
                cycle_num += 1
                continue
        
        # ===============================================================
        # INNER TURN LOOP — Qwen acts, sees results, acts again
        # ===============================================================
        MAX_TURNS_PER_CYCLE = 5
        consecutive_failures = 0
        no_progress_count = 0
        previous_results = []
        cycle_summary_parts = []
        turn_num = 1
        current_tool_calls = parsed["tool_calls"]

        while current_tool_calls and turn_num <= MAX_TURNS_PER_CYCLE:
            if turn_num > 1:
                log("INFO", f"── Turn {turn_num}/{MAX_TURNS_PER_CYCLE} (reflection) ──")

            turn_results = []  # Results from THIS turn only

            for call in current_tool_calls:
                tool_name = call["tool"]
                params = call["params"]

                # --- Normalize aliases BEFORE cache check ---
                aliases = {
                    "radius_arcsec": "radius",
                    "radius_deg": "radius",
                    "target_name": "name",
                    "target": "name",
                    "img_path": "image",
                    "image_path": "image",
                    "size_arcmin": "size",
                }
                for alt, canonical in aliases.items():
                    if alt in params and canonical not in params:
                        params[canonical] = params.pop(alt)

                # --- Anti-loop: track current region ---
                ra = params.get("ra")
                dec = params.get("dec")
                if ra is not None and dec is not None:
                    region = _region_key(ra, dec)
                    visited_regions[region] = visited_regions.get(region, 0) + 1

                    if current_region != region:
                        current_region = region
                        current_region_cycles = 1
                    else:
                        current_region_cycles += 1

                    # --- Anti-loop: VISIT CEILING — auto-exhaust overvisited regions ---
                    # NOTE: A thorough investigation (download + detect + compare + validate
                    # + photometry) easily uses 15-30 tool calls on one region. Set ceiling
                    # high enough to let Qwen work deeply.
                    VISIT_CEILING = 50
                    region_key_str = f"{round(float(ra), 1)},{round(float(dec), 1)}"
                    mem_region = memory.get("regions", {}).get(region_key_str)
                    if mem_region and mem_region.get("visits", 0) >= VISIT_CEILING and not mem_region.get("exhausted"):
                        log("WARN", f"VISIT CEILING: RA={ra}, Dec={dec} has {mem_region['visits']} visits — auto-exhausting")
                        mem_region["exhausted"] = True
                        if "notes" not in mem_region:
                            mem_region["notes"] = []
                        mem_region["notes"].append({
                            "text": f"[AUTO-EXHAUSTED] Visit ceiling ({VISIT_CEILING}) reached. Region force-closed.",
                            "cycle": memory.get("total_cycles_all_runs", 0),
                            "timestamp": datetime.datetime.now().isoformat(),
                        })
                        memory["best_leads"] = [
                            l for l in memory.get("best_leads", [])
                            if not (abs(l["ra"] - float(ra)) < 1.5 and abs(l["dec"] - float(dec)) < 1.5)
                        ]
                        rand_ra = round(random.uniform(0, 360), 2)
                        rand_dec = round(random.uniform(-30, 80), 2)
                        result = {
                            "blocked": True,
                            "message": f"⚠ VISIT CEILING: This region (RA={ra}, Dec={dec}) has been visited {mem_region['visits']} times and is now EXHAUSTED. "
                                       f"Explore a NEW region instead. Try: search_region(ra={rand_ra}, dec={rand_dec}, radius=0.05)"
                        }
                        turn_results.append({"tool": tool_name, "params": params, "result": result})
                        cycle_summary_parts.append(f"{tool_name}: BLOCKED (visit ceiling)")
                        save_memory(memory)
                        continue

                # --- Anti-loop: cooldown-based duplicate prevention ---
                cache_key = _tool_cache_key(tool_name, params)
                if tool_name not in EXEMPT_FROM_COOLDOWN and cache_key in tool_call_history:
                    cycles_since = cycle_num - tool_call_history[cache_key]
                    if cycles_since < TOOL_COOLDOWN:
                        log("WARN", f"COOLDOWN: {tool_name}({params}) — called {cycles_since} cycles ago, wait {TOOL_COOLDOWN - cycles_since} more")
                        result = {
                            "cooldown": True,
                            "message": f"This exact call was made {cycles_since} cycles ago. Try a DIFFERENT tool on these coords, or explore new coordinates. Cooldown resets in {TOOL_COOLDOWN - cycles_since} cycles.",
                        }
                        turn_results.append({"tool": tool_name, "params": params, "result": result})
                        cycle_summary_parts.append(f"{tool_name}: COOLDOWN ({cycles_since}/{TOOL_COOLDOWN})")
                        continue

                # --- Anti-loop: ZTF blacklist ---
                if tool_name == "ztf_lightcurve" and ra is not None and dec is not None:
                    ztf_region = _region_key(ra, dec)
                    if ztf_region in ztf_blacklist:
                        log("WARN", f"BLOCKED ZTF at {ztf_region} — blacklisted after repeated timeouts")
                        result = {
                            "blocked": True,
                            "message": f"ZTF is BLACKLISTED for this region (RA={ra}, Dec={dec}) due to repeated timeouts. Move to a different region.",
                        }
                        turn_results.append({"tool": tool_name, "params": params, "result": result})
                        cycle_summary_parts.append(f"ZTF: BLOCKED (blacklisted)")
                        continue

                # (Image tools are exempt from cooldown — Qwen can re-analyze freely)

                log("TOOL", f"{tool_name}|Calling {tool_name}({params})")
                _write_dashboard_status(running=True, cycle=cycle_num, phase="tool", current_tool=tool_name)
                try:
                    result = execute_tool(tool_name, params, memory=memory)
                except Exception as tool_err:
                    log("ERROR", f"Tool {tool_name} crashed: {type(tool_err).__name__}: {tool_err}")
                    result = {"error": f"Tool crashed: {tool_err}"}

                # --- Vision: queue image for next turn's inline prompt ---
                if isinstance(result, dict) and result.get("__vision__"):
                    pending_images.append(result["image_b64"])
                    log("OK", f"Image queued for inline vision: {result.get('image_path', '?')}")
                    result = {
                        "status": result["status"],
                        "image_path": result.get("image_path", ""),
                        "png_path": result.get("png_path", ""),
                        "prompt": result.get("prompt", ""),
                    }

                # --- Log the result for debugging ---
                result_summary = _summarize_result(tool_name, result)
                log("RESULT", f"{tool_name}|{result_summary}")

                # --- Track when this call was made ---
                tool_call_history[cache_key] = cycle_num

                # --- Anti-loop: track ZTF failures ---
                if tool_name == "ztf_lightcurve" and "error" in result and "timeout" in str(result.get("error", "")).lower():
                    if ra is not None and dec is not None:
                        ztf_region = _region_key(ra, dec)
                        ztf_fail_count[ztf_region] = ztf_fail_count.get(ztf_region, 0) + 1
                        if ztf_fail_count[ztf_region] >= 2:
                            ztf_blacklist.add(ztf_region)
                            log("WARN", f"ZTF blacklisted for region {ztf_region} after {ztf_fail_count[ztf_region]} timeouts")

                turn_results.append({
                    "tool": tool_name,
                    "params": params,
                    "result": result,
                })

                # Build summary
                if "error" in result:
                    cycle_summary_parts.append(f"{tool_name}: ERROR - {result['error']}")
                elif tool_name == "search_region" or tool_name == "search_target":
                    n = result.get("total_results", 0)
                    cycle_summary_parts.append(f"{tool_name}: {n} observations found")
                elif tool_name == "multi_epoch":
                    n = result.get("n_groups", 0)
                    cycle_summary_parts.append(f"{tool_name}: {n} multi-epoch groups")
                elif tool_name == "ztf_lightcurve":
                    n = result.get("total_points", 0)
                    var = result.get("variability_flag", False)
                    cycle_summary_parts.append(f"ZTF: {n} points, variable={var}")
                elif tool_name == "simbad_check":
                    n = result.get("n_known_objects", 0)
                    cycle_summary_parts.append(f"SIMBAD: {n} known objects nearby")
                elif tool_name == "download_multiepoch":
                    n = result.get("n_images", 0)
                    baseline = result.get("time_baseline_days", "?")
                    cycle_summary_parts.append(f"download_multiepoch: {n} epochs ({baseline}d baseline)")
                elif tool_name == "download_legacy":
                    n = result.get("n_images", 0)
                    cycle_summary_parts.append(f"download_legacy: {n} bands from Legacy Survey")
                elif tool_name == "query_gaia":
                    n = result.get("n_sources", 0)
                    cycle_summary_parts.append(f"query_gaia: {n} Gaia sources")
                elif tool_name == "check_transients":
                    n = result.get("n_matches", 0)
                    cycle_summary_parts.append(f"check_transients: {n} known transients")
                elif tool_name == "measure_photometry":
                    mag = result.get("magnitude")
                    snr = result.get("snr")
                    if mag is not None:
                        cycle_summary_parts.append(f"measure_photometry: mag={mag}, SNR={snr}")
                    else:
                        cycle_summary_parts.append(f"measure_photometry: source not detected")
                elif tool_name == "log_finding":
                    fid = result.get("finding_id", "?")
                    cycle_summary_parts.append(f"Finding logged: {fid}")
                else:
                    cycle_summary_parts.append(f"{tool_name}: done")

            # Accumulate turn results into cycle-wide previous_results
            previous_results.extend(turn_results)

            # --- REFLECTION: send results back to Qwen for next action ---
            if turn_num >= MAX_TURNS_PER_CYCLE:
                log("INFO", f"Max turns ({MAX_TURNS_PER_CYCLE}) reached — ending cycle")
                break

            # Build reflection prompt and ask Qwen what to do next
            log("INFO", f"Reflecting on {len(turn_results)} tool result(s)...")
            _write_dashboard_status(running=True, cycle=cycle_num, phase="reflecting", current_tool=None)

            reflection_prompt = build_reflection_prompt(cycle_num, turn_num, turn_results)
            images_for_reflect = pending_images if pending_images else None
            pending_images = []

            reflect_response, reflect_elapsed = call_ollama(
                args.model, system_prompt, reflection_prompt,
                args.temperature, images=images_for_reflect,
            )

            if reflect_response is None:
                log("WARN", "No reflection response from Qwen — ending cycle")
                break

            # Deduplicate
            try:
                reflect_response, _ = deduplicate_response(reflect_response)
            except Exception:
                pass

            reflect_parsed = parse_tool_calls(reflect_response)

            # Log reflection thoughts
            if reflect_parsed["thoughts"]:
                for thought in reflect_parsed["thoughts"]:
                    log("THINK", thought)
                    _write_dashboard_status(running=True, cycle=cycle_num, phase="thought", last_thought=thought)

            # If Qwen has no more tool calls, the cycle is done
            if not reflect_parsed["tool_calls"]:
                log("INFO", f"Qwen done reflecting (no more tools) — cycle complete after {turn_num} turn(s)")
                break

            # Otherwise, loop for another turn
            current_tool_calls = reflect_parsed["tool_calls"]
            turn_num += 1

        # Log total turns if we did multi-turn
        if turn_num > 1:
            log("OK", f"Cycle {cycle_num} completed in {turn_num} turn(s)")

        # --- Anti-loop: advance seed index when region changes ---
        if current_region_cycles >= 3 and seed_index < len(SEED_TARGETS):
            seed_index += 1

        # --- Track recent regions for alternating loop detection ---
        if current_region is not None:
            recent_regions.append(current_region)
            if len(recent_regions) > 25:
                recent_regions = recent_regions[-25:]

        # --- Stuck region breaker: auto-detect semantic loops ---
        # Detects TWO patterns:
        # 1) Consecutive: same region for 12+ cycles in a row (thorough work needs ~8-10)
        # 2) Alternating: only 2 unique regions in the last 20 cycles
        # NOTE: Qwen's proper workflow (download→detect→compare→validate→photometry)
        # legitimately stays on one region for many cycles. Only intervene for
        # genuine stuck loops, not deep investigation.
        STUCK_THRESHOLD = 12
        ALTERNATING_WINDOW = 20
        ALTERNATING_MAX_UNIQUE = 2
        used_write_tools = any(
            r.get("tool") in ("dismiss_lead", "add_note", "mark_exhausted")
            for r in previous_results
        )

        stuck_detected = False
        stuck_regions_to_exhaust = []

        # Pattern 1: Consecutive
        if current_region_cycles >= STUCK_THRESHOLD and not used_write_tools and current_region is not None:
            stuck_detected = True
            stuck_regions_to_exhaust = [current_region]
            log("WARN", f"STUCK LOOP (consecutive): {current_region_cycles} cycles on RA={current_region[0]}, Dec={current_region[1]}")

        # Pattern 2: Alternating (e.g., A→B→A→B→A→B)
        if not stuck_detected and len(recent_regions) >= ALTERNATING_WINDOW:
            window = recent_regions[-ALTERNATING_WINDOW:]
            unique_in_window = set(window)
            if len(unique_in_window) <= ALTERNATING_MAX_UNIQUE:
                stuck_detected = True
                stuck_regions_to_exhaust = list(unique_in_window)
                log("WARN", f"STUCK LOOP (alternating): only {len(unique_in_window)} unique regions in last {ALTERNATING_WINDOW} cycles: {unique_in_window}")

        if stuck_detected:
            for stuck_region in stuck_regions_to_exhaust:
                stuck_ra, stuck_dec = stuck_region
                region_key = f"{round(stuck_ra, 1)},{round(stuck_dec, 1)}"
                if region_key in memory.get("regions", {}):
                    reg = memory["regions"][region_key]
                    if not reg.get("exhausted"):
                        reg["exhausted"] = True
                        if "notes" not in reg:
                            reg["notes"] = []
                        reg["notes"].append({
                            "text": f"[AUTO-EXHAUSTED] Orchestrator detected stuck loop. Region force-closed after {reg.get('visits', 0)} visits.",
                            "cycle": memory.get("total_cycles_all_runs", 0),
                            "timestamp": datetime.datetime.now().isoformat(),
                        })
                        log("WARN", f"Auto-exhausted region {region_key}")
                # Remove matching leads
                memory["best_leads"] = [
                    l for l in memory.get("best_leads", [])
                    if not (abs(l["ra"] - stuck_ra) < 1.5 and abs(l["dec"] - stuck_dec) < 1.5)
                ]

            # Pick a random fresh region for the nudge
            rand_ra = round(random.uniform(0, 360), 2)
            rand_dec = round(random.uniform(-30, 80), 2)
            regions_str = ", ".join(f"({r[0]},{r[1]})" for r in stuck_regions_to_exhaust)
            previous_results.append({
                "tool": "system",
                "result": {
                    "message": f"⚠ STUCK LOOP DETECTED: You keep returning to the same regions: {regions_str}. "
                               f"ALL have been AUTO-EXHAUSTED. You MUST move to a COMPLETELY NEW region. "
                               f"Try: TOOL: search_region(ra={rand_ra}, dec={rand_dec}, radius=0.05)"
                }
            })
            # Reset state
            current_region = None
            current_region_cycles = 0
            recent_regions.clear()
            save_memory(memory)

        summary = "; ".join(cycle_summary_parts)
        research_history.append({
            "cycle": cycle_num,
            "summary": summary,
            "timestamp": datetime.datetime.now().isoformat(),
        })
        print_cycle_summary(cycle_num, cycle_summary_parts)

        # --- Update & persist memory ---
        update_memory(memory, cycle_num, previous_results, visited_regions)
        if cycle_num % 3 == 0 or cycle_num < 5:
            # Save every 3 cycles (or always for the first 5 to catch early crashes)
            save_memory(memory)

        cycle_num += 1
    
    # --- Save memory one last time ---
    save_memory(memory)
    _write_dashboard_status(running=False, cycle=cycle_num, phase="finished")
    log("INFO", f"Memory saved: {len(memory['regions'])} regions, {memory['total_cycles_all_runs']} total cycles")

    # Final summary
    n_findings = 0
    if os.path.exists(FINDINGS_FILE):
        with open(FINDINGS_FILE) as f:
            n_findings = max(0, len(f.readlines()) - 1)

    n_regions = len(memory.get("regions", {}))
    n_runs = memory.get("run_count", 0)

    print(f"""
{C.BANNER}{C.BOLD}╔══════════════════════════════════════════════════════════╗
║  🏁  Research Complete                                   ║
╠══════════════════════════════════════════════════════════╣{C.RESET}
{C.BANNER}║{C.RESET}  Cycles     : {C.BOLD}{cycle_num}{C.RESET}
{C.BANNER}║{C.RESET}  Findings   : {C.BOLD}{C.FIND}{n_findings}{C.RESET}
{C.BANNER}║{C.RESET}  Memory     : {C.BOLD}{n_regions}{C.RESET} regions │ {C.BOLD}{n_runs}{C.RESET} run(s)
{C.BANNER}║{C.RESET}  Log        : {C.DIM}{RESEARCH_LOG}{C.RESET}
{C.BANNER}{C.BOLD}╚══════════════════════════════════════════════════════════╝{C.RESET}
""")


if __name__ == "__main__":
    while True:
        try:
            main()
            break  # Normal exit
        except KeyboardInterrupt:
            log("INFO", "Interrupted. Research log saved.")
            sys.exit(0)
        except Exception as e:
            log("ERROR", f"ORCHESTRATOR CRASHED: {type(e).__name__}: {e}")
            log("ERROR", "Restarting in 10 seconds...")
            import traceback
            traceback.print_exc()
            time.sleep(10)
            log("INFO", "Restarting orchestrator...")
            # Loop continues — main() will be called again
