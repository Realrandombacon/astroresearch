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

# Shared configuration (paths, dirs, UTF-8 setup)
from config import (
    BASE_DIR, DATA_DIR, IMAGES_DIR, PNG_DIR,
    FINDINGS_DIR, FINDINGS_FILE, RESEARCH_LOG,
    DASHBOARD_STATUS, MEMORY_FILE, OLLAMA_URL,
)

# Persistent memory extracted to agent/memory.py
from agent.memory import (
    load_memory, save_memory, update_memory, summarize_memory,
    query_memory as _query_memory,
    list_findings as _list_findings,
    list_unexplored as _list_unexplored,
    my_stats as _my_stats,
    dismiss_lead as _dismiss_lead,
    add_note as _add_note,
    mark_exhausted as _mark_exhausted,
)


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
    try:
        return (round(float(ra), precision), round(float(dec), precision))
    except (ValueError, TypeError):
        return (0.0, 0.0)

def _tool_cache_key(tool_name, params):
    """Create a hashable key for a tool call based on name + numeric params."""
    def _safe_float(v):
        try:
            return round(float(v), 2) if v is not None else None
        except (ValueError, TypeError):
            return None
    ra = params.get("ra", None)
    dec = params.get("dec", None)
    radius = params.get("radius", params.get("size", None))
    return (tool_name, _safe_float(ra), _safe_float(dec), _safe_float(radius))

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

# Terminal UI (extracted to agent/ui.py)
from agent.ui import C, TOOL_STYLE, _tool_color, log, print_banner, print_cycle_header, print_cycle_summary


# ---------------------------------------------------------------------------
# Ollama API
# ---------------------------------------------------------------------------

def call_ollama(model_name, system_prompt, user_prompt, temperature=0.3, images=None):
    """Call Ollama with think:false for direct structured output.

    If images is provided (list of base64 strings), they are included in
    the user message so Qwen can see them inline with full research context.
    """
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


# ---------------------------------------------------------------------------
# Pre-execution validation — just-in-time reminders for Qwen
# ---------------------------------------------------------------------------

# Required parameters per tool (checked BEFORE spawning subprocess)
_REQUIRED_PARAMS = {
    "measure_photometry": ["image", "ra", "dec"],
    "compare_images": ["img1", "img2"],
    "detect_sources": ["image"],
    "analyze_image": ["image"],
    "download_cutout": ["ra", "dec"],
    "download_multiepoch": ["ra", "dec"],
    "download_legacy": ["ra", "dec"],
    "search_region": ["ra", "dec"],
    "simbad_check": ["ra", "dec"],
    "query_gaia": ["ra", "dec"],
    "check_transients": ["ra", "dec"],
    "log_finding": ["ra", "dec", "description"],
    "mark_exhausted": ["ra", "dec"],
    "dismiss_lead": ["ra", "dec"],
    "add_note": ["ra", "dec", "note"],
    "query_memory": ["ra", "dec"],
}

# Tools that take file path parameters
_FILE_PARAMS = {
    "measure_photometry": ["image"],
    "compare_images": ["img1", "img2"],
    "detect_sources": ["image"],
    "analyze_image": ["image"],
}

# Investigation tools that count toward mark_exhausted readiness
_INVESTIGATION_TOOLS = {
    "download_multiepoch", "compare_images", "measure_photometry",
    "query_gaia", "simbad_check", "check_transients",
}


def _list_available_images():
    """List real image files in data/images/ for filename suggestions."""
    img_dir = os.path.join("data", "images")
    if not os.path.isdir(img_dir):
        return []
    return [f for f in os.listdir(img_dir)
            if f.endswith((".fits", ".jpg", ".jpeg", ".png"))]


def _suggest_files(requested, available, limit=5):
    """Find files similar to the requested name."""
    if not available:
        return []
    req_lower = requested.lower()
    # Extract RA/Dec/band fragments from requested name
    scored = []
    for f in available:
        f_lower = f.lower()
        score = 0
        # Band match
        for band in ["_g_", "_r_", "_i_", "_z_", "_y_"]:
            if band in req_lower and band in f_lower:
                score += 3
        # RA fragment match (first 5 chars of coordinate)
        import re
        ra_match = re.search(r"(\d{2,3}\.\d{1,4})", req_lower)
        if ra_match and ra_match.group(1)[:5] in f_lower:
            score += 5
        # Epoch match
        for ep in ["_ep1", "_ep2", "_ep3", "_ep4", "_ep5"]:
            if ep in req_lower and ep in f_lower:
                score += 2
        # Type match (warp vs cutout vs legacy)
        for prefix in ["warp_", "cutout_", "legacy_"]:
            if prefix in req_lower and prefix in f_lower:
                score += 2
        if score > 0:
            scored.append((score, f))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [f for _, f in scored[:limit]]


def _extract_band(filename):
    """Extract photometric band letter from a filename."""
    import re
    m = re.search(r"_([grizy])_", filename.lower())
    return m.group(1) if m else None


def validate_tool_call(tool_name, params, memory=None):
    """Pre-execution validation. Returns None if OK, or an error dict to skip execution.

    This catches common Qwen mistakes BEFORE spawning a subprocess,
    returning targeted guidance so Qwen self-corrects on the next turn.
    """

    # --- Rule 1: Missing required parameters ---
    required = _REQUIRED_PARAMS.get(tool_name)
    if required:
        missing = [p for p in required if p not in params or params[p] is None or str(params[p]).strip() == ""]
        if missing:
            hints = {
                "image": "Use a filename from your previous download results, or call list_images() first.",
                "img1": "Use exact filenames from download_multiepoch results or list_images().",
                "img2": "Use exact filenames from download_multiepoch results or list_images().",
                "ra": "Provide the RA in decimal degrees (e.g., ra=186.5).",
                "dec": "Provide the Dec in decimal degrees (e.g., dec=-8.5).",
                "description": "Write a detailed description with magnitudes, SNR, MJD dates, and catalogs checked.",
                "note": "Write what you observed or concluded about this region.",
            }
            hint_parts = [f"  - {p}: {hints.get(p, 'required')}" for p in missing]
            return {
                "error": "MISSING_PARAM",
                "tool": tool_name,
                "missing": missing,
                "hint": f"{tool_name} requires these parameters:\n" + "\n".join(hint_parts),
            }

    # --- Rule 2: Cross-band guard for compare_images (check BEFORE file existence) ---
    if tool_name == "compare_images":
        img1 = str(params.get("img1", ""))
        img2 = str(params.get("img2", ""))
        band1 = _extract_band(img1)
        band2 = _extract_band(img2)
        if band1 and band2 and band1 != band2:
            return {
                "error": "CROSS_BAND_BLOCKED",
                "img1_band": band1,
                "img2_band": band2,
                "hint": (
                    f"BLOCKED: You are comparing {band1}-band vs {band2}-band. "
                    f"Cross-band comparison shows stellar COLORS, not transients! "
                    f"Compare SAME band, different epochs: e.g., warp_..._g_ep1 vs warp_..._g_ep3."
                ),
            }

    # --- Rule 3: Filename validation (catch fabricated/placeholder names) ---
    file_keys = _FILE_PARAMS.get(tool_name, [])
    for key in file_keys:
        filepath = params.get(key, "")
        if not filepath:
            continue
        fname = os.path.basename(str(filepath))

        # Check for placeholder patterns
        if "XXXXX" in fname or "xxxxx" in fname:
            available = _list_available_images()
            suggestions = _suggest_files(fname, available)
            return {
                "error": "FABRICATED_FILENAME",
                "requested": fname,
                "hint": (
                    f"The filename '{fname}' contains placeholder values (XXXXX). "
                    f"Use EXACT filenames from your previous download results or call list_images()."
                ),
                "similar_files": suggestions if suggestions else "Call list_images() to see available files.",
            }

        # Check if file exists (only for .fits files we can verify)
        if fname.endswith(".fits"):
            full_path = os.path.join("data", "images", fname)
            if not os.path.exists(full_path):
                # Also try the path as given
                if not os.path.exists(str(filepath)):
                    available = _list_available_images()
                    suggestions = _suggest_files(fname, available)
                    return {
                        "error": "FILE_NOT_FOUND",
                        "requested": fname,
                        "hint": (
                            f"File '{fname}' does not exist in data/images/. "
                            f"Use exact filenames from your previous download results."
                        ),
                        "similar_files": suggestions if suggestions else "Call list_images() to see available files.",
                    }

    # --- Rule 4: mark_exhausted checklist ---
    if tool_name == "mark_exhausted" and memory:
        ra = params.get("ra")
        dec = params.get("dec")
        if ra is not None and dec is not None:
            try:
                key = _region_key(ra, dec)
            except (ValueError, TypeError):
                return None  # let it fail naturally

            reg = memory.get("regions", {}).get(key)
            if reg:
                # Already exhausted → existing warning handles it
                if reg.get("exhausted"):
                    return {
                        "error": "ALREADY_EXHAUSTED",
                        "hint": (
                            f"STOP — region RA={ra}, Dec={dec} is ALREADY marked exhausted. "
                            f"Do NOT call mark_exhausted again. Move to a new region: call list_unexplored()."
                        ),
                    }

                # Check investigation completeness
                tools_done = set(reg.get("tools_used", []))
                tools_done_investigation = tools_done & _INVESTIGATION_TOOLS
                tools_missing = _INVESTIGATION_TOOLS - tools_done
                if len(tools_done_investigation) < 3:
                    return {
                        "error": "INCOMPLETE_INVESTIGATION",
                        "hint": (
                            f"Before marking exhausted, complete your investigation. "
                            f"You used: {', '.join(sorted(tools_done_investigation)) or 'none of the key tools'}. "
                            f"Still needed: {', '.join(sorted(tools_missing))}. "
                            f"Run at least 3 of: download_multiepoch, compare_images, measure_photometry, "
                            f"query_gaia, simbad_check, check_transients."
                        ),
                        "tools_used": sorted(tools_done_investigation),
                        "tools_missing": sorted(tools_missing),
                    }

    return None  # All checks passed — proceed with execution


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
        params.setdefault("size", 2.0)
    elif tool_name == "download_legacy":
        params.setdefault("bands", "grz")
        params.setdefault("size", 256)
    elif tool_name == "download_cutout":
        params.setdefault("size", 2.0)
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
    """Log a potential astronomical finding with quality checks."""

    # ── Quality gate: reject low-quality findings with explanation ──
    desc_lower = description.lower()
    reasons = []  # collect ALL rejection reasons to give Qwen full feedback

    # 1. Description too short — no scientific value
    if len(description) < 50:
        reasons.append(
            "DESCRIPTION TOO SHORT: Your finding description must include "
            "magnitude values, SNR, MJD epochs, and which catalogs were checked. "
            f"You wrote only {len(description)} chars — minimum is 50."
        )

    # 2. Extract SNR from description to reject noise detections
    import re
    snr_matches = re.findall(r'SNR\s*[=:]\s*(\d+(?:\.\d+)?)', description, re.IGNORECASE)
    if snr_matches:
        max_snr = max(float(s) for s in snr_matches)
        if max_snr < 3.0 and significance == "high":
            reasons.append(
                f"SNR TOO LOW FOR HIGH SIGNIFICANCE: You reported SNR={max_snr:.1f} "
                f"but marked significance='high'. A detection with SNR < 3 is NOT "
                f"statistically significant — it's indistinguishable from noise. "
                f"Either downgrade to significance='low' or investigate further to "
                f"get a stronger detection."
            )

    # 3. Extract Δmag to reject photometric noise
    dmag_matches = re.findall(
        r'[Δδ]?\s*mag\s*[=:]\s*[+-]?(0\.\d+)|'
        r'delta\s*[=:]?\s*[+-]?(0\.\d+)\s*mag|'
        r'(\d+\.\d+)\s*(?:mag|→)\s*(\d+\.\d+)',
        description, re.IGNORECASE
    )
    if dmag_matches:
        # Try to find explicit Δmag values
        for m in dmag_matches:
            if m[0]:  # Δmag=0.xx pattern
                dmag = float(m[0])
                if dmag < 0.3 and significance in ("high", "medium"):
                    reasons.append(
                        f"MAGNITUDE CHANGE TOO SMALL: Δmag={dmag:.2f} is within "
                        f"Pan-STARRS photometric uncertainty (~0.1-0.2 mag for faint "
                        f"sources). A real transient should show Δmag ≥ 0.3. "
                        f"This is likely measurement noise, not a real event."
                    )
                    break
            elif m[1]:  # delta=0.xx mag pattern
                dmag = float(m[1])
                if dmag < 0.3 and significance in ("high", "medium"):
                    reasons.append(
                        f"MAGNITUDE CHANGE TOO SMALL: Δmag={dmag:.2f} is within "
                        f"photometric noise. Real transients show Δmag ≥ 0.3."
                    )
                    break

    # 4. Duplicate check — same coords within 1° already logged
    if os.path.exists(FINDINGS_FILE):
        try:
            with open(FINDINGS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.strip().split("\t")
                    if len(parts) >= 4 and parts[0] != "id":
                        try:
                            ex_ra, ex_dec = float(parts[2]), float(parts[3])
                            if abs(ex_ra - float(ra)) < 1.0 and abs(ex_dec - float(dec)) < 1.0:
                                reasons.append(
                                    f"DUPLICATE: A finding already exists near RA={ex_ra}, "
                                    f"Dec={ex_dec} (finding {parts[0]}). Do NOT log the same "
                                    f"source twice. If you have NEW information about it, "
                                    f"use add_note() instead."
                                )
                                break
                        except (ValueError, TypeError):
                            continue
        except Exception:
            pass

    # ── If any quality issues found, REJECT with full explanation ──
    if reasons:
        log("WARN", f"Finding REJECTED at RA={ra}, Dec={dec}: {reasons[0][:80]}")
        return {
            "status": "rejected",
            "REJECTED": True,
            "reasons": reasons,
            "explanation": (
                "Your finding was REJECTED because it did not pass quality checks. "
                "Review the reasons above and either: "
                "(1) fix the issue and call log_finding again with better data, or "
                "(2) continue investigating with more photometry/validation before logging."
            ),
        }

    # ── Quality OK — log the finding ──
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    finding_id = f"F{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"

    finding_path = os.path.join(FINDINGS_DIR, f"{finding_id}.json")

    # Append to findings.tsv (ensure header exists)
    needs_header = not os.path.exists(FINDINGS_FILE)
    if not needs_header:
        try:
            with open(FINDINGS_FILE, "r", encoding="utf-8") as f:
                first_line = f.readline().strip()
                if not first_line or not first_line.startswith("id"):
                    needs_header = True
        except Exception:
            needs_header = True
    if needs_header:
        with open(FINDINGS_FILE, "w", encoding="utf-8") as f:
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


# Response parsing (extracted to agent/parsing.py)
from agent.parsing import deduplicate_response, parse_tool_calls

# Prompt construction (extracted to agent/prompts.py)
from agent.prompts import build_system_prompt as _build_system_prompt_base
from agent.prompts import build_reflection_prompt, build_user_prompt

def build_system_prompt():
    """Wrapper that passes AVAILABLE_TOOLS to the extracted prompt builder."""
    return _build_system_prompt_base(AVAILABLE_TOOLS)


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
        MAX_TURNS_PER_CYCLE = 10
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

                    # VISIT CEILING — DISABLED
                    # Qwen manages region transitions autonomously via
                    # mark_exhausted / list_unexplored. No need to force-close.

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

                # --- Pre-execution validation (just-in-time reminders) ---
                validation = validate_tool_call(tool_name, params, memory=memory)
                if validation is not None:
                    log("WARN", f"PRE-CHECK: {tool_name} blocked — {validation.get('error', '?')}")
                    result = validation
                    turn_results.append({"tool": tool_name, "params": params, "result": result})
                    cycle_summary_parts.append(f"{tool_name}: BLOCKED ({validation.get('error', '?')})")
                    continue

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
        # STUCK LOOP DETECTION — DISABLED
        # Previously auto-exhausted regions after N consecutive tool calls.
        # Removed because: (1) counter was per-tool-call not per-cycle,
        # so threshold of 25 triggered after only ~5 real cycles;
        # (2) Qwen's improved pipeline (download→detect→compare→photometry→
        # validate) legitimately needs many cycles on one region;
        # (3) Qwen now handles region transitions on its own via
        # mark_exhausted / list_unexplored.
        # If stuck loops reappear, re-enable with a TRUE cycle counter.

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
