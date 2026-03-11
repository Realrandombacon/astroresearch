"""
Deep validation of the 5 STRONG Tier 1 candidates.
Queries: Pan-STARRS DR2 catalog, Transient Name Server, NED, Legacy Survey DR10, SDSS
"""
import json, sys, time, math, urllib.request, urllib.parse, ssl, re

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

def fetch_json(url, timeout=20):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'AstroResearch/1.0'})
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as resp:
            return json.loads(resp.read().decode('utf-8', errors='replace'))
    except Exception as e:
        return {"error": str(e)[:150]}

def fetch_text(url, timeout=20):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'AstroResearch/1.0'})
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as resp:
            return resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        return f"ERROR: {str(e)[:150]}"

# ============================================================
# 1. Pan-STARRS DR2 catalog (MAST CasJobs)
# ============================================================
def query_panstarrs_dr2(ra, dec, radius_arcsec=5):
    """Query Pan-STARRS DR2 MeanObject table via MAST API."""
    url = (f"https://catalogs.mast.stsci.edu/api/v0.1/panstarrs/dr2/mean?"
           f"ra={ra}&dec={dec}&radius={radius_arcsec/3600.0}"
           f"&nDetections.gte=1&columns=[objID,raMean,decMean,nDetections,gMeanPSFMag,rMeanPSFMag,iMeanPSFMag,zMeanPSFMag,yMeanPSFMag]"
           f"&pagesize=10")
    data = fetch_json(url)
    if 'error' in data:
        return data
    if isinstance(data, dict) and 'data' in data:
        return data['data']
    return data

# ============================================================
# 2. Pan-STARRS DR2 detection table (individual epoch detections)
# ============================================================
def query_panstarrs_detections(ra, dec, radius_arcsec=3):
    """Query Pan-STARRS DR2 Detection table for individual epoch measurements."""
    url = (f"https://catalogs.mast.stsci.edu/api/v0.1/panstarrs/dr2/detection?"
           f"ra={ra}&dec={dec}&radius={radius_arcsec/3600.0}"
           f"&columns=[objID,detectID,filterID,obsTime,psfFlux,psfFluxErr,psfMag,psfMagErr]"
           f"&pagesize=50")
    data = fetch_json(url)
    if 'error' in data:
        return data
    if isinstance(data, dict) and 'data' in data:
        return data['data']
    return data

# ============================================================
# 3. Transient Name Server (TNS)
# ============================================================
def query_tns(ra, dec, radius_arcsec=10):
    """Check TNS for known transients near this position."""
    # TNS cone search via their public API
    url = (f"https://www.wis-tns.org/search?"
           f"ra={ra}&decl={dec}&radius={radius_arcsec}&coords_unit=arcsec"
           f"&format=json")
    text = fetch_text(url, timeout=15)
    if 'ERROR' in text:
        return {"error": text}
    # TNS may return HTML, check for object names
    if 'AT ' in text or 'SN ' in text:
        # Try to extract transient names
        matches = re.findall(r'(AT\s*\d{4}[a-z]+|SN\s*\d{4}[a-z]+)', text)
        return {"matches": matches, "raw_len": len(text)}
    return {"matches": [], "raw_len": len(text)}

# ============================================================
# 4. NED (NASA Extragalactic Database)
# ============================================================
def query_ned(ra, dec, radius_arcmin=0.5):
    """Query NED for galaxies, AGN, etc."""
    url = (f"https://ned.ipac.caltech.edu/cgi-bin/objsearch?"
           f"search_type=Near+Position+Search&in_csys=Equatorial&in_equinox=J2000.0"
           f"&lon={ra}d&lat={dec}d&radius={radius_arcmin}"
           f"&hconst=73&omegam=0.27&omegav=0.73&corr_z=1"
           f"&z_constraint=Unconstrained&z_value1=&z_value2=&z_unit=z"
           f"&ot_include=ANY&nmp_op=ANY"
           f"&out_csys=Equatorial&out_equinox=J2000.0&obj_sort=Distance+to+search+center"
           f"&of=ascii_tab&zv_breaker=30000.0&list_limit=5&img_stamp=NO")
    text = fetch_text(url, timeout=20)
    if 'ERROR' in text[:20]:
        return {"error": text[:150]}
    # Parse NED ASCII table output
    lines = text.strip().split('\n')
    objects = []
    in_data = False
    for line in lines:
        if line.startswith('1|') or (in_data and '|' in line and not line.startswith('#')):
            in_data = True
            parts = line.split('|')
            if len(parts) >= 4:
                objects.append({
                    'name': parts[1].strip() if len(parts) > 1 else '?',
                    'type': parts[3].strip() if len(parts) > 3 else '?',
                })
    return objects

# ============================================================
# 5. Legacy Survey DR10
# ============================================================
def query_legacy_survey(ra, dec, radius_arcsec=10):
    """Query Legacy Survey DR10 catalog."""
    url = (f"https://www.legacysurvey.org/viewer/cat.json?"
           f"ra={ra}&dec={dec}&radius={radius_arcsec/3600.0}")
    data = fetch_json(url)
    if isinstance(data, list):
        return data
    elif isinstance(data, dict) and 'error' in data:
        return data
    return data

# ============================================================
# 6. SDSS DR17
# ============================================================
def query_sdss(ra, dec, radius_arcmin=0.17):
    """Query SDSS DR17 for photometric + spectroscopic objects."""
    # Photometric
    url = (f"https://skyserver.sdss.org/dr17/SkyServerWS/SearchTools/RadialSearch?"
           f"ra={ra}&dec={dec}&radius={radius_arcmin}&format=json&limit=5")
    data = fetch_json(url, timeout=15)
    return data

# ============================================================
# MAIN
# ============================================================
for cand in STRONG:
    ra, dec = cand['ra'], cand['dec']
    print(f"\n{'='*80}")
    print(f"CANDIDATE: {cand['id']}  RA={ra}, Dec={dec}  sigma={cand['sigma']}")
    print(f"  {cand['desc']}")
    print(f"{'='*80}")

    # --- Pan-STARRS DR2 catalog ---
    print(f"\n  [1] Pan-STARRS DR2 Catalog (5\" radius):")
    ps = query_panstarrs_dr2(ra, dec, 5)
    time.sleep(0.3)
    if isinstance(ps, dict) and 'error' in ps:
        print(f"      ERROR: {ps['error']}")
    elif isinstance(ps, list) and len(ps) > 0:
        print(f"      FOUND {len(ps)} source(s)!")
        for obj in ps[:3]:
            if isinstance(obj, dict):
                gmag = obj.get('gMeanPSFMag', -999)
                rmag = obj.get('rMeanPSFMag', -999)
                imag = obj.get('iMeanPSFMag', -999)
                ndet = obj.get('nDetections', '?')
                objid = obj.get('objID', '?')
                print(f"      objID={objid}  nDet={ndet}  g={gmag}  r={rmag}  i={imag}")
    else:
        print(f"      No cataloged sources (truly blank field)")

    # --- Pan-STARRS DR2 detections ---
    print(f"\n  [2] Pan-STARRS DR2 Detections (individual epochs, 3\" radius):")
    det = query_panstarrs_detections(ra, dec, 3)
    time.sleep(0.3)
    if isinstance(det, dict) and 'error' in det:
        print(f"      ERROR: {det['error']}")
    elif isinstance(det, list) and len(det) > 0:
        print(f"      FOUND {len(det)} detection(s)!")
        # Group by filter
        filters = {1: 'g', 2: 'r', 3: 'i', 4: 'z', 5: 'y'}
        for d in det[:10]:
            if isinstance(d, dict):
                filt = filters.get(d.get('filterID', 0), '?')
                mag = d.get('psfMag', -999)
                magerr = d.get('psfMagErr', -999)
                flux = d.get('psfFlux', -999)
                obstime = d.get('obsTime', '?')
                print(f"      {filt}-band  MJD={obstime}  mag={mag}  flux={flux}")
        if len(det) > 10:
            print(f"      ... and {len(det)-10} more detections")
    else:
        print(f"      No individual detections found")

    # --- TNS ---
    print(f"\n  [3] Transient Name Server (10\" radius):")
    tns = query_tns(ra, dec, 10)
    time.sleep(0.5)
    if isinstance(tns, dict) and 'error' in tns:
        print(f"      ERROR: {tns.get('error', '?')[:100]}")
    elif isinstance(tns, dict) and tns.get('matches'):
        print(f"      KNOWN TRANSIENT(S): {', '.join(tns['matches'])}")
    else:
        print(f"      No known transients")

    # --- NED ---
    print(f"\n  [4] NED (30\" radius):")
    ned = query_ned(ra, dec, 0.5)
    time.sleep(0.3)
    if isinstance(ned, dict) and 'error' in ned:
        print(f"      ERROR: {ned.get('error', '?')[:100]}")
    elif isinstance(ned, list) and len(ned) > 0:
        for obj in ned[:5]:
            print(f"      {obj.get('name', '?')} ({obj.get('type', '?')})")
    else:
        print(f"      No extragalactic objects")

    # --- Legacy Survey DR10 ---
    print(f"\n  [5] Legacy Survey DR10 (10\" radius):")
    ls = query_legacy_survey(ra, dec, 10)
    time.sleep(0.3)
    if isinstance(ls, dict) and 'error' in ls:
        print(f"      ERROR: {ls.get('error', '?')[:100]}")
    elif isinstance(ls, list) and len(ls) > 0:
        print(f"      FOUND {len(ls)} source(s)!")
        for obj in ls[:5]:
            if isinstance(obj, dict):
                stype = obj.get('type', '?')
                flux_g = obj.get('flux_g', '?')
                flux_r = obj.get('flux_r', '?')
                ra_ls = obj.get('ra', '?')
                dec_ls = obj.get('dec', '?')
                print(f"      type={stype}  ra={ra_ls}  dec={dec_ls}  flux_g={flux_g}  flux_r={flux_r}")
    else:
        print(f"      No sources in Legacy Survey footprint (or not covered)")

    # --- SDSS ---
    print(f"\n  [6] SDSS DR17 (10\" radius):")
    sdss = query_sdss(ra, dec, 0.17)
    time.sleep(0.3)
    if isinstance(sdss, dict) and 'error' in sdss:
        print(f"      ERROR: {sdss.get('error', '?')[:100]}")
    elif isinstance(sdss, list) and len(sdss) > 0:
        print(f"      FOUND {len(sdss)} source(s)!")
        for obj in sdss[:3]:
            if isinstance(obj, dict):
                objtype = obj.get('type', '?')  # 3=galaxy, 6=star
                g = obj.get('g', '?')
                r = obj.get('r', '?')
                print(f"      type={objtype}  g={g}  r={r}")
    else:
        print(f"      No SDSS coverage or no sources")

    # --- Viewer links ---
    print(f"\n  [LINKS]")
    print(f"      Legacy Survey: https://www.legacysurvey.org/viewer?ra={ra}&dec={dec}&layer=ls-dr10&zoom=16")
    print(f"      Pan-STARRS:    https://ps1images.stsci.edu/cgi-bin/ps1cutouts?pos={ra}+{dec}&filter=color&filetypes=stack")
    print(f"      Aladin:        https://aladin.cds.unistra.fr/AladinLite/?target={ra}+{dec}&fov=0.05&survey=P/PanSTARRS/DR1/color-i-r-g")

print(f"\n\n{'='*80}")
print("DEEP VALIDATION COMPLETE")
print(f"{'='*80}")
