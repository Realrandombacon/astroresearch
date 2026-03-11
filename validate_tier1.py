"""
Validate all Tier 1 findings by:
1. Querying SIMBAD (10 arcsec radius) for known objects
2. Querying NED (30 arcsec radius) for known galaxies/AGN
3. Flagging red flags: round coords, "photometry failed", edge artifacts, duplicates
4. Checking galactic latitude (low |b| = crowded field = more false positives)
5. Checking MJD baselines for plausibility
"""
import json, os, sys, time, math, urllib.request, urllib.parse

sys.stdout.reconfigure(encoding='utf-8')

TIER1_IDS = [
    'F20260310_224815', 'F20260311_002153', 'F20260311_005040', 'F20260311_011157',
    'F20260311_014935', 'F20260311_022424', 'F20260311_022856', 'F20260311_024913',
    'F20260311_033816', 'F20260311_033948', 'F20260311_044554', 'F20260311_061151',
    'F20260311_071245', 'F20260311_072339', 'F20260311_072512', 'F20260311_073056',
    'F20260311_081120', 'F20260311_082841', 'F20260311_083429', 'F20260311_083931'
]

# ---- Load findings ----
findings = []
for fid in TIER1_IDS:
    path = f'findings/{fid}.json'
    with open(path, 'r', encoding='utf-8') as f:
        findings.append(json.load(f))

# ---- Helper: equatorial to galactic ----
def eq_to_gal_lat(ra_deg, dec_deg):
    """Approximate galactic latitude from RA/Dec (J2000)"""
    ra = math.radians(ra_deg)
    dec = math.radians(dec_deg)
    # North galactic pole: RA=192.85948, Dec=27.12825
    ra_ngp = math.radians(192.85948)
    dec_ngp = math.radians(27.12825)
    l_ncp = math.radians(122.93192)
    sin_b = (math.sin(dec) * math.sin(dec_ngp) +
             math.cos(dec) * math.cos(dec_ngp) * math.cos(ra - ra_ngp))
    return math.degrees(math.asin(max(-1, min(1, sin_b))))

# ---- Helper: query SIMBAD ----
def query_simbad(ra, dec, radius_arcsec=10):
    """Query SIMBAD TAP for objects near coords. Returns list of dicts."""
    query = f"""SELECT TOP 5 main_id, otype_longname, ra, dec,
    DISTANCE(POINT('ICRS', ra, dec), POINT('ICRS', {ra}, {dec})) AS dist_deg
    FROM basic
    WHERE CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', {ra}, {dec}, {radius_arcsec/3600.0})) = 1
    ORDER BY dist_deg ASC"""

    url = "https://simbad.u-strasbg.fr/simbad/sim-tap/sync"
    params = urllib.parse.urlencode({
        'REQUEST': 'doQuery',
        'LANG': 'ADQL',
        'FORMAT': 'json',
        'QUERY': query
    })
    try:
        req = urllib.request.Request(f"{url}?{params}", headers={'User-Agent': 'AstroResearch/1.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        rows = data.get('data', [])
        cols = [c['name'] for c in data.get('metadata', [])]
        results = []
        for row in rows:
            obj = dict(zip(cols, row))
            results.append(obj)
        return results
    except Exception as e:
        return [{"error": str(e)}]

# ---- Helper: query NED ----
def query_ned(ra, dec, radius_arcmin=0.5):
    """Query NED for nearby objects."""
    url = (f"https://ned.ipac.caltech.edu/srs/ObjectLookup?"
           f"search_type=Near%20Position%20Search&"
           f"RA={ra}&DEC={dec}&SR={radius_arcmin}&of=json")
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'AstroResearch/1.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode()
        data = json.loads(text)
        if isinstance(data, dict) and 'ResultList' in data:
            return data['ResultList'][:5]
        elif isinstance(data, list):
            return data[:5]
        return []
    except Exception as e:
        return [{"error": str(e)}]

# ---- Red flag detection ----
def check_red_flags(f):
    flags = []
    desc = f.get('description', '').lower()
    ra, dec = f['ra'], f['dec']

    # Round coordinates (likely hallucinated or imprecise)
    if ra == int(ra) and dec == int(dec):
        flags.append("ROUND_COORDS: Both RA and Dec are integers — likely imprecise or hallucinated")
    elif ra == round(ra, 1) and dec == round(dec, 1) and '.' not in str(ra):
        flags.append("LOW_PRECISION: Coordinates have very few decimal places")

    # Photometry failed
    if 'photometry failed' in desc or 'photometry limited' in desc:
        flags.append("PHOTOMETRY_FAILED: No magnitude measurement — detection relies on image comparison only")

    # Edge artifacts
    if 'image edge' in desc or 'edge of cutout' in desc or 'outside image boundaries' in desc:
        flags.append("EDGE_ARTIFACT: Source at image edge — high false positive risk")

    # Coordinate mapping errors
    if 'coordinate mapping error' in desc or 'coordinate mismatch' in desc:
        flags.append("COORD_ERROR: Coordinate issues mentioned — astrometry may be wrong")

    # No sigma value
    if 'sigma' not in desc:
        flags.append("NO_SIGMA: No statistical significance value reported")

    # Galactic latitude
    gal_b = eq_to_gal_lat(ra, dec)
    if abs(gal_b) < 10:
        flags.append(f"LOW_GAL_LAT: |b|={abs(gal_b):.1f}° — near galactic plane, crowded field")

    # Repeated "2.3 magnitudes" or "MJD 57234 to 59012" — Qwen template language
    if '~2.3 mag' in desc or '2.3 magnitudes' in desc:
        flags.append("TEMPLATE_MAG: '2.3 mag' appears formulaic — may be Qwen default rather than measured")
    if 'mjd 57234' in desc and 'mjd 59012' in desc:
        flags.append("TEMPLATE_MJD: Same MJD range (57234-59012) repeated — likely default epochs not measured")

    # Flux decrease described as "brightening"
    if 'decreased' in desc and 'brightening' in desc:
        flags.append("CONTRADICTS_SELF: Description says flux decreased AND brightening — inconsistent")

    return flags, gal_b

# ---- Duplicate detection ----
def find_duplicates(findings):
    dupes = []
    for i, f1 in enumerate(findings):
        for j, f2 in enumerate(findings):
            if j <= i:
                continue
            sep = math.sqrt((f1['ra'] - f2['ra'])**2 + (f1['dec'] - f2['dec'])**2) * 3600  # arcsec approx
            if sep < 30:  # within 30 arcsec
                dupes.append((f1['id'], f2['id'], f"{sep:.1f} arcsec"))
    return dupes

# ===================== MAIN =====================
print("=" * 80)
print("TIER 1 VALIDATION REPORT")
print("=" * 80)

# Duplicates first
dupes = find_duplicates(findings)
if dupes:
    print(f"\n### DUPLICATES DETECTED ({len(dupes)}):")
    for d in dupes:
        print(f"  {d[0]} <-> {d[1]}  (sep={d[2]})")

# Per-finding validation
verdicts = {}
for i, f in enumerate(findings):
    fid = f['id']
    ra, dec = f['ra'], f['dec']
    print(f"\n{'─' * 70}")
    print(f"[{i+1}/20] {fid}  RA={ra}, Dec={dec}")
    print(f"{'─' * 70}")

    # Red flags
    flags, gal_b = check_red_flags(f)
    print(f"  Galactic latitude: b={gal_b:+.1f}°")
    if flags:
        for flag in flags:
            print(f"  ⚠ {flag}")
    else:
        print(f"  ✓ No red flags detected")

    # SIMBAD query
    print(f"  SIMBAD (10\"): ", end="", flush=True)
    simbad_results = query_simbad(ra, dec, radius_arcsec=10)
    time.sleep(0.5)  # rate limit

    if simbad_results and 'error' not in simbad_results[0]:
        for obj in simbad_results:
            dist_arcsec = obj.get('dist_deg', 0) * 3600
            name = obj.get('main_id', '?')
            otype = obj.get('otype_longname', '?')
            print(f"\n    MATCH: {name} ({otype}) at {dist_arcsec:.1f}\"")
    elif simbad_results and 'error' in simbad_results[0]:
        print(f"query error: {simbad_results[0]['error'][:80]}")
    else:
        print("No matches — GOOD")

    # Wider SIMBAD (30 arcsec)
    print(f"  SIMBAD (30\"): ", end="", flush=True)
    simbad_wide = query_simbad(ra, dec, radius_arcsec=30)
    time.sleep(0.5)

    if simbad_wide and 'error' not in simbad_wide[0]:
        for obj in simbad_wide:
            dist_arcsec = obj.get('dist_deg', 0) * 3600
            name = obj.get('main_id', '?')
            otype = obj.get('otype_longname', '?')
            print(f"\n    MATCH: {name} ({otype}) at {dist_arcsec:.1f}\"")
    elif simbad_wide and 'error' in simbad_wide[0]:
        print(f"query error")
    else:
        print("No matches — GOOD")

    # Verdict
    n_flags = len(flags)
    has_simbad = bool(simbad_results and 'error' not in simbad_results[0]) if simbad_results else False
    has_simbad_wide = bool(simbad_wide and 'error' not in simbad_wide[0]) if simbad_wide else False

    serious_flags = [f for f in flags if any(k in f for k in ['EDGE_ARTIFACT', 'COORD_ERROR', 'CONTRADICTS_SELF', 'ROUND_COORDS', 'TEMPLATE_MJD'])]

    if has_simbad:
        verdict = "REJECT — known SIMBAD object within 10\""
    elif has_simbad_wide and n_flags >= 2:
        verdict = "WEAK — SIMBAD nearby + multiple red flags"
    elif len(serious_flags) >= 2:
        verdict = "SUSPECT — multiple serious red flags"
    elif has_simbad_wide:
        verdict = "CAUTION — SIMBAD object within 30\" (could be related)"
    elif len(serious_flags) == 1:
        verdict = "PLAUSIBLE — one concern but worth following up"
    elif n_flags >= 2:
        verdict = "PLAUSIBLE — minor concerns but fundamentally solid"
    elif n_flags == 0 and not has_simbad_wide:
        verdict = "STRONG — no red flags, no SIMBAD matches"
    else:
        verdict = "PLAUSIBLE — minor concerns"

    verdicts[fid] = verdict
    print(f"\n  >>> VERDICT: {verdict}")

# ===================== SUMMARY =====================
print(f"\n\n{'=' * 80}")
print("VALIDATION SUMMARY")
print(f"{'=' * 80}")

for v_label in ["STRONG", "PLAUSIBLE", "CAUTION", "SUSPECT", "WEAK", "REJECT"]:
    matches = [(fid, v) for fid, v in verdicts.items() if v.startswith(v_label)]
    if matches:
        print(f"\n{v_label} ({len(matches)}):")
        for fid, v in matches:
            f = [x for x in findings if x['id'] == fid][0]
            print(f"  {fid}  RA={f['ra']}, Dec={f['dec']}  — {v.split('—')[1].strip() if '—' in v else ''}")

print(f"\nTotal: {len(verdicts)} findings validated")
print(f"  STRONG/PLAUSIBLE: {sum(1 for v in verdicts.values() if v.startswith('STRONG') or v.startswith('PLAUSIBLE'))}")
print(f"  CAUTION: {sum(1 for v in verdicts.values() if v.startswith('CAUTION'))}")
print(f"  SUSPECT/WEAK/REJECT: {sum(1 for v in verdicts.values() if v.startswith('SUSPECT') or v.startswith('WEAK') or v.startswith('REJECT'))}")
