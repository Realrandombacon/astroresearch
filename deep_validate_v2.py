"""
Deep validation v2 — fixed API parsing for Pan-STARRS MAST, SDSS, Legacy Survey
"""
import json, sys, time, urllib.request, urllib.parse, ssl, re, csv, io

sys.stdout.reconfigure(encoding='utf-8')

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

STRONG = [
    {"id": "F20260311_005040", "ra": 244.723315, "dec": -19.256528, "sigma": 146.85,
     "desc": "Brightened from 15.6 to 2113 flux in g-band over 734 days"},
    {"id": "F20260311_014935", "ra": 80.18, "dec": -23.0, "sigma": 48.19,
     "desc": "r-band brightening by 6106 counts over 1367 days"},
    {"id": "F20260311_022856", "ra": 268.994306, "dec": 51.23059, "sigma": 124.47,
     "desc": "Flux 1.8 to 1472 in g-band over 716 days"},
    {"id": "F20260311_044554", "ra": 325.066417, "dec": 76.939167, "sigma": -234.88,
     "desc": "Extreme fading -11288 counts in 3 years"},
    {"id": "F20260311_072512", "ra": 303.21, "dec": 66.55, "sigma": 8.04,
     "desc": "5 sources brightened simultaneously, max sigma=8.04"},
]

def fetch(url, timeout=25, as_json=True):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'AstroResearch/1.0'})
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as resp:
            raw = resp.read().decode('utf-8', errors='replace')
        if as_json:
            return json.loads(raw)
        return raw
    except Exception as e:
        return {"_error": str(e)[:200]}

# ---- Pan-STARRS DR2 Mean catalog via MAST (CSV format for reliable parsing) ----
def ps_catalog(ra, dec, radius_arcsec=5):
    r_deg = radius_arcsec / 3600.0
    url = (f"https://catalogs.mast.stsci.edu/api/v0.1/panstarrs/dr2/mean.csv?"
           f"ra={ra}&dec={dec}&radius={r_deg}"
           f"&nDetections.gte=1"
           f"&columns=[objID,raMean,decMean,nDetections,gMeanPSFMag,rMeanPSFMag,iMeanPSFMag,zMeanPSFMag]"
           f"&pagesize=10")
    raw = fetch(url, as_json=False)
    if isinstance(raw, dict) and '_error' in raw:
        return raw
    if not isinstance(raw, str) or len(raw.strip()) < 10:
        return []
    reader = csv.DictReader(io.StringIO(raw))
    return list(reader)

# ---- Pan-STARRS DR2 Detections (individual epoch photometry) ----
def ps_detections(ra, dec, radius_arcsec=3):
    r_deg = radius_arcsec / 3600.0
    url = (f"https://catalogs.mast.stsci.edu/api/v0.1/panstarrs/dr2/detection.csv?"
           f"ra={ra}&dec={dec}&radius={r_deg}"
           f"&columns=[objID,detectID,filterID,obsTime,psfFlux,psfFluxErr,psfMag,psfMagErr]"
           f"&pagesize=50")
    raw = fetch(url, as_json=False)
    if isinstance(raw, dict) and '_error' in raw:
        return raw
    if not isinstance(raw, str) or len(raw.strip()) < 10:
        return []
    reader = csv.DictReader(io.StringIO(raw))
    return list(reader)

# ---- TNS search ----
def tns_check(ra, dec, radius_arcsec=10):
    url = (f"https://www.wis-tns.org/search?"
           f"ra={ra}&decl={dec}&radius={radius_arcsec}&coords_unit=arcsec"
           f"&format=csv")
    raw = fetch(url, as_json=False, timeout=15)
    if isinstance(raw, dict) and '_error' in raw:
        return raw
    if 'AT ' in str(raw) or 'SN ' in str(raw):
        return {"found": True, "raw": str(raw)[:500]}
    return {"found": False}

# ---- SDSS DR17 cone search ----
def sdss_check(ra, dec, radius_arcmin=0.17):
    url = (f"https://skyserver.sdss.org/dr17/SkyServerWS/SearchTools/RadialSearch?"
           f"ra={ra}&dec={dec}&radius={radius_arcmin}&format=csv&limit=5")
    raw = fetch(url, as_json=False, timeout=15)
    if isinstance(raw, dict) and '_error' in raw:
        return raw
    if not isinstance(raw, str) or len(raw.strip()) < 10:
        return []
    try:
        reader = csv.DictReader(io.StringIO(raw))
        return list(reader)
    except:
        return {"_raw": raw[:300]}

# ---- VizieR APASS (multi-epoch photometry catalog) ----
def apass_check(ra, dec, radius_arcsec=10):
    url = (f"https://vizier.cds.unistra.fr/viz-bin/votable?"
           f"-source=II/336/apass9&-c={ra}+{dec}&-c.rs={radius_arcsec}"
           f"&-out=RAJ2000,DEJ2000,Vmag,e_Vmag,Bmag,e_Bmag,gmag,rmag,imag&-out.max=5")
    raw = fetch(url, as_json=False, timeout=15)
    if isinstance(raw, dict) and '_error' in raw:
        return raw
    n_rows = raw.count('<TR>') - 1  # header row
    # Extract magnitudes
    cells = re.findall(r'<TD>([^<]+)</TD>', raw)
    return {"n_sources": max(0, n_rows), "cells": cells[:30]}

# ============================================================
for cand in STRONG:
    ra, dec = cand['ra'], cand['dec']
    print(f"\n{'='*80}")
    print(f"  {cand['id']}  RA={ra}, Dec={dec}  sigma={cand['sigma']}")
    print(f"  {cand['desc']}")
    print(f"{'='*80}")

    # 1. PS DR2 catalog
    print(f"\n  [PS1 Catalog] ", end="", flush=True)
    ps = ps_catalog(ra, dec, 5)
    time.sleep(0.3)
    if isinstance(ps, dict) and '_error' in ps:
        print(f"ERROR: {ps['_error'][:80]}")
    elif len(ps) == 0:
        print(f"EMPTY FIELD - no cataloged PS1 sources within 5\"")
    else:
        print(f"{len(ps)} source(s):")
        for s in ps[:5]:
            g = s.get('gMeanPSFMag', '?')
            r = s.get('rMeanPSFMag', '?')
            i = s.get('iMeanPSFMag', '?')
            nd = s.get('nDetections', '?')
            oid = s.get('objID', '?')
            print(f"    objID={oid}  nDet={nd}  g={g}  r={r}  i={i}")

    # 2. PS DR2 detections
    print(f"\n  [PS1 Detections] ", end="", flush=True)
    det = ps_detections(ra, dec, 3)
    time.sleep(0.3)
    filters = {'1': 'g', '2': 'r', '3': 'i', '4': 'z', '5': 'y'}
    if isinstance(det, dict) and '_error' in det:
        print(f"ERROR: {det['_error'][:80]}")
    elif len(det) == 0:
        print(f"No individual epoch detections (source may be in stacks only)")
    else:
        print(f"{len(det)} epoch detection(s):")
        for d in det[:15]:
            filt = filters.get(str(d.get('filterID', '?')), '?')
            mag = d.get('psfMag', '?')
            flux = d.get('psfFlux', '?')
            mjd = d.get('obsTime', '?')
            print(f"    {filt}-band  MJD={mjd}  mag={mag}  flux={flux}")
        if len(det) > 15:
            # Print summary of remaining
            mags = []
            for d in det:
                try:
                    mags.append(float(d.get('psfMag', 0)))
                except:
                    pass
            if mags:
                print(f"    ... {len(det)-15} more. Mag range: {min(mags):.2f} — {max(mags):.2f}")

    # 3. TNS
    print(f"\n  [TNS] ", end="", flush=True)
    tns = tns_check(ra, dec, 10)
    time.sleep(1)  # TNS rate limit
    if isinstance(tns, dict) and '_error' in tns:
        print(f"ERROR: {tns['_error'][:80]}")
    elif isinstance(tns, dict) and tns.get('found'):
        print(f"KNOWN TRANSIENT FOUND: {tns.get('raw', '?')[:200]}")
    else:
        print(f"No known transients")

    # 4. SDSS
    print(f"\n  [SDSS DR17] ", end="", flush=True)
    sdss = sdss_check(ra, dec, 0.17)
    time.sleep(0.3)
    if isinstance(sdss, dict) and '_error' in sdss:
        print(f"ERROR: {sdss.get('_error', '?')[:80]}")
    elif isinstance(sdss, dict) and '_raw' in sdss:
        print(f"Raw: {sdss['_raw'][:150]}")
    elif isinstance(sdss, list) and len(sdss) > 0:
        print(f"{len(sdss)} source(s):")
        for s in sdss[:3]:
            stype = s.get('type', '?')
            type_name = {'3': 'GALAXY', '6': 'STAR'}.get(str(stype), stype)
            g = s.get('g', '?')
            r = s.get('r', '?')
            i = s.get('i', '?')
            ra_s = s.get('ra', '?')
            dec_s = s.get('dec', '?')
            specclass = s.get('class', '')
            z = s.get('z', '')
            print(f"    {type_name}  g={g} r={r} i={i}  spec={specclass} z={z}")
    else:
        print(f"Not in SDSS footprint or no sources")

    # 5. APASS
    print(f"\n  [APASS DR9] ", end="", flush=True)
    ap = apass_check(ra, dec, 10)
    time.sleep(0.3)
    if isinstance(ap, dict) and '_error' in ap:
        print(f"ERROR: {ap['_error'][:80]}")
    elif isinstance(ap, dict):
        n = ap.get('n_sources', 0)
        if n > 0:
            print(f"{n} source(s), fields: {ap.get('cells', [])[:15]}")
        else:
            print(f"No sources")
    else:
        print(f"No data")

    # Links
    print(f"\n  [VIEWER LINKS]")
    print(f"    Legacy: https://www.legacysurvey.org/viewer?ra={ra}&dec={dec}&layer=ls-dr10&zoom=16")
    print(f"    PS1:    https://ps1images.stsci.edu/cgi-bin/ps1cutouts?pos={ra}+{dec}&filter=color&filetypes=stack")
    print(f"    Aladin: https://aladin.cds.unistra.fr/AladinLite/?target={ra}+{dec}&fov=0.03&survey=P/PanSTARRS/DR1/color-i-r-g")

print(f"\n\n{'='*80}")
print("DEEP VALIDATION COMPLETE")
print(f"{'='*80}")
