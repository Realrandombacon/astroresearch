"""
Image analysis tools for astronomical anomaly detection.
Compares multi-epoch images, detects sources, finds changes.
Supports both FITS (via astropy) and JPEG/PNG (via Pillow) images.
Also supports vision analysis via Qwen 3.5 multimodal.

Usage:
    python tools/image_analysis.py detect-sources --image data/images/cutout.fits
    python tools/image_analysis.py compare --img1 epoch1.fits --img2 epoch2.fits
    python tools/image_analysis.py list-images
    python tools/image_analysis.py to-png --image data/images/cutout.fits
    python tools/image_analysis.py analyze --image data/images/cutout.fits --prompt "Describe sources"
"""

import os
import sys
import json
import math
import argparse
import base64
from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS
from PIL import Image

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
IMAGES_DIR = os.path.join(DATA_DIR, "images")
PNG_DIR = os.path.join(DATA_DIR, "png")
OLLAMA_URL = "http://localhost:11434/api/chat"

os.makedirs(PNG_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# FITS reader (astropy)
# ---------------------------------------------------------------------------

def read_fits_data(filepath):
    """Read FITS image data using astropy. Returns (numpy_2d_array, header)."""
    # Try fuzzy matching first (handles coordinate rounding mismatches)
    filepath = fuzzy_find_image(filepath)
    
    if not os.path.exists(filepath):
        avail = []
        if os.path.exists(IMAGES_DIR):
            avail = [f for f in os.listdir(IMAGES_DIR) if f.endswith(".fits")]
        raise FileNotFoundError(
            f"FITS file not found: {filepath}. "
            f"Available files ({len(avail)}): {avail[:20]}"
        )

    with fits.open(filepath) as hdul:
        # Find the first image HDU with data
        for hdu in hdul:
            if hdu.data is not None and hdu.data.ndim >= 2:
                header = dict(hdu.header)
                data = hdu.data.astype(np.float64)
                # Track NaN fraction before replacing (for compare_images guard)
                nan_count = np.count_nonzero(np.isnan(data))
                header["_NAN_FRACTION"] = nan_count / data.size if data.size > 0 else 0.0
                # Handle NaN values
                data = np.nan_to_num(data, nan=0.0, posinf=0.0, neginf=0.0)
                return data, header

        # If no 2D image HDU found, try primary
        if hdul[0].data is not None:
            header = dict(hdul[0].header)
            data = hdul[0].data.astype(np.float64)
            nan_count = np.count_nonzero(np.isnan(data))
            header["_NAN_FRACTION"] = nan_count / data.size if data.size > 0 else 0.0
            data = np.nan_to_num(data, nan=0.0, posinf=0.0, neginf=0.0)
            return data, header

    return None, {}


def read_fits_header(filepath):
    """Read FITS header using astropy."""
    with fits.open(filepath) as hdul:
        return dict(hdul[0].header)


# ---------------------------------------------------------------------------
# Universal image reader (FITS + JPEG + PNG)
# ---------------------------------------------------------------------------


def fuzzy_find_image(filepath):
    """Find the closest matching image file when exact path doesn't exist.
    
    Handles coordinate rounding mismatches:
    - Qwen asks for cutout_292.085793_40.005694_g.fits
    - Actual file is cutout_292.0858_40.0057_g.fits
    
    Returns the corrected path if found, original path otherwise.
    """
    if os.path.exists(filepath):
        return filepath
    
    # Extract directory and filename
    dirname = os.path.dirname(filepath)
    basename = os.path.basename(filepath)
    
    # If no directory specified, check IMAGES_DIR
    if not dirname or dirname == ".":
        search_dir = IMAGES_DIR
    elif dirname.replace("\\", "/").endswith("data/images"):
        search_dir = IMAGES_DIR
    else:
        search_dir = dirname
    
    if not os.path.exists(search_dir):
        return filepath
    
    # Try to extract coordinates and band from filename
    # Patterns: cutout_RA_DEC_BAND.ext, warp_RA_DEC_BAND_ep..., legacy_RA_DEC_BAND.ext
    import re
    # Try warp pattern first (has extra fields after band)
    m = re.match(r'(?:cutout|warp|legacy)_([\d.]+)_([\d.]+)_([grizy])(?:_ep\d+_mjd[\d.]+)?\.(.+)$', basename)
    if not m:
        return filepath

    target_ra = float(m.group(1))
    target_dec = float(m.group(2))
    target_band = m.group(3)
    target_ext = m.group(4)
    # Detect prefix type to match same kind of file
    target_prefix = basename.split("_")[0]  # cutout, warp, or legacy

    # Search for closest coordinate match with same band and extension
    best_match = None
    best_dist = 0.01  # Max 0.01 degree tolerance (~36 arcsec)

    for f in os.listdir(search_dir):
        fm = re.match(r'(?:cutout|warp|legacy)_([\d.]+)_([\d.]+)_([grizy])(?:_ep\d+_mjd[\d.]+)?\.(.+)$', f)
        if not fm:
            continue
        # Must match same prefix, band, and extension
        file_prefix = f.split("_")[0]
        if file_prefix != target_prefix:
            continue
        if fm.group(3) != target_band or fm.group(4) != target_ext:
            continue

        file_ra = float(fm.group(1))
        file_dec = float(fm.group(2))
        dist = abs(file_ra - target_ra) + abs(file_dec - target_dec)

        if dist < best_dist:
            best_dist = dist
            best_match = f

    if best_match:
        return os.path.join(search_dir, best_match)

    return filepath

def read_image_data(filepath):
    """Read image data from any format (FITS, JPEG, PNG).

    Returns (numpy_2d_array, header_dict).
    For non-FITS images, header is minimal (just dimensions).
    """
    # Try fuzzy matching (handles coordinate rounding mismatches)
    filepath = fuzzy_find_image(filepath)
    
    if not os.path.exists(filepath):
        avail = []
        if os.path.exists(IMAGES_DIR):
            avail = [f for f in os.listdir(IMAGES_DIR)
                     if f.endswith((".fits", ".jpg", ".jpeg", ".png"))]
        raise FileNotFoundError(
            f"Image not found: {filepath}. "
            f"Available ({len(avail)}): {avail[:20]}"
        )

    # Detect format from magic bytes
    with open(filepath, "rb") as f:
        magic = f.read(10)

    if magic[:2] == b'\xff\xd8':
        # JPEG
        return _read_jpeg_data(filepath)
    elif magic[:8] == b'\x89PNG\r\n\x1a\n':
        # PNG
        return _read_png_data(filepath)
    elif magic[:6] in (b'SIMPLE', b'XTENS'):
        # FITS
        return read_fits_data(filepath)
    else:
        # Try Pillow as fallback
        try:
            return _read_jpeg_data(filepath)
        except Exception:
            raise ValueError(f"Unknown image format: {filepath} (magic: {magic[:8].hex()})")


def _read_jpeg_data(filepath):
    """Read JPEG/PNG image as numpy array using Pillow."""
    img = Image.open(filepath)
    # Convert to grayscale if color
    if img.mode != 'L':
        img = img.convert('L')
    data = np.array(img, dtype=np.float64)
    header = {
        "NAXIS1": data.shape[1],
        "NAXIS2": data.shape[0],
        "FORMAT": "JPEG",
        "BITPIX": 8,
    }
    return data, header


def _read_png_data(filepath):
    """Read PNG image as numpy array using Pillow."""
    img = Image.open(filepath)
    if img.mode != 'L':
        img = img.convert('L')
    data = np.array(img, dtype=np.float64)
    header = {
        "NAXIS1": data.shape[1],
        "NAXIS2": data.shape[0],
        "FORMAT": "PNG",
        "BITPIX": 8,
    }
    return data, header


# ---------------------------------------------------------------------------
# FITS to PNG converter (for vision pipeline)
# ---------------------------------------------------------------------------

def fits_to_png(filepath, output_path=None, percentile_low=1, percentile_high=99):
    """Convert any image (FITS, JPEG, PNG) to a contrast-stretched PNG.

    Uses percentile stretching for good visibility of astronomical sources.
    Returns the output PNG path.
    """
    # Use universal reader — handles FITS, JPEG, PNG
    data, header = read_image_data(filepath)
    if data is None:
        return None

    # Percentile stretch for good contrast
    vmin = np.percentile(data, percentile_low)
    vmax = np.percentile(data, percentile_high)

    if vmax <= vmin:
        vmax = vmin + 1

    # Normalize to 0-255
    stretched = np.clip((data - vmin) / (vmax - vmin) * 255, 0, 255).astype(np.uint8)

    # Flip vertically (FITS convention: origin at bottom-left)
    stretched = np.flipud(stretched)

    img = Image.fromarray(stretched, mode="L")

    if output_path is None:
        basename = Path(filepath).stem
        output_path = os.path.join(PNG_DIR, f"{basename}.png")

    img.save(output_path)
    return output_path


def image_to_base64(png_path):
    """Encode a PNG image to base64 string for Ollama vision API."""
    with open(png_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


# ---------------------------------------------------------------------------
# Vision analysis via Qwen 3.5 multimodal
# ---------------------------------------------------------------------------

def analyze_image_with_vision(filepath, prompt=None, model="qwen3.5:4b"):
    """Send any image (FITS, JPEG, PNG) to Qwen 3.5 vision for analysis.

    Converts to PNG if needed, encodes as base64, and sends to Ollama.
    Returns the model's visual analysis as text.
    """
    # For JPEG/PNG, we can send directly; for FITS, convert first
    ext = os.path.splitext(filepath)[1].lower()
    if ext in (".jpg", ".jpeg", ".png"):
        # Already a viewable image — use directly
        b64 = image_to_base64(filepath)
        png_path = filepath
    else:
        # FITS or unknown — convert to PNG
        png_path = fits_to_png(filepath)
        if png_path is None:
            return {"error": "Could not convert image to PNG — data may be empty"}
        b64 = image_to_base64(png_path)

    import requests  # lazy import — only needed for vision

    if prompt is None:
        prompt = (
            "You are an astronomical image analyst. Analyze this sky image cutout. "
            "Describe: 1) How many point sources (stars) are visible, "
            "2) Any extended objects (galaxies, nebulae), "
            "3) Any artifacts or unusual features, "
            "4) Any sources that look unusual or potentially transient. "
            "Be specific about positions (center, upper-left, etc)."
        )

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": [b64],
            }
        ],
        "stream": False,
        "options": {
            "temperature": 0.3,
            "num_predict": 2000,
        },
    }

    try:
        resp = requests.post(OLLAMA_URL, json=payload, timeout=80)
        resp.raise_for_status()
        result = resp.json()
        analysis = result.get("message", {}).get("content", "").strip()
        return {
            "image": filepath,
            "png": png_path,
            "analysis": analysis,
            "model": model,
        }
    except requests.exceptions.Timeout:
        return {"error": "Ollama timeout (80s) during vision analysis — model may be overloaded"}
    except Exception as e:
        return {"error": f"Vision analysis failed: {str(e)}"}


# ---------------------------------------------------------------------------
# Source detection (numpy-based, much faster than pure Python)
# ---------------------------------------------------------------------------

def detect_sources_simple(data, threshold_sigma=3.0, min_size=3):
    """Detect point sources using sigma-clipping peak detection with numpy."""
    if data is None:
        return []

    nrows, ncols = data.shape

    # Statistics (with sigma clipping for robust background)
    mean_val = np.mean(data)
    std_val = np.std(data)

    if std_val == 0:
        return []

    threshold = mean_val + threshold_sigma * std_val

    # Find pixels above threshold
    above = data > threshold
    sources = []
    margin = min_size

    # Check local maxima among above-threshold pixels
    for y in range(margin, nrows - margin):
        for x in range(margin, ncols - margin):
            if not above[y, x]:
                continue

            val = data[y, x]
            # Check if local maximum in 3x3 neighborhood
            neighborhood = data[y-1:y+2, x-1:x+2]
            if val < np.max(neighborhood) or val == neighborhood[0, 0]:
                # Not a local max (or tied with corner — skip ties except center)
                if val != data[y, x] or np.sum(neighborhood == val) > 1:
                    if val < np.max(neighborhood):
                        continue

            # Centroid refinement in 3x3 box
            box = data[y-1:y+2, x-1:x+2] - mean_val
            box = np.clip(box, 0, None)
            total_flux = np.sum(box)

            if total_flux > 0:
                yy, xx = np.mgrid[-1:2, -1:2]
                cx = x + np.sum(xx * box) / total_flux
                cy = y + np.sum(yy * box) / total_flux
            else:
                cx, cy = float(x), float(y)

            sources.append({
                "x": round(float(cx), 2),
                "y": round(float(cy), 2),
                "peak_value": round(float(val), 4),
                "flux": round(float(total_flux), 4),
                "snr": round(float((val - mean_val) / std_val), 2),
            })

    # Sort by flux, deduplicate nearby sources
    sources.sort(key=lambda s: s["flux"], reverse=True)

    # Remove duplicates within 3 pixels
    filtered = []
    for src in sources:
        too_close = False
        for existing in filtered:
            dist = math.sqrt((src["x"] - existing["x"])**2 + (src["y"] - existing["y"])**2)
            if dist < 3.0:
                too_close = True
                break
        if not too_close:
            filtered.append(src)

    return filtered


# ---------------------------------------------------------------------------
# Image comparison / difference
# ---------------------------------------------------------------------------

def compare_images(data1, data2, header1=None, header2=None):
    """Compare two images and find significant differences (transient detection)."""
    if data1 is None or data2 is None:
        return {"error": "Cannot compare: one or both images are None"}

    # Crop to common size
    nrows = min(data1.shape[0], data2.shape[0])
    ncols = min(data1.shape[1], data2.shape[1])
    d1 = data1[:nrows, :ncols]
    d2 = data2[:nrows, :ncols]

    # Difference image
    diff = d2 - d1

    mean_diff = np.mean(diff)
    std_diff = np.std(diff)

    if std_diff == 0:
        return {
            "image_size": f"{ncols}x{nrows}",
            "total_anomalies": 0,
            "brightening_events": [],
            "fading_events": [],
            "message": "Images are identical",
        }

    sigma_thresh = 5.0
    significance = (diff - mean_diff) / std_diff

    # Find positive anomalies (new/brightening sources)
    pos_mask = significance > sigma_thresh
    neg_mask = significance < -sigma_thresh

    positive_anomalies = []
    negative_anomalies = []
    margin = 3

    for mask, anomaly_list, sign in [
        (pos_mask, positive_anomalies, 1),
        (neg_mask, negative_anomalies, -1),
    ]:
        ys, xs = np.where(mask[margin:-margin, margin:-margin])
        ys += margin
        xs += margin

        for y, x in zip(ys, xs):
            val = significance[y, x]
            # Local maximum check
            neighborhood = significance[y-1:y+2, x-1:x+2]
            if sign > 0 and val < np.max(neighborhood):
                continue
            if sign < 0 and val > np.min(neighborhood):
                continue

            anomaly_list.append({
                "x": int(x),
                "y": int(y),
                "significance_sigma": round(float(val), 2),
                "diff_value": round(float(diff[y, x]), 4),
                "epoch1_value": round(float(d1[y, x]), 4),
                "epoch2_value": round(float(d2[y, x]), 4),
            })

    positive_anomalies.sort(key=lambda a: a["significance_sigma"], reverse=True)
    negative_anomalies.sort(key=lambda a: a["significance_sigma"])

    return {
        "image_size": f"{ncols}x{nrows}",
        "diff_stats": {
            "mean": round(float(mean_diff), 6),
            "std": round(float(std_diff), 6),
            "sigma_threshold": sigma_thresh,
        },
        "brightening_events": positive_anomalies[:20],
        "fading_events": negative_anomalies[:20],
        "n_brightening": len(positive_anomalies),
        "n_fading": len(negative_anomalies),
        "total_anomalies": len(positive_anomalies) + len(negative_anomalies),
    }


# ---------------------------------------------------------------------------
# Pixel-to-WCS conversion (astropy)
# ---------------------------------------------------------------------------

def pixel_to_radec(x, y, header):
    """Convert pixel coordinates to RA/Dec using astropy WCS."""
    try:
        w = WCS(header)
        coords = w.pixel_to_world(x, y)
        return round(coords.ra.deg, 6), round(coords.dec.deg, 6)
    except Exception:
        # Fallback to simple linear WCS
        crpix1 = header.get("CRPIX1", 0)
        crpix2 = header.get("CRPIX2", 0)
        crval1 = header.get("CRVAL1", 0)
        crval2 = header.get("CRVAL2", 0)
        cd1_1 = header.get("CD1_1", header.get("CDELT1", 0))
        cd1_2 = header.get("CD1_2", 0)
        cd2_1 = header.get("CD2_1", 0)
        cd2_2 = header.get("CD2_2", header.get("CDELT2", 0))

        dx = x - crpix1
        dy = y - crpix2
        ra = crval1 + cd1_1 * dx + cd1_2 * dy
        dec = crval2 + cd2_1 * dx + cd2_2 * dy

        return round(ra, 6), round(dec, 6)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Astronomical Image Analysis Tools")
    sub = parser.add_subparsers(dest="command")

    # detect-sources
    p_detect = sub.add_parser("detect-sources", help="Detect sources in a FITS image")
    p_detect.add_argument("--image", required=True, help="Path to FITS image")
    p_detect.add_argument("--sigma", type=float, default=3.0, help="Detection threshold")

    # compare
    p_compare = sub.add_parser("compare", help="Compare two epoch images")
    p_compare.add_argument("--img1", required=True, help="Epoch 1 FITS image")
    p_compare.add_argument("--img2", required=True, help="Epoch 2 FITS image")

    # info
    p_info = sub.add_parser("info", help="Show FITS header info")
    p_info.add_argument("--image", required=True, help="Path to FITS image")

    # list-images
    sub.add_parser("list-images", help="List available FITS images in data/images/")

    # to-png
    p_png = sub.add_parser("to-png", help="Convert FITS to PNG for visual inspection")
    p_png.add_argument("--image", required=True, help="Path to FITS image")

    # analyze (vision)
    p_analyze = sub.add_parser("analyze", help="Analyze image using Qwen 3.5 vision")
    p_analyze.add_argument("--image", required=True, help="Path to FITS image")
    p_analyze.add_argument("--prompt", default=None, help="Custom analysis prompt")
    p_analyze.add_argument("--model", default="qwen3.5:4b", help="Ollama model name")

    args = parser.parse_args()

    if args.command == "detect-sources":
        data, header = read_image_data(args.image)
        sources = detect_sources_simple(data, threshold_sigma=args.sigma)

        # Add WCS coordinates
        for src in sources:
            ra, dec = pixel_to_radec(src["x"], src["y"], header)
            src["ra"] = ra
            src["dec"] = dec

        result = {
            "image": args.image,
            "image_size": f"{header.get('NAXIS1', '?')}x{header.get('NAXIS2', '?')}",
            "n_sources": len(sources),
            "sources": sources[:50],
        }
        print(json.dumps(result, indent=2))

    elif args.command == "compare":
        data1, h1 = read_image_data(args.img1)
        data2, h2 = read_image_data(args.img2)

        # --- NaN guard: reject images that are mostly empty ---
        nan_frac1 = h1.get("_NAN_FRACTION", 0.0)
        nan_frac2 = h2.get("_NAN_FRACTION", 0.0)
        NAN_THRESHOLD = 0.5  # reject if >50% NaN

        if nan_frac1 > NAN_THRESHOLD or nan_frac2 > NAN_THRESHOLD:
            bad = []
            if nan_frac1 > NAN_THRESHOLD:
                bad.append(f"img1 ({os.path.basename(args.img1)}) is {nan_frac1:.0%} NaN")
            if nan_frac2 > NAN_THRESHOLD:
                bad.append(f"img2 ({os.path.basename(args.img2)}) is {nan_frac2:.0%} NaN")
            result = {
                "error": "BLANK_IMAGE",
                "epoch1": args.img1,
                "epoch2": args.img2,
                "nan_fraction_img1": round(nan_frac1, 3),
                "nan_fraction_img2": round(nan_frac2, 3),
                "WARNING": (
                    f"COMPARISON REJECTED: {'; '.join(bad)}. "
                    f"An image with >50% NaN pixels has no valid astronomical data — "
                    f"it was likely outside the survey footprint for that epoch. "
                    f"Any 'transients' would be artifacts (zeros vs real pixels). "
                    f"Try download_multiepoch with fewer epochs, or try a different filter."
                ),
                "brightening_events": [],
                "fading_events": [],
            }
            print(json.dumps(result, indent=2))
            sys.exit(0)

        # --- NaN edge masking: zero out pixels that were NaN in EITHER image ---
        # Warp images have NaN at edges where coverage differs between epochs.
        # Without masking, these become 0 vs real_value → false "transients".
        if nan_frac1 > 0 or nan_frac2 > 0:
            # Re-open files to get raw NaN masks (data already has NaN→0)
            def _get_nan_mask(filepath):
                filepath = fuzzy_find_image(filepath)
                ext = os.path.splitext(filepath)[1].lower()
                if ext not in ('.fits',):
                    return None
                try:
                    with fits.open(filepath) as hdul:
                        for hdu in hdul:
                            if hdu.data is not None and hdu.data.ndim >= 2:
                                return np.isnan(hdu.data)
                        if hdul[0].data is not None:
                            return np.isnan(hdul[0].data)
                except Exception:
                    pass
                return None

            mask1 = _get_nan_mask(args.img1)
            mask2 = _get_nan_mask(args.img2)
            if mask1 is not None and mask2 is not None:
                # combined_bad = True where EITHER image had NaN
                combined_bad = mask1 | mask2
                n_masked = int(np.count_nonzero(combined_bad))
                if n_masked > 0:
                    data1[combined_bad] = 0.0
                    data2[combined_bad] = 0.0
                    nan_frac_combined = n_masked / combined_bad.size
                    h1["_MASKED_PIXELS"] = n_masked
                    h1["_MASKED_FRACTION"] = round(nan_frac_combined, 3)

        result = compare_images(data1, data2, h1, h2)
        result["epoch1"] = args.img1
        result["epoch2"] = args.img2

        # Add NaN masking info if applicable
        if "_MASKED_FRACTION" in h1:
            result["nan_masked_fraction"] = h1["_MASKED_FRACTION"]
            result["nan_note"] = (
                f"{h1['_MASKED_PIXELS']} edge pixels masked "
                f"({h1['_MASKED_FRACTION']:.0%} of image) — "
                f"these were NaN in one or both epochs and excluded from analysis."
            )

        # --- Band & epoch extraction helpers ---
        import re as _re

        def _extract_band(path):
            """Extract photometric band from any image filename.

            Handles:
              cutout_RA_DEC_g.fits       (Pan-STARRS stacked)
              warp_RA_DEC_g_ep1_mjd...   (Pan-STARRS warp)
              legacy_RA_DEC_g.fits       (Legacy Survey)
            """
            basename = os.path.basename(path)
            # Try warp pattern first (band before _ep)
            m = _re.search(r'_([grizy])_ep\d+', basename)
            if m:
                return m.group(1)
            # Standard cutout/legacy pattern (band before extension)
            m = _re.search(r'_([grizy])\.(fits|jpg|jpeg|png)$', basename)
            return m.group(1) if m else None

        def _extract_epoch_info(path):
            """Detect source type and epoch metadata from filename."""
            basename = os.path.basename(path)
            if basename.startswith('warp_'):
                m = _re.search(r'_ep(\d+)_mjd(\d+\.?\d*)', basename)
                if m:
                    return {'source': 'panstarrs_warp', 'epoch': int(m.group(1)),
                            'mjd': float(m.group(2))}
                return {'source': 'panstarrs_warp', 'epoch': None, 'mjd': None}
            elif basename.startswith('legacy_'):
                return {'source': 'legacy', 'epoch': None, 'mjd': None}
            elif basename.startswith('cutout_'):
                return {'source': 'panstarrs_stack', 'epoch': None, 'mjd': None}
            return None

        band1 = _extract_band(args.img1)
        band2 = _extract_band(args.img2)
        epoch1 = _extract_epoch_info(args.img1)
        epoch2 = _extract_epoch_info(args.img2)

        if band1 and band2:
            if band1 != band2:
                # ===== CROSS-BAND: STRONG WARNING =====
                result["comparison_type"] = "CROSS-BAND"
                result["bands"] = f"{band1} vs {band2}"
                result["WARNING"] = (
                    f"CROSS-BAND COMPARISON ({band1} vs {band2}): "
                    f"ALL differences are due to NORMAL STELLAR COLORS, not transients! "
                    f"Stars are naturally brighter in redder bands. "
                    f"This comparison CANNOT detect transients. "
                    f"Use download_multiepoch() to get same-band, different-epoch images instead."
                )
                result["brightening_events_note"] = "NOT real brightenings — just color differences between bands."
                result["fading_events_note"] = "NOT real fadings — just color differences between bands."
            else:
                # ===== SAME BAND — check if truly different epochs =====
                is_cross_epoch = False
                time_info = ""

                if epoch1 and epoch2:
                    mjd1 = epoch1.get('mjd')
                    mjd2 = epoch2.get('mjd')
                    if mjd1 and mjd2 and mjd1 != mjd2:
                        is_cross_epoch = True
                        time_diff = abs(mjd1 - mjd2)
                        result["time_baseline_days"] = round(time_diff, 1)
                        time_info = f", {round(time_diff, 1)} day baseline"
                    elif epoch1.get('source') != epoch2.get('source'):
                        # Different surveys (e.g., Pan-STARRS stack vs Legacy)
                        is_cross_epoch = True
                        result["cross_survey"] = True
                        time_info = f", cross-survey ({epoch1.get('source')} vs {epoch2.get('source')})"

                result["comparison_type"] = "same-band"
                result["band"] = band1
                result["temporal"] = is_cross_epoch

                if is_cross_epoch:
                    result["note"] = (
                        f"TEMPORAL COMPARISON ({band1}-band{time_info}): "
                        f"Differences here may indicate REAL variability or transient events! "
                        f"This is the correct way to find transients."
                    )
                else:
                    result["note"] = (
                        f"Same-band ({band1}) but possibly same epoch. "
                        f"Use download_multiepoch(filter='{band1}') to get images "
                        f"from different times for real transient detection."
                    )

        for anom in result.get("brightening_events", []) + result.get("fading_events", []):
            ra, dec = pixel_to_radec(anom["x"], anom["y"], h1)
            anom["ra"] = ra
            anom["dec"] = dec

        print(json.dumps(result, indent=2))

    elif args.command == "info":
        ext = os.path.splitext(args.image)[1].lower()
        if ext in (".jpg", ".jpeg", ".png"):
            img = Image.open(args.image)
            safe = {
                "FORMAT": ext.lstrip(".").upper(),
                "NAXIS1": img.size[0],
                "NAXIS2": img.size[1],
                "MODE": img.mode,
                "SIZE_BYTES": os.path.getsize(args.image),
            }
        else:
            header = read_fits_header(args.image)
            safe = {}
            for k, v in header.items():
                try:
                    json.dumps(v)
                    safe[k] = v
                except (TypeError, ValueError):
                    safe[k] = str(v)
        print(json.dumps(safe, indent=2))

    elif args.command == "list-images":
        if not os.path.exists(IMAGES_DIR):
            print(json.dumps({"images": [], "message": "No data/images/ directory yet. Use download_cutout first!"}))
        else:
            files = sorted([f for f in os.listdir(IMAGES_DIR) if f.endswith((".fits", ".jpg", ".jpeg", ".png"))])
            images = []
            for f in files:
                fpath = os.path.join(IMAGES_DIR, f)
                ext = os.path.splitext(f)[1].lower()
                try:
                    if ext in (".jpg", ".jpeg", ".png"):
                        img = Image.open(fpath)
                        images.append({
                            "filename": f,
                            "path": os.path.join("data", "images", f),
                            "size_bytes": os.path.getsize(fpath),
                            "dimensions": f"{img.size[0]}x{img.size[1]}",
                            "format": ext.lstrip("."),
                            "has_image_data": True,
                        })
                    else:
                        with fits.open(fpath) as hdul:
                            hdr = hdul[0].header
                            has_data = any(h.data is not None for h in hdul)
                            images.append({
                                "filename": f,
                                "path": os.path.join("data", "images", f),
                                "size_bytes": os.path.getsize(fpath),
                                "dimensions": f"{hdr.get('NAXIS1', '?')}x{hdr.get('NAXIS2', '?')}",
                                "format": "fits",
                                "has_image_data": has_data,
                            })
                except Exception as e:
                    images.append({
                        "filename": f,
                        "path": os.path.join("data", "images", f),
                        "size_bytes": os.path.getsize(fpath),
                        "error": str(e)[:80],
                    })
            print(json.dumps({
                "images": images,
                "total": len(images),
                "directory": os.path.join("data", "images"),
            }, indent=2))

    elif args.command == "to-png":
        png_path = fits_to_png(args.image)
        if png_path:
            print(json.dumps({"fits": args.image, "png": png_path, "status": "ok"}))
        else:
            print(json.dumps({"error": "Could not convert — no image data in FITS"}))

    elif args.command == "analyze":
        result = analyze_image_with_vision(
            args.image,
            prompt=args.prompt,
            model=args.model,
        )
        print(json.dumps(result, indent=2))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
