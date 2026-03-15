"""
Radio astronomy query tools — download radio survey data and cross-check catalogs.

Usage:
    python tools/radio_query.py download-radio --ra 150.0 --dec 30.0 --radius 5 --survey vlass
    python tools/radio_query.py check-pulsar --ra 150.0 --dec 30.0 --radius 60
    python tools/radio_query.py check-frb --ra 150.0 --dec 30.0 --radius 60

All outputs go to stdout as JSON for the orchestrator to parse.
"""

import os
import sys
import json
import argparse
import re

# Add project root to path so we can import shared modules
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import DATA_DIR, IMAGES_DIR
from tools.common import safe_request, timestamp_str


# ---------------------------------------------------------------------------
# VLASS / FIRST / LOFAR — Radio survey cutout downloads
# ---------------------------------------------------------------------------

# CIRADA Image Cutout Service (VLASS Quick Look)
CIRADA_CUTOUT = "https://ws-cadc.canfar.net/caom2ops/cutout"
# FIRST Survey cutout service
FIRST_CUTOUT = "https://third.ucllnl.org/cgi-bin/firstcutout"
# LOFAR VO TAP (for metadata — actual images via SkyView)
SKYVIEW_URL = "https://skyview.gsfc.nasa.gov/current/cgi/runquery.pl"


def download_radio_cutout(ra, dec, radius_arcmin=5.0, survey="vlass"):
    """Download a radio survey cutout image.

    Args:
        ra: Right ascension in degrees
        dec: Declination in degrees
        radius_arcmin: Cutout radius in arcminutes
        survey: One of 'vlass', 'first', 'nvss', 'lofar'

    Returns:
        dict with status, filepath, and metadata
    """
    survey = survey.lower().strip()
    filename = f"radio_{ra:.4f}_{dec:.4f}_{survey}.fits"
    filepath = os.path.join(IMAGES_DIR, filename)

    # Check if already downloaded
    if os.path.exists(filepath) and os.path.getsize(filepath) > 1000:
        return {
            "status": "already_exists",
            "filepath": filepath,
            "survey": survey,
            "ra": ra, "dec": dec,
            "message": f"Radio cutout already downloaded: {filename}"
        }

    if survey == "vlass":
        return _download_via_skyview(ra, dec, radius_arcmin, "VLASS1.2", filepath, survey)
    elif survey == "first":
        return _download_first(ra, dec, radius_arcmin, filepath)
    elif survey == "nvss":
        return _download_via_skyview(ra, dec, radius_arcmin, "NVSS", filepath, survey)
    elif survey == "lofar":
        return _download_via_skyview(ra, dec, radius_arcmin, "LoTSS DR2", filepath, survey)
    else:
        return {"error": f"Unknown survey '{survey}'. Use: vlass, first, nvss, lofar"}


def _download_via_skyview(ra, dec, radius_arcmin, skyview_survey, filepath, survey_name):
    """Download a radio cutout via NASA SkyView (supports VLASS, NVSS, LOFAR/LoTSS)."""
    params = {
        "Position": f"{ra},{dec}",
        "Survey": skyview_survey,
        "Pixels": "300",
        "Size": str(radius_arcmin / 60.0),  # SkyView expects degrees
        "Return": "FITS",
    }

    resp, err = safe_request("get", SKYVIEW_URL, timeout=90, params=params)
    if err:
        return {
            "error": f"SkyView download failed for {skyview_survey}: {err['error']}",
            "survey": survey_name,
            "ra": ra, "dec": dec,
            "hint": f"SkyView may not have {skyview_survey} coverage at this position. Try a different survey or coordinates."
        }

    # SkyView returns FITS directly if successful, or HTML error page
    content = resp.content
    if content[:6] in (b'SIMPLE', b'XTENS') or content[:30].startswith(b'SIMPLE'):
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "wb") as f:
            f.write(content)
        return {
            "status": "downloaded",
            "filepath": filepath,
            "survey": survey_name,
            "skyview_survey": skyview_survey,
            "ra": ra, "dec": dec,
            "size_bytes": len(content),
            "message": f"Downloaded {skyview_survey} cutout ({len(content)} bytes)"
        }
    else:
        # Probably an HTML error page
        snippet = content[:500].decode("utf-8", errors="replace")
        return {
            "error": f"SkyView returned non-FITS response for {skyview_survey}",
            "survey": survey_name,
            "ra": ra, "dec": dec,
            "response_preview": snippet[:200],
            "hint": f"No {skyview_survey} coverage at RA={ra}, Dec={dec}. Try a different position or survey."
        }


def _download_first(ra, dec, radius_arcmin, filepath):
    """Download a FIRST survey cutout (1.4 GHz, 5\" resolution)."""
    # FIRST cutout service expects specific format
    params = {
        "RA": f"{ra}",
        "Dec": f"{dec}",
        "Equinox": "J2000",
        "ImageSize": str(min(radius_arcmin, 30)),  # max 30 arcmin
        "ImageType": "FITS File",
        "MaxInt": "10",
        ".submit": "Extract the Cutout",
    }

    resp, err = safe_request("post", FIRST_CUTOUT, timeout=60, data=params)
    if err:
        return {
            "error": f"FIRST cutout download failed: {err['error']}",
            "survey": "first",
            "ra": ra, "dec": dec,
            "hint": "FIRST coverage: Dec > -10° only. Check if your target is in range."
        }

    content = resp.content
    if content[:6] in (b'SIMPLE', b'XTENS'):
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "wb") as f:
            f.write(content)
        return {
            "status": "downloaded",
            "filepath": filepath,
            "survey": "first",
            "frequency_MHz": 1400,
            "resolution_arcsec": 5.0,
            "ra": ra, "dec": dec,
            "size_bytes": len(content),
            "message": f"Downloaded FIRST 1.4GHz cutout ({len(content)} bytes)"
        }
    else:
        return {
            "error": "FIRST cutout returned non-FITS data (likely outside coverage)",
            "survey": "first",
            "ra": ra, "dec": dec,
            "hint": "FIRST survey covers Dec > -10° in the north galactic cap."
        }


# ---------------------------------------------------------------------------
# ATNF Pulsar Catalogue
# ---------------------------------------------------------------------------

ATNF_URL = "https://www.atnf.csiro.au/research/pulsar/psrcat/proc_form.php"


def check_pulsar_catalog(ra, dec, radius_arcsec=60):
    """Query the ATNF Pulsar Catalogue for known pulsars near a position.

    Args:
        ra: Right ascension in degrees
        dec: Declination in degrees
        radius_arcsec: Search radius in arcseconds

    Returns:
        dict with match count and pulsar details
    """
    # ATNF uses a web form — we query via the custom interface
    # Convert to HMS/DMS for the circular search
    radius_deg = radius_arcsec / 3600.0

    # Use the ATNF psrcat web interface
    params = {
        "Name": "",
        "startUserDefined": "true",
        "c1_val": str(ra),
        "c2_val": str(dec),
        "c3_val": str(radius_deg),
        "c4_val": "",
        "sort_attr": "jname",
        "sort_order": "asc",
        "condition": "",
        "pulession": "short",
        "ephession": "short",
        "coords_unit": "raj/decj",
        "radius": str(radius_deg),
        "coords_1": str(ra),
        "coords_2": str(dec),
        "style": "Long+with+last+digit+error",
        "no_value": "*",
        "fsize": "3",
        "x_axis": "",
        "x_scale": "linear",
        "y_axis": "",
        "y_scale": "linear",
        "state": "query",
        "table_bottom.x": "40",
        "table_bottom.y": "4",
    }

    # Try the simpler VizieR approach for ATNF
    vizier_url = (
        f"https://vizier.cds.unistra.fr/viz-bin/votable?"
        f"-source=B/psr&"
        f"-c={ra}+{dec}&-c.rd={radius_arcsec}&-c.u=arcsec&"
        f"-out=PSR,RAJ,DECJ,P0,DM,Dist,S1400&"
        f"-out.max=20"
    )

    resp, err = safe_request("get", vizier_url, timeout=30)
    if err:
        return {
            "error": f"Pulsar catalog query failed: {err['error']}",
            "ra": ra, "dec": dec,
            "radius_arcsec": radius_arcsec,
            "hint": "VizieR may be temporarily down. Try again later."
        }

    # Parse VOTable response
    pulsars = _parse_votable_pulsars(resp.text)

    return {
        "n_matches": len(pulsars),
        "ra": ra, "dec": dec,
        "radius_arcsec": radius_arcsec,
        "pulsars": pulsars,
        "message": f"Found {len(pulsars)} known pulsar(s) within {radius_arcsec}\" of RA={ra}, Dec={dec}"
    }


def _parse_votable_pulsars(xml_text):
    """Extract pulsar data from VizieR VOTable XML response."""
    pulsars = []
    try:
        # Simple regex extraction from VOTable XML
        # Look for <TR> rows with <TD> cells
        rows = re.findall(r'<TR>(.*?)</TR>', xml_text, re.DOTALL)
        for row in rows:
            cells = re.findall(r'<TD>(.*?)</TD>', row, re.DOTALL)
            if len(cells) >= 4:
                pulsar = {
                    "name": cells[0].strip() if len(cells) > 0 else "",
                    "raj": cells[1].strip() if len(cells) > 1 else "",
                    "decj": cells[2].strip() if len(cells) > 2 else "",
                    "period_s": cells[3].strip() if len(cells) > 3 else "",
                    "dm": cells[4].strip() if len(cells) > 4 else "",
                    "distance_kpc": cells[5].strip() if len(cells) > 5 else "",
                    "flux_1400MHz_mJy": cells[6].strip() if len(cells) > 6 else "",
                }
                # Only include if name looks like a pulsar
                if pulsar["name"] and (pulsar["name"].startswith("J") or pulsar["name"].startswith("B")):
                    pulsars.append(pulsar)
    except Exception as e:
        pass  # Return empty list on parse failure

    return pulsars


# ---------------------------------------------------------------------------
# FRB Catalog (TNS + FRBcat)
# ---------------------------------------------------------------------------

FRBSTATS_API = "https://www.frbstats.org/api/repeaters"
TNS_SEARCH = "https://www.wis-tns.org/search"


def check_frb_catalog(ra, dec, radius_arcsec=60):
    """Check coordinates against known FRB catalogs.

    Queries:
    1. VizieR FRB catalog (Petroff+ 2016, FRBCAT)
    2. Transient Name Server (TNS) for FRB-classified events

    Args:
        ra: Right ascension in degrees
        dec: Declination in degrees
        radius_arcsec: Search radius in arcseconds

    Returns:
        dict with match count and FRB details
    """
    frbs = []

    # 1. Query VizieR for FRB catalog
    vizier_url = (
        f"https://vizier.cds.unistra.fr/viz-bin/votable?"
        f"-source=J/PASA/33/e045/frbcat&"
        f"-c={ra}+{dec}&-c.rd={radius_arcsec}&-c.u=arcsec&"
        f"-out=Name,RAJ2000,DEJ2000,DM,z,Repeater,Tel&"
        f"-out.max=20"
    )

    resp, err = safe_request("get", vizier_url, timeout=30)
    if not err:
        frbs.extend(_parse_votable_frbs(resp.text))

    # 2. Also try the more recent CHIME/FRB catalog on VizieR
    chime_url = (
        f"https://vizier.cds.unistra.fr/viz-bin/votable?"
        f"-source=J/ApJS/257/59/catalog&"
        f"-c={ra}+{dec}&-c.rd={radius_arcsec}&-c.u=arcsec&"
        f"-out=FRB,RAJ2000,DEJ2000,DM,SNR&"
        f"-out.max=20"
    )

    resp2, err2 = safe_request("get", chime_url, timeout=30)
    if not err2:
        chime_frbs = _parse_votable_frbs(resp2.text, name_field="FRB")
        frbs.extend(chime_frbs)

    return {
        "n_matches": len(frbs),
        "ra": ra, "dec": dec,
        "radius_arcsec": radius_arcsec,
        "frbs": frbs,
        "catalogs_checked": ["FRBCAT (Petroff+2016)", "CHIME/FRB Catalog 1"],
        "message": f"Found {len(frbs)} known FRB(s) within {radius_arcsec}\" of RA={ra}, Dec={dec}"
    }


def _parse_votable_frbs(xml_text, name_field="Name"):
    """Extract FRB data from VizieR VOTable XML response."""
    frbs = []
    try:
        rows = re.findall(r'<TR>(.*?)</TR>', xml_text, re.DOTALL)
        for row in rows:
            cells = re.findall(r'<TD>(.*?)</TD>', row, re.DOTALL)
            if len(cells) >= 3:
                frb = {
                    "name": cells[0].strip(),
                    "ra": cells[1].strip(),
                    "dec": cells[2].strip(),
                    "dm": cells[3].strip() if len(cells) > 3 else "",
                }
                if len(cells) > 4:
                    frb["extra"] = cells[4].strip()
                if len(cells) > 5:
                    frb["extra2"] = cells[5].strip()
                if frb["name"]:
                    frbs.append(frb)
    except Exception:
        pass
    return frbs


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Radio Astronomy Query Tools")
    sub = parser.add_subparsers(dest="command")

    # download-radio
    p_dl = sub.add_parser("download-radio", help="Download radio survey cutout")
    p_dl.add_argument("--ra", type=float, required=True, help="RA in degrees")
    p_dl.add_argument("--dec", type=float, required=True, help="Dec in degrees")
    p_dl.add_argument("--radius", type=float, default=5.0, help="Radius in arcmin")
    p_dl.add_argument("--survey", default="vlass", help="Survey: vlass, first, nvss, lofar")

    # check-pulsar
    p_psr = sub.add_parser("check-pulsar", help="Check ATNF Pulsar Catalogue")
    p_psr.add_argument("--ra", type=float, required=True, help="RA in degrees")
    p_psr.add_argument("--dec", type=float, required=True, help="Dec in degrees")
    p_psr.add_argument("--radius", type=float, default=60.0, help="Radius in arcsec")

    # check-frb
    p_frb = sub.add_parser("check-frb", help="Check FRB catalogs (FRBCAT + CHIME)")
    p_frb.add_argument("--ra", type=float, required=True, help="RA in degrees")
    p_frb.add_argument("--dec", type=float, required=True, help="Dec in degrees")
    p_frb.add_argument("--radius", type=float, default=60.0, help="Radius in arcsec")

    args = parser.parse_args()

    if args.command == "download-radio":
        result = download_radio_cutout(args.ra, args.dec, args.radius, args.survey)
    elif args.command == "check-pulsar":
        result = check_pulsar_catalog(args.ra, args.dec, args.radius)
    elif args.command == "check-frb":
        result = check_frb_catalog(args.ra, args.dec, args.radius)
    else:
        parser.print_help()
        result = {"error": "No command specified. Use: download-radio, check-pulsar, check-frb"}

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
