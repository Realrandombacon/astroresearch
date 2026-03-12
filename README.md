# AstroResearch

> ⚠️ **Work in progress** — this project is under active development. Features, tools, and the agent's workflow may change significantly.

Autonomous astronomical anomaly detection powered by a local LLM agent.

Inspired by Andrej Karpathy's [autoresearch](https://github.com/karpathy/autoresearch) — which demonstrated that LLM agents can autonomously conduct research — AstroResearch applies the same philosophy to observational astronomy. Instead of papers and code, this agent explores the real sky.

AstroResearch uses **Qwen 3.5 (4B)** running on **Ollama** to autonomously explore the sky, download real multi-epoch survey data, analyze images, and flag potential discoveries — all without human intervention.

## How it works

```
┌─────────────────────────────────────────────────────┐
│                   Orchestrator                      │
│                                                     │
│   THOUGHT → TOOL call → result → THOUGHT → ...     │
│                                                     │
│   Quality gates · Crash recovery · Memory            │
└─────────┬───────────────────────────┬───────────────┘
          │                           │
    ┌─────▼─────┐             ┌──────▼──────┐
    │  Qwen 4B  │             │  25 tools   │
    │  (Ollama) │             │  (Python)   │
    └───────────┘             └─────────────┘
```

1. The LLM picks a sky region or follows up on a previous finding
2. It downloads **multi-epoch** data from Pan-STARRS warps and DESI Legacy Survey
3. It compares **same-band, different-epoch** images to detect real temporal changes
4. Cross-references with SIMBAD, ZTF, MAST, **Gaia DR3**, and **ALeRCE** catalogs
5. Validates candidates with **aperture photometry** and known-transient checks before logging
6. Interesting findings are logged with coordinates, magnitudes, and cross-references
7. A persistent **memory system** tracks every region visited, preventing redundant work

The agent runs continuously with no human input. It builds context across cycles, revisits promising regions, and self-corrects when errors occur.

## Key features

- **Multi-epoch temporal analysis** — downloads individual Pan-STARRS warp exposures spanning years, enabling real transient and variability detection
- **Cross-survey verification** — compares Pan-STARRS data against DESI Legacy Survey (independent survey, ~5yr baseline) to cross-check candidates
- **Cross-band guard** — automatically warns when the agent tries to compare different photometric bands (g vs r), which reveals stellar colors not transients
- **NaN edge artifact filter** — dilated NaN mask rejects false detections near Pan-STARRS tile boundaries in both source detection and epoch comparison
- **Quality gates on findings** — rejects low-quality discoveries with explanatory feedback (SNR < 3, Δmag < 0.3, missing metadata, duplicates) so the agent learns and retries
- **Persistent memory** — tracks every region explored, tools used, and outcomes across the entire session
- **Crash recovery** — automatic restart on failure with context preservation
- **Live dashboard** — Flask web UI showing real-time progress, sky coverage, and findings

## Requirements

- **Python 3.10+**
- **Ollama** running locally with a Qwen model pulled
- **GPU recommended** (tested on RTX 3070/4060 with Qwen 3.5 4B)
- Internet connection (for astronomical archive queries)

## Setup

```bash
# 1. Install Ollama (https://ollama.com)
# 2. Pull a model
ollama pull qwen3:4b

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Run
python orchestrator.py --max-cycles 50
```

### Command-line options

```
--target "Crab Nebula"    Start with a specific target
--model qwen3.5:4b        Choose Ollama model (default: qwen3.5:4b)
--max-cycles 0            Run indefinitely (0 = no limit)
--max-cycles 50           Stop after 50 cycles
```

### Live dashboard

```bash
python dashboard.py
# Open http://localhost:5000 in your browser
```

The dashboard shows sky coverage maps, finding statistics, tool usage breakdown, and real-time agent status.

## Project structure

```
autoresearch/
├── orchestrator.py          # Main agent loop, LLM integration, memory system
├── dashboard.py             # Flask web dashboard for live monitoring
├── tools/
│   ├── astro_query.py       # SIMBAD, ZTF, MAST, Pan-STARRS, Legacy Survey queries
│   └── image_analysis.py    # FITS/image reading, comparison, source detection
├── start_ollama.bat         # GPU launcher script (Windows)
├── start_ollama.ps1         # GPU launcher script (PowerShell)
├── findings/                # JSON files for each discovery (gitignored)
├── findings.tsv             # Tabular log of all findings (gitignored)
├── memory.json              # Agent persistent memory (gitignored)
├── data/                    # Downloaded images and cached queries (gitignored)
├── requirements.txt
├── LICENSE
└── README.md
```

## Tools available to the agent

### Data acquisition

| Tool | Description |
|------|-------------|
| `search_region` | Search MAST archives (JWST, Hubble) for observations at coordinates |
| `search_target` | Search MAST archives by target name |
| `multi_epoch` | Find multi-epoch observations of the same region |
| `ztf_lightcurve` | Retrieve ZTF light curves for variability analysis |
| `simbad_check` | Check SIMBAD catalog for known objects |
| `download_cutout` | Download Pan-STARRS stacked image cutouts (g/r/i/z/y bands) |
| `download_multiepoch` | Download Pan-STARRS warp exposures from different dates (real temporal data) |
| `download_legacy` | Download DESI Legacy Survey DR10 cutouts (independent survey, ~5yr baseline) |
| `list_images` | List all downloaded images with coordinates and bands |

### Image analysis

| Tool | Description |
|------|-------------|
| `detect_sources` | Detect point sources in a FITS image |
| `compare_images` | Pixel-level comparison with cross-band guard and temporal detection |
| `analyze_image` | Visual inspection — image is shown directly to the LLM |
| `convert_to_png` | Convert FITS/JPEG to PNG for inspection |
| `measure_photometry` | Calibrated aperture photometry — magnitude, flux, SNR at specific coordinates |

### Validation (verify candidates before logging)

| Tool | Description |
|------|-------------|
| `query_gaia` | Query Gaia DR3 for parallax, proper motion, and variability classification |
| `check_transients` | Check ALeRCE/ZTF broker for known transients with ML classification |

### Knowledge management

| Tool | Description |
|------|-------------|
| `log_finding` | Record a potential discovery with structured metadata |
| `query_memory` | Look up past exploration data for a region |
| `list_findings` | List all logged findings, filterable by significance |
| `list_unexplored` | Show unexplored sky regions and coverage gaps |
| `my_stats` | Global performance dashboard — findings, coverage, tool usage |
| `dismiss_lead` | Mark a priority lead as resolved (stop revisiting) |
| `add_note` | Write a note to future self about a region |
| `mark_exhausted` | Flag a region as fully analyzed (deprioritize) |

## How the agent thinks

The orchestrator uses a structured prompt that guides the LLM to:

- **Plan** before acting (THOUGHT blocks)
- **Use tools** by emitting `TOOL: function_name(params)` lines
- **Follow the correct workflow**: download multi-epoch → compare same-band different-epoch → cross-check with Legacy Survey → validate with Gaia/ALeRCE → measure photometry
- **Avoid false positives**: cross-band comparisons (g vs r) are flagged as color anomalies, not transients; Gaia checks filter known variables; ALeRCE checks filter already-reported transients
- **Manage its own memory**: dismiss resolved leads, add notes, mark regions exhausted
- **Self-correct** when files are missing or queries fail

## Technical details

- Communicates with Ollama via REST API (`/api/chat`)
- Tools are Python scripts called as subprocesses with JSON I/O
- Multi-epoch images from Pan-STARRS via `ps1filenames.py?type=warp` (individual exposures, not stacks)
- Cross-survey data from DESI Legacy Survey DR10 via `legacysurvey.org` API
- Smart epoch selection: greedy max-spread algorithm picks maximally time-spread exposures
- WCS handling preserves raw FITS Header objects (not dicts) to support Pan-STARRS non-standard `PC001001` keywords
- NaN boundary dilation (3–4px) filters interpolation artifacts at warp tile edges
- Quality gates reject findings with SNR < 3, Δmag < 0.3, short descriptions, or duplicate coordinates
- Fuzzy file matching handles coordinate precision mismatches across tool calls
- Path normalization handles Windows/Linux differences
- Aggressive timeouts (15–120s) prevent stalling on slow APIs

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgments

This project is inspired by [autoresearch](https://github.com/karpathy/autoresearch) by Andrej Karpathy, which showed that LLM agents can autonomously drive the research cycle. AstroResearch adapts that idea to observational astronomy — swapping literature search for sky surveys, and paper analysis for multi-epoch image comparison.

## Contributing

Issues, ideas, and PRs are welcome.

If you adapt this framework for another scientific domain, I'd love to hear about it.
