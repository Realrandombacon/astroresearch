# Radio Astronomy Tools — Implementation Plan

## Goal

Add radio astronomy capabilities to AstroResearch (Option A: same agent, new tools).
If Qwen 4B struggles with 30+ tools, we fall back to Option B (separate RadioResearch project).

---

## New Tools (5)

### 1. `download_radio_spectrum`
- **Source**: VLA Sky Survey (VLASS) Quick Look images via CIRADA cutout service
- **Fallback**: LOFAR VO interface, FIRST survey cutouts
- **Input**: `ra`, `dec`, `radius` (arcmin), `survey` (vlass/first/lofar)
- **Output**: FITS cutout saved to `data/images/radio_<ra>_<dec>_<survey>.fits`
- **Script**: `tools/radio_query.py download-radio --ra {ra} --dec {dec} --radius {radius} --survey {survey}`
- **Timeout**: 90s
- **API**: `https://ws-cadc.canfar.net/caom2ops/cutout` (VLASS), `https://third.ucllnl.org/cgi-bin/firstcutout` (FIRST)

### 2. `analyze_spectrum`
- **Purpose**: Analyze radio FITS data — measure peak flux, integrated flux, RMS noise, spectral index if multi-freq
- **Input**: `image` (path to radio FITS)
- **Output**: `{ peak_flux_mJy, integrated_flux_mJy, rms_noise, snr, beam_size, frequency_MHz, sources: [...] }`
- **Script**: `tools/radio_analysis.py analyze-spectrum --image {image}`
- **Implementation**: Use astropy.io.fits + simple peak detection (similar to detect_sources but for radio)

### 3. `check_rfi`
- **Purpose**: Flag probable RFI (Radio Frequency Interference) in a radio observation
- **Input**: `image` (path to radio FITS)
- **Output**: `{ is_rfi: bool, rfi_fraction: float, rfi_type: str, confidence: float, details: str }`
- **Script**: `tools/radio_analysis.py check-rfi --image {image}`
- **Checks**:
  - Narrow-band spikes (bandwidth < 1 kHz → likely RFI)
  - Constant-amplitude stripes (terrestrial broadcast pattern)
  - Known RFI frequency ranges (GPS L1/L2, WiFi 2.4/5GHz, satellite bands)
  - Spatial pattern: point source (astrophysical) vs stripe/ring (RFI)

### 4. `check_pulsar_catalog`
- **Purpose**: Cross-check coordinates against ATNF Pulsar Catalogue
- **Input**: `ra`, `dec`, `radius` (arcsec)
- **Output**: `{ n_matches, pulsars: [{ name, period, dm, distance, ra, dec, separation }] }`
- **Script**: `tools/radio_query.py check-pulsar --ra {ra} --dec {dec} --radius {radius}`
- **API**: `https://www.atnf.csiro.au/research/pulsar/psrcat/proc_form.php` (VO query)

### 5. `check_frb_catalog`
- **Purpose**: Cross-check coordinates against known FRBs (Transient Name Server + FRBcat)
- **Input**: `ra`, `dec`, `radius` (arcsec)
- **Output**: `{ n_matches, frbs: [{ name, dm, redshift, ra, dec, separation, repeater }] }`
- **Script**: `tools/radio_query.py check-frb --ra {ra} --dec {dec} --radius {radius}`
- **API**: `https://www.wis-tns.org/api/get/search` (TNS), `https://frbcat.org/api/` (FRBcat)

---

## Files to Create

| File | Content |
|------|---------|
| `tools/radio_query.py` | `download-radio`, `check-pulsar`, `check-frb` CLI commands |
| `tools/radio_analysis.py` | `analyze-spectrum`, `check-rfi` CLI commands |

## Files to Modify

| File | Changes |
|------|---------|
| `orchestrator.py` | Add 5 tools to `AVAILABLE_TOOLS`, add to `_REQUIRED_PARAMS`, `_FILE_PARAMS`, `TOOL_TIMEOUTS`, add defaults |
| `agent/ui.py` | Add 5 entries to `TOOL_STYLE` with 📡 emoji and new color |
| `agent/prompts.py` | Add radio workflow section to system prompt |
| `requirements.txt` | Add `astroquery` if not present (for VO queries) |

---

## Integration Points

### AVAILABLE_TOOLS entries

```python
"download_radio_spectrum": {
    "description": "Download radio survey cutout (VLASS, FIRST, or LOFAR). Use for radio-wavelength analysis of a region.",
    "usage": "download_radio_spectrum(ra=<degrees>, dec=<degrees>, radius=5, survey='vlass')",
    "script": "tools/radio_query.py download-radio --ra {ra} --dec {dec} --radius {radius} --survey {survey}",
},
"analyze_spectrum": {
    "description": "Analyze a radio FITS image — measure peak flux, integrated flux, RMS noise, SNR, and detect radio sources.",
    "usage": "analyze_spectrum(image=<path>)",
    "script": "tools/radio_analysis.py analyze-spectrum --image {image}",
},
"check_rfi": {
    "description": "Check a radio observation for RFI (Radio Frequency Interference). Run this BEFORE logging any radio finding.",
    "usage": "check_rfi(image=<path>)",
    "script": "tools/radio_analysis.py check-rfi --image {image}",
},
"check_pulsar_catalog": {
    "description": "Cross-check coordinates against the ATNF Pulsar Catalogue. Use to verify if a radio source is a known pulsar.",
    "usage": "check_pulsar_catalog(ra=<degrees>, dec=<degrees>, radius=60)",
    "script": "tools/radio_query.py check-pulsar --ra {ra} --dec {dec} --radius {radius}",
},
"check_frb_catalog": {
    "description": "Cross-check coordinates against known Fast Radio Bursts (TNS + FRBcat). Use to verify if a radio transient is a known FRB.",
    "usage": "check_frb_catalog(ra=<degrees>, dec=<degrees>, radius=60)",
    "script": "tools/radio_query.py check-frb --ra {ra} --dec {dec} --radius {radius}",
},
```

### TOOL_STYLE entries

```python
"download_radio_spectrum": ("\033[38;5;208m", "📡"),  # orange
"analyze_spectrum":        ("\033[38;5;209m", "📊"),  # coral
"check_rfi":               ("\033[38;5;196m", "🚫"),  # red (RFI = bad)
"check_pulsar_catalog":    ("\033[38;5;220m", "💫"),  # gold
"check_frb_catalog":       ("\033[38;5;199m", "⚡"),  # hot pink
```

### Validation rules

```python
# _REQUIRED_PARAMS
"download_radio_spectrum": ("ra", "dec"),
"analyze_spectrum": ("image",),
"check_rfi": ("image",),
"check_pulsar_catalog": ("ra", "dec"),
"check_frb_catalog": ("ra", "dec"),

# _FILE_PARAMS
"analyze_spectrum": ("image",),
"check_rfi": ("image",),

# TOOL_TIMEOUTS
"download_radio_spectrum": 90,
"analyze_spectrum": 60,
"check_rfi": 30,
"check_pulsar_catalog": 30,
"check_frb_catalog": 30,
```

### Defaults

```python
if tool_name == "download_radio_spectrum":
    params.setdefault("survey", "vlass")
    params.setdefault("radius", 5)
if tool_name in ("check_pulsar_catalog", "check_frb_catalog"):
    params.setdefault("radius", 60)
```

---

## System Prompt Addition

Add after the optical workflow section:

```
### Radio astronomy workflow (when investigating radio sources)
1. download_radio_spectrum — get VLASS/FIRST cutout at coordinates
2. check_rfi — ALWAYS check for RFI before analyzing
3. analyze_spectrum — measure flux, SNR, detect radio sources
4. check_pulsar_catalog — cross-check against known pulsars
5. check_frb_catalog — cross-check against known FRBs
6. If source is NOT in any radio catalog → measure_photometry on optical counterpart → log_finding

IMPORTANT: Radio RFI is extremely common. NEVER log a radio finding without running check_rfi first.
A radio source that IS in the pulsar catalog is NOT a discovery — dismiss it.
```

---

## Quality Gates (Radio-specific)

Add to `log_finding()` validation:
- Radio finding without `check_rfi` in recent tool history → warning (not block)
- Peak flux < 1 mJy with significance='high' → reject (below typical survey noise floor)

---

## Rollback Plan

If Qwen 4B performance degrades (hallucinated tool names, wrong tool selection rate > 20%):
1. Remove 5 radio tools from `AVAILABLE_TOOLS`
2. Remove from `TOOL_STYLE`, `_REQUIRED_PARAMS`, etc.
3. Keep `tools/radio_query.py` and `tools/radio_analysis.py` for future standalone project
4. Fork into separate `RadioResearch` project (Option B)

---

## Implementation Order

1. [x] Write this plan
2. [ ] Create `tools/radio_query.py` with download-radio, check-pulsar, check-frb
3. [ ] Create `tools/radio_analysis.py` with analyze-spectrum, check-rfi
4. [ ] Register tools in orchestrator.py (AVAILABLE_TOOLS, validation, timeouts, defaults)
5. [ ] Add TOOL_STYLE entries in agent/ui.py
6. [ ] Update system prompt in agent/prompts.py
7. [ ] Test each tool standalone: `python tools/radio_query.py download-radio --ra 150 --dec 30`
8. [ ] Run 10-cycle test with Qwen to verify tool selection quality
9. [ ] Monitor for tool confusion / hallucination regression
