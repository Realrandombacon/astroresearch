"""
Validate Tier 1 findings v2:
1. SIMBAD sim-coo (30 arcsec)
2. VizieR Gaia DR3 cone search (10 arcsec)
3. Red flag analysis
4. Duplicate detection
5. Final verdict
"""
import json, os, sys, time, math, urllib.request, urllib.parse, ssl, re

sys.stdout.reconfigure(encoding='utf-8')

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

TIER1_IDS = [
    'F20260310_224815', 'F20260311_002153', 'F20260311_005040', 'F20260311_011157',
    'F20260311_014935', 'F20260311_022424', 'F20260311_022856', 'F20260311_024913',
    'F20260311_033816', 'F20260311_033948', 'F20260311_044554', 'F20260311_061151',
    'F20260311_071245', 'F20260311_072339', 'F20260311_072512', 'F20260311_073056',
    'F20260311_081120', 'F20260311_082841', 'F20260311_083429', 'F20260311_083931'
]

findings = []
for fid in TIER1_IDS:
    with open(f'findings/{fid}.json', 'r', encoding='utf-8') as f:
        findings.append(json.load(f))

# ---- SIMBAD sim-coo ----
def query_simbad(ra, dec, radius_arcsec=30):
    url = (f"https://simbad.cds.unistra.fr/simbad/sim-coo?"
           f"Coord={ra}+{dec}&CooFrame=FK5&CooEpoch=2000&CooEqui=2000"
           f"&CooDefinedFrames=none&Radius={radius_arcsec}&Radius.unit=arcsec"
           f"&submit=submit+query&output.format=ASCII")
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'AstroResearch/1.0'})
        with urllib.request.urlopen(req, timeout=20, context=CTX) as resp:
            text = resp.read().decode('utf-8', errors='replace')
        if 'No astronomical object found' in text:
            return None
        # Parse the response for object info
        lines = text.strip().split('\n')
        # Look for object identifier and type
        obj_name = None
        obj_type = None
        for line in lines:
            if line.startswith('Object'):
                obj_name = line.split('---')[0].replace('Object', '').strip()
            if 'type' in line.lower() and ':' in line:
                obj_type = line.split(':')[-1].strip()
        # Also extract number of objects if it's a list
        n_objects = 0
        for line in lines:
            if 'Number of objects' in line:
                m = re.search(r'(\d+)', line)
                if m:
                    n_objects = int(m.group(1))
        return {'name': obj_name, 'type': obj_type, 'n_objects': n_objects, 'raw': text[:500]}
    except Exception as e:
        return {'error': str(e)[:100]}

# ---- VizieR Gaia DR3 cone search ----
def query_gaia_vizier(ra, dec, radius_arcsec=10):
    """Query VizieR for Gaia DR3 sources."""
    url = (f"https://vizier.cds.unistra.fr/viz-bin/votable?"
           f"-source=I/355/gaiadr3&-c={ra}+{dec}&-c.rs={radius_arcsec}"
           f"&-out=Source,RA_ICRS,DE_ICRS,Gmag,Plx&-out.max=5")
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'AstroResearch/1.0'})
        with urllib.request.urlopen(req, timeout=20, context=CTX) as resp:
            text = resp.read().decode('utf-8', errors='replace')
        # Count TABLEDATA rows
        n_rows = text.count('<TR>')
        # Extract Gmag values
        gmags = re.findall(r'<TD>([0-9.]+)</TD>\s*<TD>', text)
        return {'n_sources': max(0, n_rows - 1), 'gmags': gmags[:5], 'has_data': '<TABLEDATA>' in text}
    except Exception as e:
        return {'error': str(e)[:100]}

# ---- Galactic latitude ----
def eq_to_gal_lat(ra_deg, dec_deg):
    ra = math.radians(ra_deg)
    dec = math.radians(dec_deg)
    ra_ngp = math.radians(192.85948)
    dec_ngp = math.radians(27.12825)
    sin_b = (math.sin(dec) * math.sin(dec_ngp) +
             math.cos(dec) * math.cos(dec_ngp) * math.cos(ra - ra_ngp))
    return math.degrees(math.asin(max(-1, min(1, sin_b))))

# ---- Red flags ----
def check_red_flags(f):
    flags = []
    desc = f.get('description', '').lower()
    ra, dec = f['ra'], f['dec']

    if 'photometry failed' in desc or 'photometry limited' in desc:
        flags.append("PHOT_FAIL")
    if 'image edge' in desc or 'edge of cutout' in desc or 'outside image boundaries' in desc:
        flags.append("EDGE")
    if 'coordinate mapping error' in desc or 'coordinate mismatch' in desc:
        flags.append("COORD_ERR")
    if 'sigma' not in desc:
        flags.append("NO_SIGMA")
    if '~2.3 mag' in desc or '2.3 magnitudes' in desc:
        flags.append("TEMPLATE_MAG")
    if 'mjd 57234' in desc and 'mjd 59012' in desc:
        flags.append("TEMPLATE_MJD")
    if 'decreased' in desc and 'brightening' in desc:
        flags.append("CONTRADICTS")

    gal_b = eq_to_gal_lat(ra, dec)
    if abs(gal_b) < 10:
        flags.append(f"LOW_b({abs(gal_b):.0f})")

    return flags, gal_b

# ---- Duplicates ----
def find_duplicates(findings):
    dupes = []
    for i, f1 in enumerate(findings):
        for j, f2 in enumerate(findings):
            if j <= i: continue
            sep = math.sqrt((f1['ra'] - f2['ra'])**2 +
                           ((f1['dec'] - f2['dec']))**2) * 3600
            if sep < 60:
                dupes.append((f1['id'], f2['id'], f"{sep:.0f}\""))
    return dupes

# ===================== MAIN =====================
print("=" * 80)
print("TIER 1 VALIDATION REPORT v2 — SIMBAD + Gaia DR3 + Red Flags")
print("=" * 80)

dupes = find_duplicates(findings)
if dupes:
    print(f"\nDUPLICATES:")
    for d in dupes:
        print(f"  {d[0]} <-> {d[1]}  (sep={d[2]})")

results = []

for i, f in enumerate(findings):
    fid = f['id']
    ra, dec = f['ra'], f['dec']
    flags, gal_b = check_red_flags(f)

    print(f"\n{'─'*70}")
    print(f"[{i+1}/20] {fid}  RA={ra}, Dec={dec}  b={gal_b:+.1f}")
    print(f"  Desc: {f['description'][:120]}...")

    # Red flags
    if flags:
        print(f"  Flags: {', '.join(flags)}")
    else:
        print(f"  Flags: NONE")

    # SIMBAD
    print(f"  SIMBAD 30\": ", end="", flush=True)
    simbad = query_simbad(ra, dec, 30)
    time.sleep(0.3)

    simbad_match = False
    if simbad is None:
        print("No match")
    elif 'error' in simbad:
        print(f"ERROR: {simbad['error']}")
    else:
        simbad_match = True
        print(f"MATCH! {simbad.get('name', '?')} ({simbad.get('type', '?')})")
        if simbad.get('n_objects', 0) > 1:
            print(f"         ({simbad['n_objects']} objects in field)")

    # Gaia DR3
    print(f"  Gaia DR3 10\": ", end="", flush=True)
    gaia = query_gaia_vizier(ra, dec, 10)
    time.sleep(0.3)

    gaia_match = False
    if 'error' in gaia:
        print(f"ERROR: {gaia['error']}")
    elif gaia['n_sources'] > 0:
        gaia_match = True
        print(f"MATCH! {gaia['n_sources']} source(s), Gmag={gaia['gmags']}")
    else:
        print("No sources")

    # Extract sigma from description
    sigma = None
    sigma_match = re.search(r'sigma[=:]?\s*(-?[\d]+\.?\d*)', f['description'].lower())
    if sigma_match:
        try:
            sigma = float(sigma_match.group(1))
        except ValueError:
            sigma = None

    # Verdict
    serious = [fl for fl in flags if fl in ('EDGE', 'COORD_ERR', 'CONTRADICTS', 'TEMPLATE_MJD')]

    if simbad_match and gaia_match:
        verdict = "REJECT"
        reason = "Known SIMBAD object + Gaia source at location"
    elif simbad_match:
        verdict = "REJECT"
        reason = f"Known SIMBAD object: {simbad.get('name', '?')}"
    elif gaia_match and len(serious) >= 1:
        verdict = "WEAK"
        reason = f"Gaia source present + serious flags ({', '.join(serious)})"
    elif gaia_match:
        verdict = "CAUTION"
        reason = f"Gaia source(s) at location — may be known variable"
    elif len(serious) >= 2:
        verdict = "SUSPECT"
        reason = f"Multiple serious flags: {', '.join(serious)}"
    elif len(flags) == 0 and sigma and abs(sigma) > 20:
        verdict = "STRONG"
        reason = f"No flags, no catalog matches, sigma={sigma}"
    elif len(flags) == 0:
        verdict = "STRONG"
        reason = "No flags, no catalog matches"
    elif len(serious) == 1:
        verdict = "PLAUSIBLE"
        reason = f"One serious flag ({serious[0]}) but no catalog matches"
    elif len(flags) <= 2:
        verdict = "PLAUSIBLE"
        reason = f"Minor flags only ({', '.join(flags)})"
    else:
        verdict = "WEAK"
        reason = f"Many flags: {', '.join(flags)}"

    print(f"  >>> {verdict}: {reason}")
    results.append({
        'id': fid, 'ra': ra, 'dec': dec, 'gal_b': gal_b,
        'flags': flags, 'simbad': simbad_match, 'gaia': gaia_match,
        'sigma': sigma, 'verdict': verdict, 'reason': reason
    })

# ===================== SUMMARY =====================
print(f"\n\n{'='*80}")
print("FINAL VALIDATION SUMMARY")
print(f"{'='*80}")

for label in ['STRONG', 'PLAUSIBLE', 'CAUTION', 'SUSPECT', 'WEAK', 'REJECT']:
    group = [r for r in results if r['verdict'] == label]
    if not group:
        continue
    print(f"\n{label} ({len(group)}):")
    for r in group:
        sigma_str = f"  sigma={r['sigma']}" if r['sigma'] else ""
        flag_str = f"  [{','.join(r['flags'])}]" if r['flags'] else ""
        print(f"  {r['id']}  RA={r['ra']:<12} Dec={r['dec']:<12} b={r['gal_b']:+.0f}{sigma_str}{flag_str}")
        print(f"    -> {r['reason']}")

# Dedup note
if dupes:
    print(f"\nNOTE: Remove duplicate(s): {dupes}")

n_strong = sum(1 for r in results if r['verdict'] == 'STRONG')
n_plaus = sum(1 for r in results if r['verdict'] == 'PLAUSIBLE')
n_bad = sum(1 for r in results if r['verdict'] in ('CAUTION', 'SUSPECT', 'WEAK', 'REJECT'))
print(f"\nTOTAL: {n_strong} STRONG + {n_plaus} PLAUSIBLE + {n_bad} questionable")
print(f"After removing 1 duplicate: {n_strong + n_plaus - 1} solid candidates remaining (if dupe is in STRONG/PLAUS)")
