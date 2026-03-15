"""
Radio image analysis tools — spectrum analysis and RFI detection.

Usage:
    python tools/radio_analysis.py analyze-spectrum --image data/images/radio_150.0_30.0_vlass.fits
    python tools/radio_analysis.py check-rfi --image data/images/radio_150.0_30.0_vlass.fits

All outputs go to stdout as JSON for the orchestrator to parse.
"""

import os
import sys
import json
import argparse
import math

import numpy as np
import warnings
warnings.filterwarnings("ignore", category=FutureWarning)
try:
    from astropy.wcs import FITSFixedWarning
    warnings.filterwarnings("ignore", category=FITSFixedWarning)
except ImportError:
    pass

from astropy.io import fits
from astropy.wcs import WCS

# Add project root to path so we can import shared modules
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import IMAGES_DIR
from tools.common import dilate_mask


# ---------------------------------------------------------------------------
# Radio FITS reader
# ---------------------------------------------------------------------------

def read_radio_fits(filepath):
    """Read a radio FITS image and return data + header.

    Handles common radio FITS quirks:
    - 4D cubes (freq, stokes, y, x) → squeeze to 2D
    - Jy/beam units
    - BMAJ/BMIN beam size in header

    Args:
        filepath: Path to FITS file

    Returns:
        (data_2d, header) tuple
    """
    filepath = os.path.normpath(filepath)
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"File not found: {filepath}")

    with fits.open(filepath) as hdul:
        data = hdul[0].data
        header = hdul[0].header

    if data is None:
        raise ValueError(f"No image data in {filepath}")

    # Squeeze extra dimensions (common in radio: 4D = stokes, freq, y, x)
    while data.ndim > 2:
        data = data[0]

    data = data.astype(np.float64)
    return data, header


# ---------------------------------------------------------------------------
# Spectrum / flux analysis
# ---------------------------------------------------------------------------

def analyze_radio_spectrum(filepath):
    """Analyze a radio FITS image — measure flux, noise, detect sources.

    Args:
        filepath: Path to radio FITS image

    Returns:
        dict with peak_flux, rms_noise, snr, sources, beam info, etc.
    """
    try:
        data, header = read_radio_fits(filepath)
    except Exception as e:
        return {"error": f"Failed to read radio FITS: {str(e)}"}

    # Mask NaN/inf
    valid_mask = np.isfinite(data)
    if not np.any(valid_mask):
        return {"error": "Image is entirely NaN/inf — no valid data"}

    valid_data = data[valid_mask]

    # Basic statistics
    rms_noise = float(np.std(valid_data))
    median_val = float(np.median(valid_data))
    peak_val = float(np.max(valid_data))
    min_val = float(np.min(valid_data))

    # Peak SNR
    peak_snr = float(peak_val / rms_noise) if rms_noise > 0 else 0.0

    # Extract beam info from header
    beam_info = {}
    if "BMAJ" in header:
        beam_info["bmaj_arcsec"] = float(header["BMAJ"]) * 3600
    if "BMIN" in header:
        beam_info["bmin_arcsec"] = float(header["BMIN"]) * 3600
    if "BPA" in header:
        beam_info["bpa_deg"] = float(header["BPA"])

    # Frequency info
    freq_mhz = None
    if "CRVAL3" in header:
        freq_mhz = float(header["CRVAL3"]) / 1e6
    elif "FREQ" in header:
        freq_mhz = float(header["FREQ"]) / 1e6
    elif "RESTFREQ" in header:
        freq_mhz = float(header["RESTFREQ"]) / 1e6

    # Units
    bunit = header.get("BUNIT", "unknown")

    # Detect radio sources (peaks above 5-sigma)
    sources = _detect_radio_sources(data, valid_mask, header, sigma_threshold=5.0, rms=rms_noise)

    # Convert to mJy/beam if in Jy/beam
    flux_unit = "raw"
    peak_flux_mjy = peak_val
    rms_mjy = rms_noise
    if "Jy" in str(bunit).lower():
        if "mjy" in str(bunit).lower():
            flux_unit = "mJy/beam"
        else:
            # Jy/beam → mJy/beam
            peak_flux_mjy = peak_val * 1000
            rms_mjy = rms_noise * 1000
            flux_unit = "mJy/beam"

    result = {
        "filepath": filepath,
        "image_shape": list(data.shape),
        "peak_flux": round(peak_flux_mjy, 4),
        "rms_noise": round(rms_mjy, 4),
        "flux_unit": flux_unit,
        "peak_snr": round(peak_snr, 1),
        "median": round(float(median_val), 6),
        "n_sources": len(sources),
        "sources": sources[:30],  # Limit output
        "beam": beam_info,
        "frequency_MHz": round(freq_mhz, 2) if freq_mhz else None,
        "bunit": str(bunit),
        "valid_pixel_fraction": round(float(np.sum(valid_mask)) / data.size, 3),
    }

    if freq_mhz:
        # Classify frequency band
        if freq_mhz < 300:
            result["band"] = "P-band (< 300 MHz)"
        elif freq_mhz < 1000:
            result["band"] = "UHF (300-1000 MHz)"
        elif freq_mhz < 2000:
            result["band"] = "L-band (1-2 GHz)"
        elif freq_mhz < 4000:
            result["band"] = "S-band (2-4 GHz)"
        elif freq_mhz < 8000:
            result["band"] = "C-band (4-8 GHz)"
        else:
            result["band"] = f"High-freq ({freq_mhz/1000:.1f} GHz)"

    return result


def _detect_radio_sources(data, valid_mask, header, sigma_threshold=5.0, rms=None):
    """Detect point sources in radio image above sigma threshold.

    Uses simple peak finding with NaN-aware masking.
    """
    if rms is None or rms <= 0:
        return []

    threshold = sigma_threshold * rms + np.nanmedian(data)
    sources = []

    # Find pixels above threshold
    above = (data > threshold) & valid_mask

    # Dilate NaN mask to avoid edge artifacts (same as optical pipeline)
    nan_mask = ~valid_mask
    dilated_nan = dilate_mask(nan_mask, iterations=3)
    above = above & ~dilated_nan

    if not np.any(above):
        return []

    # Simple peak finding: for each connected region, find the peak
    # Use a simple approach: find all above-threshold pixels, cluster by proximity
    ys, xs = np.where(above)
    peaks = []

    # Sort by flux (brightest first)
    fluxes = data[ys, xs]
    order = np.argsort(-fluxes)
    ys, xs, fluxes = ys[order], xs[order], fluxes[order]

    used = np.zeros(len(ys), dtype=bool)
    for i in range(len(ys)):
        if used[i]:
            continue
        y0, x0, f0 = int(ys[i]), int(xs[i]), float(fluxes[i])
        # Mark nearby pixels as used (within 5 pixels = ~1 beam)
        dists = np.sqrt((ys - y0)**2 + (xs - x0)**2)
        used[dists < 5] = True

        snr = f0 / rms if rms > 0 else 0

        source = {
            "x": x0, "y": y0,
            "peak_flux": round(f0, 6),
            "snr": round(snr, 1),
        }

        # Try to get RA/Dec from WCS
        try:
            wcs = WCS(header, naxis=2)
            sky = wcs.pixel_to_world(x0, y0)
            source["ra"] = round(float(sky.ra.deg), 6)
            source["dec"] = round(float(sky.dec.deg), 6)
        except Exception:
            pass

        peaks.append(source)

        if len(peaks) >= 50:
            break

    return peaks


# ---------------------------------------------------------------------------
# RFI detection
# ---------------------------------------------------------------------------

# Known RFI frequency ranges (MHz)
KNOWN_RFI_BANDS = [
    (87.5, 108.0, "FM Radio"),
    (174.0, 230.0, "VHF TV"),
    (470.0, 790.0, "UHF TV"),
    (824.0, 894.0, "Cellular 850"),
    (925.0, 960.0, "GSM 900"),
    (1176.0, 1176.5, "GPS L5"),
    (1227.0, 1227.6, "GPS L2"),
    (1381.05, 1381.05, "GPS L1 harmonic"),
    (1525.0, 1559.0, "Inmarsat"),
    (1575.0, 1575.5, "GPS L1"),
    (1710.0, 1880.0, "Cellular 1800"),
    (1920.0, 1980.0, "UMTS uplink"),
    (2110.0, 2170.0, "UMTS downlink"),
    (2400.0, 2500.0, "WiFi 2.4GHz"),
    (5150.0, 5850.0, "WiFi 5GHz"),
]


def check_rfi(filepath):
    """Check a radio FITS image for signs of RFI contamination.

    Checks:
    1. Known RFI frequency bands (from header frequency info)
    2. Stripe patterns (constant-flux rows/columns = terrestrial interference)
    3. Extreme outlier fraction (RFI creates bright narrow spikes)
    4. Spatial symmetry (astrophysical sources are ~point-like, RFI creates stripes)

    Args:
        filepath: Path to radio FITS image

    Returns:
        dict with is_rfi, rfi_fraction, rfi_type, confidence, details
    """
    try:
        data, header = read_radio_fits(filepath)
    except Exception as e:
        return {"error": f"Failed to read radio FITS: {str(e)}"}

    valid_mask = np.isfinite(data)
    if not np.any(valid_mask):
        return {"error": "Image is entirely NaN — cannot check for RFI"}

    rfi_flags = []
    rfi_score = 0.0  # 0 = clean, 1 = definitely RFI

    # --- Check 1: Known RFI frequency ---
    freq_mhz = None
    if "CRVAL3" in header:
        freq_mhz = float(header["CRVAL3"]) / 1e6
    elif "FREQ" in header:
        freq_mhz = float(header["FREQ"]) / 1e6
    elif "RESTFREQ" in header:
        freq_mhz = float(header["RESTFREQ"]) / 1e6

    if freq_mhz:
        for f_low, f_high, rfi_name in KNOWN_RFI_BANDS:
            if f_low <= freq_mhz <= f_high:
                rfi_flags.append(f"Frequency {freq_mhz:.1f} MHz falls in known RFI band: {rfi_name}")
                rfi_score += 0.5
                break

    # --- Check 2: Stripe detection (RFI creates horizontal/vertical stripes) ---
    valid_data = data.copy()
    valid_data[~valid_mask] = np.nan

    # Check row-to-row variance vs column-to-column variance
    row_medians = np.nanmedian(valid_data, axis=1)
    col_medians = np.nanmedian(valid_data, axis=0)
    row_var = float(np.nanstd(row_medians))
    col_var = float(np.nanstd(col_medians))
    pixel_rms = float(np.nanstd(valid_data))

    if pixel_rms > 0:
        stripe_ratio = max(row_var, col_var) / pixel_rms
        if stripe_ratio > 0.5:
            direction = "horizontal" if row_var > col_var else "vertical"
            rfi_flags.append(f"Strong {direction} stripe pattern detected (ratio={stripe_ratio:.2f})")
            rfi_score += min(stripe_ratio * 0.3, 0.4)

    # --- Check 3: Extreme outlier fraction ---
    if pixel_rms > 0:
        z_scores = np.abs((valid_data[valid_mask] - np.nanmedian(valid_data)) / pixel_rms)
        extreme_fraction = float(np.sum(z_scores > 10) / len(z_scores))
        if extreme_fraction > 0.01:
            rfi_flags.append(f"High fraction of extreme outliers: {extreme_fraction*100:.2f}% of pixels > 10σ")
            rfi_score += min(extreme_fraction * 10, 0.3)

    # --- Check 4: Edge brightening (satellite/ground reflection artifacts) ---
    ny, nx = data.shape
    if ny > 20 and nx > 20:
        edge_width = min(5, ny // 10, nx // 10)
        edge_pixels = np.concatenate([
            valid_data[:edge_width, :].flatten(),
            valid_data[-edge_width:, :].flatten(),
            valid_data[:, :edge_width].flatten(),
            valid_data[:, -edge_width:].flatten(),
        ])
        center_pixels = valid_data[ny//4:3*ny//4, nx//4:3*nx//4].flatten()

        edge_med = float(np.nanmedian(edge_pixels))
        center_med = float(np.nanmedian(center_pixels))

        if pixel_rms > 0:
            edge_excess = abs(edge_med - center_med) / pixel_rms
            if edge_excess > 2.0:
                rfi_flags.append(f"Edge brightening detected: edges {edge_excess:.1f}σ brighter than center")
                rfi_score += min(edge_excess * 0.1, 0.2)

    # --- Final verdict ---
    rfi_score = min(rfi_score, 1.0)

    if rfi_score >= 0.7:
        verdict = "LIKELY_RFI"
        confidence = rfi_score
    elif rfi_score >= 0.3:
        verdict = "POSSIBLE_RFI"
        confidence = rfi_score
    else:
        verdict = "CLEAN"
        confidence = 1.0 - rfi_score

    return {
        "filepath": filepath,
        "is_rfi": rfi_score >= 0.5,
        "verdict": verdict,
        "rfi_score": round(rfi_score, 3),
        "confidence": round(confidence, 3),
        "rfi_flags": rfi_flags,
        "frequency_MHz": round(freq_mhz, 2) if freq_mhz else None,
        "details": {
            "pixel_rms": round(pixel_rms, 6) if pixel_rms else None,
            "stripe_row_var": round(row_var, 6),
            "stripe_col_var": round(col_var, 6),
        },
        "message": f"RFI check: {verdict} (score={rfi_score:.2f}). {len(rfi_flags)} flag(s) raised."
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Radio Image Analysis Tools")
    sub = parser.add_subparsers(dest="command")

    # analyze-spectrum
    p_spec = sub.add_parser("analyze-spectrum", help="Analyze radio FITS image")
    p_spec.add_argument("--image", required=True, help="Path to radio FITS image")

    # check-rfi
    p_rfi = sub.add_parser("check-rfi", help="Check for RFI contamination")
    p_rfi.add_argument("--image", required=True, help="Path to radio FITS image")

    args = parser.parse_args()

    if args.command == "analyze-spectrum":
        result = analyze_radio_spectrum(args.image)
    elif args.command == "check-rfi":
        result = check_rfi(args.image)
    else:
        parser.print_help()
        result = {"error": "No command specified. Use: analyze-spectrum, check-rfi"}

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
