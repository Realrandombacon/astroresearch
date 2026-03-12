"""
Shared utilities for astronomical tools.

- File format detection (magic bytes)
- NaN mask dilation (with scipy/numpy fallback)
- Safe HTTP requests with timeout
- Common helpers
"""

import os
import json
import numpy as np
from datetime import datetime


# ---------------------------------------------------------------------------
# File format detection
# ---------------------------------------------------------------------------

def detect_file_format(data_bytes):
    """Detect actual file format from magic bytes.

    Args:
        data_bytes: First 10+ bytes of a file.

    Returns:
        One of: "fits", "jpeg", "png", "gif", "unknown"
    """
    if data_bytes[:2] == b'\xff\xd8':
        return "jpeg"
    if data_bytes[:6] in (b'SIMPLE', b'XTENS'):
        return "fits"
    if data_bytes[:8] == b'\x89PNG\r\n\x1a\n':
        return "png"
    if data_bytes[:3] == b'GIF':
        return "gif"
    return "unknown"


# ---------------------------------------------------------------------------
# NaN mask dilation
# ---------------------------------------------------------------------------

def dilate_mask(mask, iterations=3):
    """Dilate a boolean mask to expand boundary regions.

    Uses scipy.ndimage.binary_dilation if available, otherwise falls back
    to a pure-numpy manual dilation (4-connected).

    Pan-STARRS warp images have NaN at tile edges; pixels adjacent to NaN
    have interpolation artifacts that mimic real sources. Dilating the NaN
    mask catches these edge artifacts.

    Args:
        mask: 2D boolean numpy array (True = bad pixel).
        iterations: Number of dilation iterations (pixels of expansion).

    Returns:
        Dilated boolean mask (same shape as input).
    """
    if mask is None or not np.any(mask):
        return mask

    try:
        from scipy.ndimage import binary_dilation
        return binary_dilation(mask, iterations=iterations)
    except ImportError:
        dilated_mask = mask.copy()
        for _ in range(iterations):
            expanded = np.zeros_like(dilated_mask)
            expanded[1:, :] |= dilated_mask[:-1, :]
            expanded[:-1, :] |= dilated_mask[1:, :]
            expanded[:, 1:] |= dilated_mask[:, :-1]
            expanded[:, :-1] |= dilated_mask[:, 1:]
            dilated_mask = expanded | dilated_mask
        return dilated_mask


# ---------------------------------------------------------------------------
# Safe HTTP request wrapper
# ---------------------------------------------------------------------------

def safe_request(method, url, timeout=30, **kwargs):
    """Make an HTTP request with consistent error handling.

    Args:
        method: "get" or "post"
        url: Target URL
        timeout: Request timeout in seconds
        **kwargs: Passed to requests.get/post

    Returns:
        (response, None) on success, (None, {"error": str}) on failure.
    """
    import requests

    try:
        func = requests.get if method.lower() == "get" else requests.post
        resp = func(url, timeout=timeout, **kwargs)
        resp.raise_for_status()
        return resp, None
    except Exception as e:
        return None, {"error": f"{type(e).__name__}: {str(e)[:200]}"}


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------

def timestamp_str(fmt="%Y%m%d_%H%M%S"):
    """Return current timestamp as a formatted string."""
    return datetime.now().strftime(fmt)


def save_result(subdir, filename, data, data_dir=None):
    """Save query result to data/<subdir>/<filename> as JSON.

    Args:
        subdir: Subdirectory under data/ (e.g., "simbad", "mast")
        filename: Output filename
        data: Dict/list to serialize
        data_dir: Override data directory (defaults to project data/)
    """
    if data_dir is None:
        # Import here to avoid circular imports — tools may not have config on sys.path
        data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
    out_dir = os.path.join(data_dir, subdir)
    os.makedirs(out_dir, exist_ok=True)
    fpath = os.path.join(out_dir, filename)
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    return fpath


def read_raster_image(filepath):
    """Read JPEG or PNG image as numpy array using Pillow.

    Args:
        filepath: Path to JPEG or PNG file.

    Returns:
        (data, header) tuple — data is float64 2D array, header is a dict.
    """
    from PIL import Image
    img = Image.open(filepath)
    if img.mode != 'L':
        img = img.convert('L')
    data = np.array(img, dtype=np.float64)

    # Detect format from extension
    ext = os.path.splitext(filepath)[1].lower()
    fmt = "PNG" if ext == ".png" else "JPEG"

    header = {
        "NAXIS1": data.shape[1],
        "NAXIS2": data.shape[0],
        "FORMAT": fmt,
        "BITPIX": 8,
    }
    return data, header
