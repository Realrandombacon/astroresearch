"""
Persistent memory — load, save, update, query, and manage the agent's
cross-run knowledge base (regions explored, findings, notes, sky coverage).
"""

import os
import json
import math
import random
import datetime

from config import MEMORY_FILE, FINDINGS_FILE


# ---------------------------------------------------------------------------
# Core memory: create / load / save / update
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
        try:
            ra_f, dec_f = float(ra), float(dec)
        except (ValueError, TypeError):
            continue

        region_key = f"{round(ra_f,1)},{round(dec_f,1)}"

        # ------ Update region entry ------
        if region_key not in mem["regions"]:
            mem["regions"][region_key] = {
                "ra": round(ra_f, 2),
                "dec": round(dec_f, 2),
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
                lead = {"ra": round(ra_f, 2), "dec": round(dec_f, 2),
                        "finding_id": fid, "why": desc, "cycle": cycle_num}
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
# Memory READ tools — called by the orchestrator on behalf of Qwen
# ---------------------------------------------------------------------------

def query_memory(memory, ra=0, dec=0, radius=5.0, **kwargs):
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
                    entry["notes"] = [n["text"] for n in reg["notes"][-5:]]
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


def list_findings(memory, significance="all", **kwargs):
    """List all logged findings from memory, optionally filtered by significance."""
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


def list_unexplored(memory, **kwargs):
    """Show unexplored sky regions and coverage gaps with suggested coordinates.

    Uses adaptive bin resolution: starts at 10° RA bins, switches to 2° when
    all coarse bins are covered. Always generates fresh random coordinates
    far from existing regions.
    """
    cov = memory.get("sky_coverage", {})
    ra_bins_coarse = set(cov.get("ra_bins_visited", []))
    regions = memory.get("regions", {})

    # Collect all explored coordinates for distance checks
    explored_coords = []
    for key, reg in regions.items():
        explored_coords.append((float(reg.get("ra", 0)), float(reg.get("dec", 0))))

    # --- Adaptive bin resolution ---
    all_coarse = set(range(0, 360, 10))
    coarse_missing = sorted(all_coarse - ra_bins_coarse)

    if coarse_missing:
        bin_size = 10
        missing_bins = coarse_missing
        coverage_label = f"{len(ra_bins_coarse)}/36 (10° bins)"
    else:
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
    def _min_distance(ra, dec):
        """Min angular distance (degrees) to any explored region."""
        if not explored_coords:
            return 999
        best = 999
        cos_dec = math.cos(math.radians(dec))
        for era, edec in explored_coords:
            dra = abs(ra - era)
            if dra > 180:
                dra = 360 - dra  # RA wrap-around fix
            dra *= cos_dec     # project onto sky after wrap correction
            ddec = abs(dec - edec)
            dist = math.sqrt(dra ** 2 + ddec ** 2)
            if dist < best:
                best = dist
        return best

    candidates = []
    for _ in range(200):
        rand_ra = round(random.uniform(0, 360), 4)
        rand_dec = round(random.uniform(-30, 70), 4)
        dist = _min_distance(rand_ra, rand_dec)
        candidates.append((rand_ra, rand_dec, dist))

    candidates.sort(key=lambda c: c[2], reverse=True)
    n_random = max(2, 5 - len(suggested_coords))
    for rand_ra, rand_dec, dist in candidates[:n_random]:
        suggested_coords.append({
            "ra": rand_ra,
            "dec": rand_dec,
            "reason": f"Random unexplored position ({dist:.1f}° from nearest explored region)",
        })

    n_explored = len(regions)
    sky_area_explored = n_explored * 0.0011  # ~2 arcmin cutout ≈ 0.0011 sq deg
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


def my_stats(memory, **kwargs):
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
    sampled_area = round(n_regions * 0.008, 2)

    # --- Finding rate ---
    finding_rate = round(total_findings / max(total_cycles, 1) * 100, 1)

    # --- Strategic recommendations ---
    recommendations = []
    if n_active > 0 and n_with_findings / max(n_regions, 1) < 0.05:
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


def search_memory(memory, keyword="", **kwargs):
    """Search all regions by keyword across notes, outcomes, reasons, and tools.

    Scans every region's notes, outcomes, exhaustion reasons, and tool lists
    for a case-insensitive keyword match. Returns matching regions with context
    so the agent can learn from past patterns.
    """
    keyword = str(keyword).strip().lower()
    if not keyword or len(keyword) < 2:
        return {"error": "Provide a keyword of at least 2 characters."}

    matches = []
    regions = memory.get("regions", {})

    for key, reg in regions.items():
        snippets = []

        # Search notes
        for note in reg.get("notes", []):
            text = note.get("text", "")
            if keyword in text.lower():
                snippets.append(f"note: {text[:120]}")

        # Search outcomes
        for outcome in reg.get("outcomes", []):
            if keyword in outcome.lower():
                snippets.append(f"outcome: {outcome[:120]}")

        # Search exhaustion reason
        reason = reg.get("exhaustion_reason", "")
        if keyword in reason.lower():
            snippets.append(f"exhausted: {reason[:120]}")

        # Search tools used
        for tool in reg.get("tools_used", []):
            if keyword in tool.lower():
                snippets.append(f"tool: {tool}")

        if snippets:
            matches.append({
                "ra": reg.get("ra", 0),
                "dec": reg.get("dec", 0),
                "visits": reg.get("visits", 1),
                "exhausted": reg.get("exhausted", False),
                "n_findings": len(reg.get("findings", [])),
                "matches": snippets[:5],  # Cap per region
            })

    # Sort by number of matches (most relevant first)
    matches.sort(key=lambda m: len(m["matches"]), reverse=True)

    return {
        "keyword": keyword,
        "n_regions_matched": len(matches),
        "results": matches[:15],  # Cap total to save tokens
        "tip": f"Found {len(matches)} regions mentioning '{keyword}'."
              if matches else f"No regions mention '{keyword}'. Try a different keyword.",
    }


# ---------------------------------------------------------------------------
# Memory WRITE tools — Qwen manages its own knowledge base
# ---------------------------------------------------------------------------

def dismiss_lead(memory, ra=0, dec=0, reason="", **kwargs):
    """Remove a lead from best_leads so Qwen stops revisiting it."""
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

    if removed == 0:
        return {
            "status": "no_leads",
            "leads_removed": 0,
            "reason": reason,
            "WARNING": f"No leads exist near RA={ra}, Dec={dec} — nothing to dismiss. "
                       f"If this region is done, use mark_exhausted() once, then list_unexplored() to move on.",
        }

    return {
        "status": "ok",
        "leads_removed": removed,
        "reason": reason,
        "message": f"Dismissed {removed} lead(s) near RA={ra}, Dec={dec}. "
                   f"Use list_unexplored() to pick a new region.",
    }


def add_note(memory, ra=0, dec=0, note="", **kwargs):
    """Write a persistent note on a region. Notes survive across runs."""
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


def mark_exhausted(memory, ra=0, dec=0, reason="", **kwargs):
    """Flag a region as exhausted — fully investigated, move on."""
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

    # If already exhausted, return a forceful STOP message
    if reg.get("exhausted"):
        return {
            "status": "already_exhausted",
            "region": region_key,
            "exhausted": True,
            "WARNING": (
                f"STOP — Region RA={ra}, Dec={dec} is ALREADY EXHAUSTED. "
                f"Do NOT call mark_exhausted again. "
                f"Use list_unexplored() NOW to pick a NEW region and start downloading data there."
            ),
        }

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
                   f"Use list_unexplored() NOW to pick a new region.",
    }
