"""
Shared configuration — paths, directories, and constants used across all modules.
"""

import os
import sys

# Force UTF-8 output on Windows (for emoji & ANSI box-drawing)
if sys.platform == "win32":
    os.system("")   # Enable ANSI escape sequences on Windows 10+
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
IMAGES_DIR = os.path.join(DATA_DIR, "images")
PNG_DIR = os.path.join(DATA_DIR, "png")
FINDINGS_DIR = os.path.join(BASE_DIR, "findings")
FINDINGS_FILE = os.path.join(BASE_DIR, "findings.tsv")
RESEARCH_LOG = os.path.join(BASE_DIR, "research.log")
DASHBOARD_STATUS = os.path.join(BASE_DIR, "dashboard_status.json")
MEMORY_FILE = os.path.join(BASE_DIR, "memory.json")

# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

OLLAMA_URL = "http://localhost:11434/api/chat"

# ---------------------------------------------------------------------------
# Ensure required directories exist
# ---------------------------------------------------------------------------

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(IMAGES_DIR, exist_ok=True)
os.makedirs(PNG_DIR, exist_ok=True)
os.makedirs(FINDINGS_DIR, exist_ok=True)
