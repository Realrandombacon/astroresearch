"""
Terminal UI — ANSI colors, tool styling, logging, banners, and cycle summaries.
"""

import json
import datetime

from config import RESEARCH_LOG


# ---------------------------------------------------------------------------
# ANSI color codes
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
    ZTF         = "\033[38;5;69m"   # dark blue        — transient checks
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


# ---------------------------------------------------------------------------
# Tool styling — map tool names to (color, emoji)
# ---------------------------------------------------------------------------

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
    "check_transients":(C.ZTF,      "🔎"),
    "measure_photometry":(C.DETECT, "📐"),
    # Radio astronomy tools
    "download_radio_spectrum": ("\033[38;5;208m", "📡"),  # orange
    "analyze_spectrum":        ("\033[38;5;209m", "📊"),  # coral
    "check_rfi":               ("\033[38;5;196m", "🚫"),  # red (RFI = bad)
    "check_pulsar_catalog":    ("\033[38;5;220m", "💫"),  # gold
    "check_frb_catalog":       ("\033[38;5;199m", "⚡"),  # hot pink
}


def _tool_color(tool_name):
    """Get (color, emoji) for a tool, with fallback."""
    return TOOL_STYLE.get(tool_name, ("\033[96m", "🔧"))


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(level, message, **extra):
    """Write a log entry to file and console with rich colors."""
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    ts_str = f"\033[38;5;114m{timestamp}{C.RESET}"

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


# ---------------------------------------------------------------------------
# Banners & summaries
# ---------------------------------------------------------------------------

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
            "download_radio_spectrum": "download_radio_spectrum",
            "analyze_spectrum": "analyze_spectrum", "check_rfi": "check_rfi",
            "check_pulsar_catalog": "check_pulsar_catalog", "check_frb_catalog": "check_frb_catalog",
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
