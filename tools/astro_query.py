"""
Astronomical archive query tools.
Searches MAST (JWST, Hubble), ZTF, SIMBAD, Gaia, and ALeRCE for observations.

Usage:
    python tools/astro_query.py search-region --ra 150.0 --dec 2.0 --radius 0.1
    python tools/astro_query.py search-target "Crab Nebula"
    python tools/astro_query.py multi-epoch --ra 150.0 --dec 2.0 --radius 0.05
    python tools/astro_query.py ztf-lightcurve --ra 150.0 --dec 2.0 --radius 5
    python tools/astro_query.py simbad-check --ra 150.0 --dec 2.0 --radius 10
    python tools/astro_query.py query-gaia --ra 83.633 --dec 22.015 --radius 10
    python tools/astro_query.py check-transients --ra 150.0 --dec 30.0 --radius 5

All outputs go to stdout as JSON for the orchestrator to parse.
"""

import os
import sys
import json
import argparse
import re
import requests
from datetime import datetime

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

def save_result(subdir, filename, data):
    """Save query result to data/<subdir>/<filename> for local persistence."""
    out_dir = os.path.join(DATA_DIR, subdir)
    os.makedirs(out_dir, exist_ok=True)
    fpath = os.path.join(out_dir, filename)
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    return fpath

# ---------------------------------------------------------------------------
# MAST (Mikulski Archive for Space Telescopes) — JWST + Hubble
# ---------------------------------------------------------------------------

MAST_API = "https://mast.stsci.edu/api/v0/invoke"

def mast_query(params):
    """Send a query to MAST API and return results."""
    headers = {"Content-type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    try:
        resp = requests.post(MAST_API, data=params, headers=headers, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


def search_mast_region(ra, dec, radius_deg, missions=None):
    """Search MAST for observations at a given sky position.
    
    Args:
        ra: Right Ascension in degrees
        dec: Declination in degrees
        radius_deg: Search radius in degrees
        missions: List of missions to filter (e.g. ['JWST', 'HST'])
    """
    # Use the filtered cone search
    params = {
        "request": json.dumps({
            "service": "Mast.Caom.Cone",
            "params": {
                "ra": ra,
                "dec": dec,
                "radius": radius_deg
            },
            "format": "json",
            "pagesize": 100,
            "page": 1
        })
    }
    
    result = mast_query(params)
    
    if "error" in result:
        return result
    
    # Parse results
    observations = []
    if "data" in result:
        for obs in result["data"]:
            entry = {
                "obs_id": obs.get("obs_id", ""),
                "target_name": obs.get("target_name", ""),
                "mission": obs.get("obs_collection", ""),
                "instrument": obs.get("instrument_name", ""),
                "filters": obs.get("filters", ""),
                "wavelength": obs.get("em_min", ""),
                "date_obs": obs.get("t_min", ""),
                "date_end": obs.get("t_max", ""),
                "exposure_time": obs.get("t_exptime", ""),
                "ra": obs.get("s_ra", ""),
                "dec": obs.get("s_dec", ""),
                "dataproduct_type": obs.get("dataproduct_type", ""),
                "calib_level": obs.get("calib_level", ""),
            }
            
            # Filter by mission if specified
            if missions:
                if entry["mission"] in missions:
                    observations.append(entry)
            else:
                observations.append(entry)
    
    result = {
        "source": "MAST",
        "query": {"ra": ra, "dec": dec, "radius_deg": radius_deg},
        "total_results": len(observations),
        "observations": observations[:50],  # limit output
    }
    # Save locally
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_result("mast", f"search_{ra:.4f}_{dec:.4f}_{ts}.json", result)
    return result


def search_mast_target(target_name):
    """Resolve a target name and search MAST for observations."""
    # First resolve the name
    resolve_params = {
        "request": json.dumps({
            "service": "Mast.Name.Lookup",
            "params": {"input": target_name, "format": "json"}
        })
    }
    
    resolve = mast_query(resolve_params)
    
    if "error" in resolve:
        return resolve
    
    if "resolvedCoordinate" not in resolve or not resolve["resolvedCoordinate"]:
        return {"error": f"Could not resolve target name: {target_name}"}
    
    coord = resolve["resolvedCoordinate"][0]
    ra = coord.get("ra", 0)
    dec = coord.get("decl", 0)
    
    result = search_mast_region(ra, dec, 0.05)
    result["resolved_name"] = target_name
    result["resolved_ra"] = ra
    result["resolved_dec"] = dec
    
    # Save locally
    safe_name = target_name.replace(" ", "_").replace("/", "_")[:30]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_result("mast", f"target_{safe_name}_{ts}.json", result)
    return result


def find_multi_epoch(ra, dec, radius_deg=0.02):
    """Find observations of the same region at multiple epochs.
    
    This is key for anomaly detection: we need the same patch of sky
    observed at different times to spot changes.
    """
    result = search_mast_region(ra, dec, radius_deg)
    
    if "error" in result:
        return result
    
    # Group by instrument+filter to find comparable observations
    groups = {}
    for obs in result.get("observations", []):
        key = f"{obs['mission']}_{obs['instrument']}_{obs['filters']}"
        if key not in groups:
            groups[key] = []
        groups[key].append(obs)
    
    # Find groups with multiple epochs
    multi_epoch = {}
    for key, obs_list in groups.items():
        if len(obs_list) >= 2:
            # Sort by date
            obs_list.sort(key=lambda x: str(x.get("date_obs", "")))
            multi_epoch[key] = {
                "n_epochs": len(obs_list),
                "date_range": f"{obs_list[0].get('date_obs', '?')} to {obs_list[-1].get('date_obs', '?')}",
                "observations": obs_list
            }
    
    me_result = {
        "source": "MAST",
        "query": {"ra": ra, "dec": dec, "radius_deg": radius_deg},
        "multi_epoch_groups": multi_epoch,
        "n_groups": len(multi_epoch),
        "total_observations": result["total_results"],
    }
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_result("multi_epoch", f"me_{ra:.4f}_{dec:.4f}_{ts}.json", me_result)
    return me_result

# ---------------------------------------------------------------------------
# ZTF (Zwicky Transient Facility) via IRSA
# ---------------------------------------------------------------------------

ZTF_LIGHTCURVE_API = "https://irsa.ipac.caltech.edu/cgi-bin/ZTF/nph_light_curves"

def ztf_lightcurve(ra, dec, radius_arcsec=5.0):
    """Query ZTF for light curve data at a position.
    
    ZTF is ideal for finding transients: it surveys the northern sky
    every ~2 days in g, r, and i bands.
    
    Args:
        ra: Right Ascension in degrees
        dec: Declination in degrees
        radius_arcsec: Search radius in arcseconds
    """
    # Convert arcsec to degrees for API; cap at 0.1667 deg (API limit)
    radius_deg = min(radius_arcsec / 3600.0, 0.1667)
    
    params = {
        "POS": f"CIRCLE {ra} {dec} {radius_deg}",
        "FORMAT": "CSV",
        "BAD_CATFLAGS_MASK": "32768",
        "NOBS_MIN": "3",
    }
    
    # Single request with generous timeout
    try:
        resp = requests.get(ZTF_LIGHTCURVE_API, params=params, timeout=20)
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        return {"source": "ZTF", "error": "IRSA timeout (20s) — server may be busy, try again later"}
    except requests.exceptions.HTTPError as e:
        return {"source": "ZTF", "error": f"HTTP {e.response.status_code}: {e.response.text[:200]}"}
    except Exception as e:
        return {"source": "ZTF", "error": str(e)}
    
    try:
        text = resp.text.strip()
        
        if not text or "no data" in text.lower() or len(text) < 50:
            return {
                "source": "ZTF",
                "query": {"ra": ra, "dec": dec, "radius_arcsec": radius_arcsec},
                "total_points": 0,
                "message": "No ZTF data found at this position",
            }
        
        # Parse CSV response
        lines = text.split("\n")
        # Skip comment/metadata lines (start with \ or #)
        data_lines = [l.strip() for l in lines if l.strip() and not l.startswith("\\") and not l.startswith("#")]
        
        if len(data_lines) < 2:
            return {
                "source": "ZTF",
                "query": {"ra": ra, "dec": dec, "radius_arcsec": radius_arcsec},
                "total_points": 0,
                "message": "No ZTF data rows found",
            }
        
        headers = [h.strip() for h in data_lines[0].split(",")]
        data = []
        for row_str in data_lines[1:]:
            vals = [v.strip() for v in row_str.split(",")]
            if len(vals) == len(headers):
                entry = {}
                for h, v in zip(headers, vals):
                    try:
                        entry[h] = float(v)
                    except ValueError:
                        entry[h] = v
                data.append(entry)
    
    except requests.exceptions.HTTPError as e:
        return {"source": "ZTF", "error": f"HTTP {e.response.status_code}: {e.response.text[:200]}"}
    except Exception as e:
        return {"source": "ZTF", "error": str(e)}
    
    # Parse light curve data
    if data:
        # Compute basic statistics per band
        bands = {}
        for point in data:
            band = point.get("filtercode", point.get("filter", "unknown"))
            if band not in bands:
                bands[band] = {"magnitudes": [], "dates": [], "n_points": 0}
            mag = point.get("mag", point.get("psfmag", None))
            hjd = point.get("hjd", point.get("mjd", None))
            if mag is not None:
                bands[band]["magnitudes"].append(float(mag))
            if hjd is not None:
                bands[band]["dates"].append(float(hjd))
            bands[band]["n_points"] += 1
        
        # Compute variability stats
        stats = {}
        for band, info in bands.items():
            mags = info["magnitudes"]
            if mags:
                stats[band] = {
                    "n_points": info["n_points"],
                    "mag_mean": round(sum(mags) / len(mags), 3),
                    "mag_min": round(min(mags), 3),
                    "mag_max": round(max(mags), 3),
                    "mag_range": round(max(mags) - min(mags), 3),
                    "date_range_days": round((max(info["dates"]) - min(info["dates"])), 1) if info["dates"] else 0,
                }
        
        ztf_result = {
            "source": "ZTF",
            "query": {"ra": ra, "dec": dec, "radius_arcsec": radius_arcsec},
            "total_points": len(data),
            "bands": stats,
            "variability_flag": any(s["mag_range"] > 0.5 for s in stats.values()),
        }
        # Save raw data + stats locally
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        save_result("ztf", f"ztf_{ra:.4f}_{dec:.4f}_{ts}.json", {
            "stats": ztf_result,
            "raw_points": data[:500],  # save up to 500 data points
        })
        return ztf_result
    
    return {
        "source": "ZTF",
        "query": {"ra": ra, "dec": dec, "radius_arcsec": radius_arcsec},
        "total_points": 0,
        "message": "No ZTF data found at this position",
    }

# ---------------------------------------------------------------------------
# SIMBAD — Known object catalog
# ---------------------------------------------------------------------------

SIMBAD_TAP = "https://simbad.cds.unistra.fr/simbad/sim-tap/sync"

def simbad_check(ra, dec, radius_arcsec=10.0):
    """Check SIMBAD for known objects near a position.
    
    This is used to cross-reference: if we detect something,
    is it already known or potentially new?
    """
    # Use TAP/ADQL query  
    radius_deg = radius_arcsec / 3600.0
    query = f"""
    SELECT TOP 20
        main_id, ra, dec, otype_txt, sp_type,
        DISTANCE(POINT('ICRS', ra, dec), POINT('ICRS', {ra}, {dec})) AS dist_deg
    FROM basic
    WHERE 1=CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', {ra}, {dec}, {radius_deg}))
    ORDER BY dist_deg ASC
    """
    
    params = {
        "request": "doQuery",
        "lang": "adql",
        "format": "json",
        "query": query,
    }
    
    try:
        resp = requests.get(SIMBAD_TAP, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return {"source": "SIMBAD", "error": str(e)}
    
    # Parse results
    objects = []
    if "data" in data:
        columns = [col["name"] for col in data.get("metadata", [])]
        for row in data["data"]:
            obj = dict(zip(columns, row))
            objects.append({
                "name": obj.get("main_id", ""),
                "type": obj.get("otype_txt", ""),
                "ra": obj.get("ra", ""),
                "dec": obj.get("dec", ""),
                "spectral_type": obj.get("sp_type", ""),
                
                "distance_arcsec": round(obj.get("dist_deg", 0) * 3600, 2),
            })
    
    sim_result = {
        "source": "SIMBAD",
        "query": {"ra": ra, "dec": dec, "radius_arcsec": radius_arcsec},
        "n_known_objects": len(objects),
        "objects": objects,
    }
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_result("simbad", f"simbad_{ra:.4f}_{dec:.4f}_{ts}.json", sim_result)
    return sim_result

# ---------------------------------------------------------------------------
# Image cutout download
# ---------------------------------------------------------------------------

def detect_file_format(data_bytes):
    """Detect actual file format from magic bytes."""
    if data_bytes[:2] == b'\xff\xd8':
        return "jpeg"
    if data_bytes[:6] in (b'SIMPLE', b'XTENS'):
        return "fits"
    if data_bytes[:8] == b'\x89PNG\r\n\x1a\n':
        return "png"
    if data_bytes[:3] == b'GIF':
        return "gif"
    return "unknown"








def download_cutout(ra, dec, size_arcmin=1.0, survey="panstarrs"):
    """Download image cutouts from Pan-STARRS survey.

    Detects actual file format (FITS vs JPEG) and saves with correct extension.
    Pan-STARRS fitscut.cgi often returns JPEG even when FITS is requested.
    """
    os.makedirs(os.path.join(DATA_DIR, "images"), exist_ok=True)

    if survey == "panstarrs":
        size_pix = int(size_arcmin * 60 / 0.25)  # 0.25 arcsec/pixel
        size_pix = min(size_pix, 1920)  # max 1920 pixels

        # Strategy: Try the PS1 filename API first for real FITS, fall back to fitscut
        downloaded = []

        # ----- Approach 1: ps1filenames.py → fitscut.cgi with explicit FITS format -----
        try:
            filenames_url = (
                f"https://ps1images.stsci.edu/cgi-bin/ps1filenames.py"
                f"?ra={ra}&dec={dec}&filters=grizy&type=stack"
            )
            resp = requests.get(filenames_url, timeout=15)
            resp.raise_for_status()
            lines = resp.text.strip().split("\n")

            if len(lines) > 1:
                # Parse the table (header + data rows)
                header_cols = lines[0].split()
                for line in lines[1:]:
                    cols = line.split()
                    if len(cols) < 8:
                        continue

                    # Build fitscut URL with explicit format=fits
                    filename = cols[7] if len(cols) > 7 else ""
                    shortname = cols[8] if len(cols) > 8 else ""
                    filter_name = cols[4] if len(cols) > 4 else f"band{len(downloaded)}"

                    if not filename:
                        continue

                    cutout_url = (
                        f"https://ps1images.stsci.edu/cgi-bin/fitscut.cgi"
                        f"?ra={ra}&dec={dec}&size={size_pix}"
                        f"&format=fits&red={filename}"
                    )

                    fname_base = f"cutout_{ra:.4f}_{dec:.4f}_{filter_name}"
                    fpath_fits = os.path.join(DATA_DIR, "images", f"{fname_base}.fits")
                    fpath_jpg = os.path.join(DATA_DIR, "images", f"{fname_base}.jpg")

                    try:
                        img_resp = requests.get(cutout_url, timeout=20)
                        if img_resp.status_code == 200 and len(img_resp.content) > 100:
                            # Detect actual format
                            fmt = detect_file_format(img_resp.content)

                            if fmt == "fits":
                                fpath = fpath_fits
                                ext = ".fits"
                            elif fmt == "jpeg":
                                fpath = fpath_jpg
                                ext = ".jpg"
                                # Remove stale .fits version if exists
                                if os.path.exists(fpath_fits):
                                    os.remove(fpath_fits)
                            else:
                                # Unknown format, save as-is with .dat
                                fpath = os.path.join(DATA_DIR, "images", f"{fname_base}.dat")
                                ext = ".dat"

                            with open(fpath, "wb") as f:
                                f.write(img_resp.content)

                            downloaded.append({
                                "file": fpath,
                                "filter": filter_name,
                                "size_bytes": len(img_resp.content),
                                "format": fmt,
                            })
                    except Exception:
                        continue

                    if len(downloaded) >= 5:
                        break

        except Exception as e:
            pass  # Fall through to approach 2

        # ----- Approach 2: Fallback to ps1cutouts HTML scraping -----
        if not downloaded:
            try:
                search_url = (
                    f"https://ps1images.stsci.edu/cgi-bin/ps1cutouts"
                    f"?pos={ra}+{dec}&filter=grizy&filetypes=stack&size={size_pix}"
                )
                resp = requests.get(search_url, timeout=15)
                resp.raise_for_status()
                html = resp.text

                fits_urls = re.findall(
                    r'(//ps1images\.stsci\.edu/cgi-bin/fitscut\.cgi\?[^"\s]+)',
                    html
                )

                for url in fits_urls[:5]:
                    if url.startswith("//"):
                        url = "https:" + url

                    # Force format=fits in URL
                    url = re.sub(r'format=[a-z]+', 'format=fits', url)
                    if 'format=' not in url:
                        url += '&format=fits'

                    filter_match = re.search(r'stk\.([grizy])\.', url)
                    filter_name = filter_match.group(1) if filter_match else f"band{len(downloaded)}"

                    fname_base = f"cutout_{ra:.4f}_{dec:.4f}_{filter_name}"

                    try:
                        img_resp = requests.get(url, timeout=20)
                        if img_resp.status_code == 200 and len(img_resp.content) > 100:
                            fmt = detect_file_format(img_resp.content)

                            if fmt == "fits":
                                fpath = os.path.join(DATA_DIR, "images", f"{fname_base}.fits")
                            elif fmt == "jpeg":
                                fpath = os.path.join(DATA_DIR, "images", f"{fname_base}.jpg")
                            else:
                                fpath = os.path.join(DATA_DIR, "images", f"{fname_base}.dat")

                            with open(fpath, "wb") as f:
                                f.write(img_resp.content)

                            downloaded.append({
                                "file": fpath,
                                "filter": filter_name,
                                "size_bytes": len(img_resp.content),
                                "format": fmt,
                            })
                    except Exception:
                        continue

            except Exception as e:
                return {"source": "Pan-STARRS", "error": str(e)}

        return {
            "source": "Pan-STARRS",
            "query": {"ra": ra, "dec": dec, "size_arcmin": size_arcmin},
            "downloaded": downloaded,
            "n_images": len(downloaded),
            "files": [d["file"] for d in downloaded],
            "formats": list(set(d["format"] for d in downloaded)),
        }

    return {"error": f"Unknown survey: {survey}"}


# ---------------------------------------------------------------------------
# Multi-epoch warp images (Pan-STARRS individual exposures)
# ---------------------------------------------------------------------------

def mjd_to_iso(mjd):
    """Convert Modified Julian Date to ISO date string (YYYY-MM-DD)."""
    from datetime import datetime as _dt, timedelta
    # MJD epoch: 1858-11-17T00:00:00
    mjd_epoch = _dt(1858, 11, 17)
    dt = mjd_epoch + timedelta(days=float(mjd))
    return dt.strftime("%Y-%m-%d")


def download_multiepoch(ra, dec, filter_name='g', n_epochs=3, size_arcmin=1.0):
    """Download multiple epoch images from Pan-STARRS warps for temporal comparison.

    Queries ps1filenames.py for individual warp (single-exposure) images,
    selects N maximally time-spread epochs, and downloads FITS cutouts.

    Args:
        ra: Right Ascension in degrees
        dec: Declination in degrees
        filter_name: Single filter (g, r, i, z, y). Default 'g'.
        n_epochs: Number of epochs to download (2-6). Default 3.
        size_arcmin: Cutout size in arcminutes. Default 1.0.

    Returns:
        dict with downloaded files, MJD dates, time baseline info.
    """
    import time as _time

    # Validate inputs
    filter_name = str(filter_name).lower().strip()
    if filter_name not in ('g', 'r', 'i', 'z', 'y'):
        return {"error": f"Invalid filter '{filter_name}'. Must be one of: g, r, i, z, y"}
    n_epochs = max(2, min(6, int(n_epochs)))
    size_pix = int(size_arcmin * 60 / 0.25)  # 0.25 arcsec/pixel
    size_pix = min(size_pix, 1920)

    os.makedirs(os.path.join(DATA_DIR, "images"), exist_ok=True)

    # Step 1: Query available warp images
    filenames_url = (
        f"https://ps1images.stsci.edu/cgi-bin/ps1filenames.py"
        f"?ra={ra}&dec={dec}&filters={filter_name}&type=warp"
    )
    try:
        resp = requests.get(filenames_url, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        return {"error": f"Failed to query PS1 warp catalog: {e}"}

    lines = resp.text.strip().split("\n")
    if len(lines) < 2:
        return {
            "error": "No Pan-STARRS warp images found at this position. "
                     "The region may lack warp coverage (Dec must be > -30°).",
            "query": {"ra": ra, "dec": dec, "filter": filter_name},
        }

    # Step 2: Parse warp list — extract MJD and filename
    warps = []
    for line in lines[1:]:
        cols = line.split()
        if len(cols) < 8:
            continue
        try:
            mjd = float(cols[5])
        except (ValueError, IndexError):
            continue
        filename = cols[7] if len(cols) > 7 else ""
        if filename and mjd > 0:
            warps.append({"mjd": mjd, "filename": filename, "filter": filter_name})

    if not warps:
        return {
            "error": f"No warp images found for filter={filter_name} at RA={ra}, Dec={dec}.",
            "query": {"ra": ra, "dec": dec, "filter": filter_name},
        }

    # Step 3: Smart epoch selection — pick N maximally time-spread epochs
    warps.sort(key=lambda w: w["mjd"])

    if len(warps) <= n_epochs:
        selected = list(warps)
    else:
        # Greedy max-spread: start with first and last, fill widest gaps
        selected = [warps[0], warps[-1]]
        remaining = list(warps[1:-1])

        while len(selected) < n_epochs and remaining:
            selected.sort(key=lambda w: w["mjd"])
            best_insert = None
            max_gap = 0
            for i in range(len(selected) - 1):
                gap = selected[i + 1]["mjd"] - selected[i]["mjd"]
                if gap > max_gap:
                    max_gap = gap
                    midpoint = (selected[i]["mjd"] + selected[i + 1]["mjd"]) / 2
                    closest = min(remaining, key=lambda w: abs(w["mjd"] - midpoint))
                    best_insert = closest
            if best_insert:
                selected.append(best_insert)
                remaining.remove(best_insert)
            else:
                break

        selected.sort(key=lambda w: w["mjd"])

    # Step 4: Download each selected warp cutout
    downloaded = []
    for idx, warp in enumerate(selected, start=1):
        cutout_url = (
            f"https://ps1images.stsci.edu/cgi-bin/fitscut.cgi"
            f"?ra={ra}&dec={dec}&size={size_pix}"
            f"&format=fits&red={warp['filename']}"
        )

        fname = f"warp_{ra:.4f}_{dec:.4f}_{filter_name}_ep{idx}_mjd{warp['mjd']:.1f}.fits"
        fpath = os.path.join(DATA_DIR, "images", fname)

        try:
            img_resp = requests.get(cutout_url, timeout=25)
            if img_resp.status_code == 200 and len(img_resp.content) > 100:
                fmt = detect_file_format(img_resp.content)
                # If server returns JPEG instead of FITS, save with correct extension
                if fmt == "jpeg":
                    fname = fname.replace(".fits", ".jpg")
                    fpath = os.path.join(DATA_DIR, "images", fname)

                with open(fpath, "wb") as f:
                    f.write(img_resp.content)

                # Post-download NaN check: reject blank FITS images
                nan_frac = 0.0
                if fmt == "fits":
                    try:
                        import numpy as _np
                        from astropy.io import fits as _fits
                        with _fits.open(fpath) as _hdul:
                            for _hdu in _hdul:
                                if _hdu.data is not None and _hdu.data.ndim >= 2:
                                    nan_frac = _np.count_nonzero(_np.isnan(_hdu.data)) / _hdu.data.size
                                    break
                    except Exception:
                        pass

                if nan_frac > 0.5:
                    # Delete blank image and skip this epoch
                    try:
                        os.remove(fpath)
                    except OSError:
                        pass
                    continue

                downloaded.append({
                    "file": os.path.join("data", "images", fname),
                    "filter": filter_name,
                    "epoch": idx,
                    "mjd": round(warp["mjd"], 2),
                    "date_iso": mjd_to_iso(warp["mjd"]),
                    "size_bytes": len(img_resp.content),
                    "format": fmt,
                    "nan_fraction": round(nan_frac, 3),
                })
        except Exception as e:
            # Skip failed downloads, continue with remaining epochs
            pass

        # Rate-limit: 0.5s between requests
        if idx < len(selected):
            _time.sleep(0.5)

    if not downloaded:
        return {
            "error": (
                "No usable warp images at this position. "
                "All downloaded warps were blank (100% NaN) — Pan-STARRS has no valid "
                f"coverage for filter={filter_name} at RA={ra}, Dec={dec}. "
                "Try a different filter or a position with better coverage (Dec > 0°)."
            ),
            "query": {"ra": ra, "dec": dec, "filter": filter_name},
            "total_available_epochs": len(warps),
        }

    # Compute time baseline
    mjds = [d["mjd"] for d in downloaded]
    baseline_days = round(max(mjds) - min(mjds), 1) if len(mjds) > 1 else 0

    note = ""
    if len(downloaded) < n_epochs:
        note = f"Only {len(downloaded)} of {n_epochs} requested epochs downloaded successfully. "
    if len(warps) < n_epochs:
        note += f"Only {len(warps)} warp(s) available for filter={filter_name} at this position."

    return {
        "source": "Pan-STARRS Warps",
        "query": {"ra": ra, "dec": dec, "filter": filter_name, "n_epochs": n_epochs},
        "downloaded": downloaded,
        "n_images": len(downloaded),
        "files": [d["file"] for d in downloaded],
        "total_available_epochs": len(warps),
        "time_baseline_days": baseline_days,
        "epoch_summary": (
            f"{len(downloaded)} epochs spanning {downloaded[0]['date_iso']} "
            f"to {downloaded[-1]['date_iso']} ({baseline_days} days)"
        ),
        "note": note or "Compare same-band warp files with compare_images to detect real transients!",
        "tip": (
            f"Now compare these images: compare_images("
            f"img1='{downloaded[0]['file']}', "
            f"img2='{downloaded[-1]['file']}')"
        ),
    }


# ---------------------------------------------------------------------------
# Legacy Survey (DECaLS) cutout download
# ---------------------------------------------------------------------------

def download_legacy(ra, dec, bands='grz', size_pix=256):
    """Download cutout from DESI Legacy Imaging Survey DR10.

    Independent survey from Pan-STARRS with ~5-year baseline.
    Compare same-band Legacy vs Pan-STARRS images for cross-survey
    transient verification.

    Args:
        ra: Right Ascension in degrees
        dec: Declination in degrees
        bands: Band string (combination of g, r, i, z). Default 'grz'.
        size_pix: Image size in pixels (max 512). Default 256.

    Returns:
        dict with downloaded files info.
    """
    # Validate inputs
    bands = str(bands).lower().strip()
    valid_bands = set('griz')
    bands = ''.join(b for b in bands if b in valid_bands)
    if not bands:
        bands = 'grz'
    size_pix = max(64, min(512, int(size_pix)))

    os.makedirs(os.path.join(DATA_DIR, "images"), exist_ok=True)

    # Coverage warning
    coverage_warn = ""
    if dec < -20 or dec > 85:
        coverage_warn = (
            f"Position Dec={dec:.1f}° is likely outside Legacy Survey coverage "
            f"(-18° < Dec < +84°). Download may return empty data."
        )

    # Download one band at a time for clean per-band files
    downloaded = []

    for band in bands:
        url = (
            f"https://www.legacysurvey.org/viewer/cutout.fits"
            f"?ra={ra}&dec={dec}&layer=ls-dr10"
            f"&pixscale=0.262&bands={band}&size={size_pix}"
        )

        fname = f"legacy_{ra:.4f}_{dec:.4f}_{band}.fits"
        fpath = os.path.join(DATA_DIR, "images", fname)

        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code == 200 and len(resp.content) > 200:
                fmt = detect_file_format(resp.content)
                if fmt == "jpeg":
                    fname = fname.replace(".fits", ".jpg")
                    fpath = os.path.join(DATA_DIR, "images", fname)

                with open(fpath, "wb") as f:
                    f.write(resp.content)

                downloaded.append({
                    "file": os.path.join("data", "images", fname),
                    "filter": band,
                    "survey": "Legacy Survey DR10",
                    "size_bytes": len(resp.content),
                    "format": fmt,
                })
            elif resp.status_code == 404:
                # No coverage at this position for this band
                pass
            else:
                pass
        except requests.exceptions.Timeout:
            pass
        except Exception:
            pass

    if not downloaded:
        error_msg = "No Legacy Survey data at this position."
        if coverage_warn:
            error_msg += f" {coverage_warn}"
        else:
            error_msg += " The position may be outside DR10 coverage (Dec roughly -18° to +84°, away from galactic plane)."
        return {
            "error": error_msg,
            "query": {"ra": ra, "dec": dec, "bands": bands},
        }

    return {
        "source": "Legacy Survey DR10",
        "query": {"ra": ra, "dec": dec, "bands": bands, "size_pix": size_pix},
        "downloaded": downloaded,
        "n_images": len(downloaded),
        "files": [d["file"] for d in downloaded],
        "note": (
            "Independent survey from Pan-STARRS (~5yr baseline). "
            "Compare same-band Legacy vs Pan-STARRS images to cross-check transient candidates."
        ),
        "warning": coverage_warn or None,
    }


def query_gaia(ra, dec, radius_arcsec=5):
    """Query Gaia DR3 catalog for sources at a position.

    Uses ESA's TAP sync endpoint with ADQL cone search. Returns parallax,
    proper motion, photometry, and variability classification for each source.

    Args:
        ra: Right Ascension in degrees
        dec: Declination in degrees
        radius_arcsec: Search radius in arcseconds (default 5)

    Returns:
        dict with Gaia sources and their properties.
    """
    radius_deg = float(radius_arcsec) / 3600.0

    adql = f"""SELECT TOP 50
    g.source_id, g.ra, g.dec,
    g.parallax, g.parallax_error,
    g.pmra, g.pmdec,
    g.phot_g_mean_mag, g.bp_rp, g.ruwe,
    g.radial_velocity,
    v.num_selected_g_fov, v.range_mag_g_fov,
    c.best_class_name, c.best_class_score
FROM gaiadr3.gaia_source AS g
LEFT OUTER JOIN gaiadr3.vari_summary AS v
    ON g.source_id = v.source_id
LEFT OUTER JOIN gaiadr3.vari_classifier_result AS c
    ON g.source_id = c.source_id
WHERE CONTAINS(
    POINT('ICRS', g.ra, g.dec),
    CIRCLE('ICRS', {ra}, {dec}, {radius_deg})
) = 1
ORDER BY g.phot_g_mean_mag ASC"""

    tap_url = "https://gea.esac.esa.int/tap-server/tap/sync"
    params = {
        "REQUEST": "doQuery",
        "LANG": "ADQL",
        "FORMAT": "votable",
        "QUERY": adql,
    }

    try:
        resp = requests.get(tap_url, params=params, timeout=15)
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        return {"error": "Gaia TAP query timed out (15s). Try a smaller radius."}
    except requests.exceptions.RequestException as e:
        return {"error": f"Gaia TAP query failed: {e}"}

    # Parse VOTable response
    try:
        from io import BytesIO
        from astropy.io.votable import parse as parse_votable
        import numpy as np

        vot = parse_votable(BytesIO(resp.content))
        table = vot.get_first_table()
        data = table.array

        if len(data) == 0:
            return {
                "source": "Gaia DR3",
                "query": {"ra": ra, "dec": dec, "radius_arcsec": radius_arcsec},
                "n_sources": 0,
                "sources": [],
                "note": "No Gaia sources within search radius.",
            }

        sources = []
        for row in data:
            # Calculate angular separation
            dra = (float(row["ra"]) - ra) * np.cos(np.radians(dec))
            ddec = float(row["dec"]) - dec
            sep_arcsec = round(np.sqrt(dra**2 + ddec**2) * 3600, 2)

            # Handle masked/null values
            def _val(x, ndigits=4):
                try:
                    import warnings
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        v = float(x)
                    if np.isnan(v) or np.isinf(v):
                        return None
                    return round(v, ndigits)
                except (ValueError, TypeError):
                    return None

            def _str(x):
                try:
                    s = str(x).strip()
                    return s if s and s != "--" and s != "" else None
                except Exception:
                    return None

            # Total proper motion
            pmra_val = _val(row["pmra"], 2)
            pmdec_val = _val(row["pmdec"], 2)
            pm_total = None
            if pmra_val is not None and pmdec_val is not None:
                pm_total = round(np.sqrt(pmra_val**2 + pmdec_val**2), 2)

            sources.append({
                "source_id": str(row["source_id"]),
                "ra": _val(row["ra"], 6),
                "dec": _val(row["dec"], 6),
                "parallax_mas": _val(row["parallax"], 3),
                "parallax_error": _val(row["parallax_error"], 3),
                "pmra": pmra_val,
                "pmdec": pmdec_val,
                "pm_total_mas_yr": pm_total,
                "phot_g_mean_mag": _val(row["phot_g_mean_mag"], 3),
                "bp_rp": _val(row["bp_rp"], 3),
                "ruwe": _val(row["ruwe"], 2),
                "radial_velocity": _val(row["radial_velocity"], 1),
                "variable_class": _str(row["best_class_name"]),
                "variable_score": _val(row["best_class_score"], 3),
                "n_var_observations": _val(row["num_selected_g_fov"], 0),
                "var_range_mag": _val(row["range_mag_g_fov"], 3),
                "distance_arcsec": sep_arcsec,
            })

        # Sort by distance
        sources.sort(key=lambda s: s["distance_arcsec"])

        return {
            "source": "Gaia DR3",
            "query": {"ra": ra, "dec": dec, "radius_arcsec": radius_arcsec},
            "n_sources": len(sources),
            "sources": sources,
            "note": (
                "High proper motion (pm_total > 50 mas/yr) = nearby moving star (not a transient). "
                "variable_class set = known variable star. "
                "parallax > 5 mas = within ~200 pc (likely stellar, not extragalactic transient)."
            ),
        }

    except Exception as e:
        return {"error": f"Failed to parse Gaia VOTable response: {e}"}


def check_transients(ra, dec, radius_arcsec=5):
    """Check ALeRCE/ZTF broker for known transients at a position.

    ALeRCE aggregates ZTF and ATLAS alerts with ML classification.
    No authentication required.

    Args:
        ra: Right Ascension in degrees
        dec: Declination in degrees
        radius_arcsec: Search radius in arcseconds (default 5)

    Returns:
        dict with known transient objects and their classifications.
    """
    base_url = "https://api.alerce.online/ztf/v1/objects"
    params = {
        "ra": float(ra),
        "dec": float(dec),
        "radius": float(radius_arcsec),
        "page_size": 10,
        "count": "false",
    }

    try:
        resp = requests.get(base_url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.Timeout:
        return {"error": "ALeRCE API timed out (15s). Try again later."}
    except requests.exceptions.RequestException as e:
        return {"error": f"ALeRCE API request failed: {e}"}
    except (ValueError, KeyError) as e:
        return {"error": f"ALeRCE API returned unexpected response: {e}"}

    items = data.get("items", [])
    if not items:
        return {
            "source": "ALeRCE (ZTF broker)",
            "query": {"ra": ra, "dec": dec, "radius_arcsec": radius_arcsec},
            "n_matches": 0,
            "objects": [],
            "note": "No known ZTF transients at this position — candidate may be novel!",
        }

    import numpy as np

    objects = []
    for obj in items:
        oid = obj.get("oid", "")

        # Fetch classification for this object
        class_name = None
        class_prob = None
        try:
            cls_url = f"https://api.alerce.online/ztf/v1/objects/{oid}/classifiers"
            cls_resp = requests.get(cls_url, timeout=10)
            if cls_resp.status_code == 200:
                cls_data = cls_resp.json()
                # Find the latest/best classifier
                for classifier_name, classes in cls_data.items():
                    if classes:
                        # Pick highest probability class
                        best = max(classes, key=lambda c: c.get("probability", 0))
                        class_name = best.get("class_name")
                        class_prob = round(best.get("probability", 0), 3)
                        break
        except Exception:
            pass  # Classification is optional

        # Angular separation
        obj_ra = obj.get("meanra", obj.get("ra"))
        obj_dec = obj.get("meandec", obj.get("dec"))
        sep = None
        if obj_ra is not None and obj_dec is not None:
            dra = (float(obj_ra) - ra) * np.cos(np.radians(dec))
            ddec = float(obj_dec) - dec
            sep = round(np.sqrt(dra**2 + ddec**2) * 3600, 2)

        objects.append({
            "oid": oid,
            "ra": round(float(obj_ra), 6) if obj_ra else None,
            "dec": round(float(obj_dec), 6) if obj_dec else None,
            "firstmjd": obj.get("firstmjd"),
            "lastmjd": obj.get("lastmjd"),
            "n_detections": obj.get("ndet"),
            "class_name": class_name,
            "class_probability": class_prob,
            "distance_arcsec": sep,
        })

    # Sort by distance
    objects.sort(key=lambda o: o.get("distance_arcsec") or 999)

    return {
        "source": "ALeRCE (ZTF broker)",
        "query": {"ra": ra, "dec": dec, "radius_arcsec": radius_arcsec},
        "n_matches": len(objects),
        "objects": objects,
        "note": (
            "Known ZTF transients at this position. "
            "class_name indicates ML classification: SN (supernova), AGN, VS (variable star), "
            "asteroid, bogus, etc. High class_probability = confident classification."
        ),
    }


def main():
    parser = argparse.ArgumentParser(description="Astronomical Archive Query Tools")
    sub = parser.add_subparsers(dest="command")
    
    # search-region
    p_region = sub.add_parser("search-region", help="Search MAST by coordinates")
    p_region.add_argument("--ra", type=float, required=True, help="RA in degrees")
    p_region.add_argument("--dec", type=float, required=True, help="Dec in degrees")
    p_region.add_argument("--radius", type=float, default=0.05, help="Radius in degrees")
    p_region.add_argument("--mission", nargs="*", help="Filter by mission (JWST, HST)")
    
    # search-target
    p_target = sub.add_parser("search-target", help="Search MAST by target name")
    p_target.add_argument("name", help="Target name (e.g., 'Crab Nebula')")
    
    # multi-epoch
    p_multi = sub.add_parser("multi-epoch", help="Find multi-epoch observations")
    p_multi.add_argument("--ra", type=float, required=True)
    p_multi.add_argument("--dec", type=float, required=True)
    p_multi.add_argument("--radius", type=float, default=0.02)
    
    # ztf-lightcurve
    p_ztf = sub.add_parser("ztf-lightcurve", help="Get ZTF light curve")
    p_ztf.add_argument("--ra", type=float, required=True)
    p_ztf.add_argument("--dec", type=float, required=True)
    p_ztf.add_argument("--radius", type=float, default=5.0, help="Radius in arcsec")
    
    # simbad-check
    p_sim = sub.add_parser("simbad-check", help="Check SIMBAD for known objects")
    p_sim.add_argument("--ra", type=float, required=True)
    p_sim.add_argument("--dec", type=float, required=True)
    p_sim.add_argument("--radius", type=float, default=10.0, help="Radius in arcsec")
    
    # download-cutout
    p_cut = sub.add_parser("download-cutout", help="Download image cutout")
    p_cut.add_argument("--ra", type=float, required=True)
    p_cut.add_argument("--dec", type=float, required=True)
    p_cut.add_argument("--size", type=float, default=1.0, help="Size in arcmin")

    # download-multiepoch
    p_mepoch = sub.add_parser("download-multiepoch", help="Download multi-epoch warp images from Pan-STARRS")
    p_mepoch.add_argument("--ra", type=float, required=True)
    p_mepoch.add_argument("--dec", type=float, required=True)
    p_mepoch.add_argument("--filter", type=str, default="g", help="Filter band (g/r/i/z/y)")
    p_mepoch.add_argument("--epochs", type=int, default=3, help="Number of epochs (2-6)")
    p_mepoch.add_argument("--size", type=float, default=1.0, help="Size in arcmin")

    # download-legacy
    p_legacy = sub.add_parser("download-legacy", help="Download Legacy Survey DR10 cutout")
    p_legacy.add_argument("--ra", type=float, required=True)
    p_legacy.add_argument("--dec", type=float, required=True)
    p_legacy.add_argument("--bands", type=str, default="grz", help="Bands to download (e.g., grz)")
    p_legacy.add_argument("--size", type=int, default=256, help="Size in pixels (max 512)")

    # query-gaia
    p_gaia = sub.add_parser("query-gaia", help="Query Gaia DR3 catalog")
    p_gaia.add_argument("--ra", type=float, required=True)
    p_gaia.add_argument("--dec", type=float, required=True)
    p_gaia.add_argument("--radius", type=float, default=5.0, help="Radius in arcsec")

    # check-transients
    p_trans = sub.add_parser("check-transients", help="Check ALeRCE/ZTF for known transients")
    p_trans.add_argument("--ra", type=float, required=True)
    p_trans.add_argument("--dec", type=float, required=True)
    p_trans.add_argument("--radius", type=float, default=5.0, help="Radius in arcsec")

    args = parser.parse_args()
    
    if args.command == "search-region":
        result = search_mast_region(args.ra, args.dec, args.radius, args.mission)
    elif args.command == "search-target":
        result = search_mast_target(args.name)
    elif args.command == "multi-epoch":
        result = find_multi_epoch(args.ra, args.dec, args.radius)
    elif args.command == "ztf-lightcurve":
        result = ztf_lightcurve(args.ra, args.dec, args.radius)
    elif args.command == "simbad-check":
        result = simbad_check(args.ra, args.dec, args.radius)
    elif args.command == "download-cutout":
        result = download_cutout(args.ra, args.dec, args.size)
    elif args.command == "download-multiepoch":
        result = download_multiepoch(args.ra, args.dec, args.filter, args.epochs, args.size)
    elif args.command == "download-legacy":
        result = download_legacy(args.ra, args.dec, args.bands, args.size)
    elif args.command == "query-gaia":
        result = query_gaia(args.ra, args.dec, args.radius)
    elif args.command == "check-transients":
        result = check_transients(args.ra, args.dec, args.radius)
    else:
        parser.print_help()
        return
    
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
