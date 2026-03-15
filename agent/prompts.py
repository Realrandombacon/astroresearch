"""
Prompt construction — system prompt, reflection prompt, and user prompt
for the Qwen research agent.
"""

import json


def build_system_prompt(available_tools):
    """Build the system prompt with available tools.

    Args:
        available_tools: Dict of tool definitions {name: {description, usage, ...}}
    """
    tools_desc = "\n".join(
        f"  - {name}: {info['description']}\n    Usage: {info['usage']}"
        for name, info in available_tools.items()
    )

    return f"""You are an automated sky survey agent running 24/7 on real astronomical data.
Your goal: find GENUINELY NOVEL transients or anomalies that are NOT in any existing catalog.

Work methodically and carefully. Quality over quantity — one real discovery is worth more
than 100 false positives. Every claim you make must be backed by measured numbers from
your tools. If the evidence is ambiguous, note it and move on rather than overstating.

A source in Gaia, SIMBAD, or ALeRCE is ALREADY CATALOGED — it is not novel, even if
it lacks a variable_class label or class_name is null. Most Gaia sources have no
variability classification yet, and many ALeRCE detections have no ML classification.
"Unclassified" does NOT mean "novel" — it means the survey detected it but hasn't
labeled it yet. Only flag something as "novel" if it appears in NONE of these catalogs.
A magnitude change < 0.1 mag is photometric noise — never log it as a finding.

Available tools:
{tools_desc}

Your approach — systematic investigation:
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

### Radio astronomy workflow (when investigating radio sources):
  1. download_radio_spectrum(ra=..., dec=..., survey='vlass') — get a radio survey cutout
  2. check_rfi(image='data/images/radio_...fits') — ALWAYS check for RFI before analyzing!
  3. analyze_spectrum(image='data/images/radio_...fits') — measure flux, SNR, detect radio sources
  4. check_pulsar_catalog(ra=..., dec=..., radius=60) — is it a known pulsar?
  5. check_frb_catalog(ra=..., dec=..., radius=60) — is it a known FRB?
  6. If NOT in any catalog → measure_photometry on optical counterpart → log_finding

  IMPORTANT: Radio RFI is extremely common. NEVER log a radio finding without running check_rfi first.
  A radio source that IS in the pulsar/FRB catalog is NOT a discovery — dismiss it.
  Available radio surveys: vlass (3GHz), first (1.4GHz, Dec>-10°), nvss (1.4GHz), lofar (150MHz).
  You can combine optical + radio analysis: a transient seen in BOTH is very strong evidence!

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
- search_memory(keyword='dwarf') — search ALL regions by keyword (notes, outcomes, reasons). Learn from past patterns!
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
                      next_seed_target=None, memory_summary=None,
                      session_tool_usage=None):
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

    # --- Unused tool hints: nudge Qwen toward tools it hasn't tried THIS cycle ---
    unused_hint = ""
    if session_tool_usage is not None and cycle_num > 0:
        # Tools worth hinting about (not internal/memory tools)
        HINTABLE_TOOLS = {
            "download_radio_spectrum": "download radio survey data (VLASS/FIRST/NVSS/LOFAR) to analyze regions at radio wavelengths — a transient seen in both optical AND radio is extremely strong evidence",
            "analyze_spectrum": "analyze radio FITS images for flux, SNR, and radio source detection",
            "check_rfi": "check radio observations for RFI contamination before trusting results",
            "check_pulsar_catalog": "cross-check coordinates against the ATNF Pulsar Catalogue",
            "check_frb_catalog": "cross-check coordinates against known Fast Radio Bursts (FRBCAT + CHIME)",
            "download_legacy": "download DESI Legacy Survey cutouts for independent cross-survey verification",
            "ztf_lightcurve": "retrieve ZTF light curves to check for known variability history",
            "my_stats": "see your global performance dashboard with strategic recommendations",
            "search_memory": "search all explored regions by keyword to learn from past patterns",
        }
        unused = {name: desc for name, desc in HINTABLE_TOOLS.items()
                  if name not in session_tool_usage}
        if unused:
            # Rotate which tool we hint about (1 per cycle, round-robin)
            unused_list = list(unused.items())
            pick = unused_list[cycle_num % len(unused_list)]
            unused_hint = f"\n💡 Did you know? You have access to **{pick[0]}** — {pick[1]}\n"

    return f"""Research Cycle {cycle_num}
{target_hint}
{memory_text}
{history_text}
{explored_text}
{ztf_text}
{results_text}
{unused_hint}
What would you like to investigate next? Remember to use THOUGHT: and TOOL: format.
"""
