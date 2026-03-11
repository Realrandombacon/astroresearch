"""
Pull epoch-by-epoch Pan-STARRS DR2 detections for each STRONG candidate.
Build mini light curves to verify the variability claim.
Also cross-check with Pan-STARRS forced photometry and stack detections.
"""
import json, sys, time, csv, io, ssl, urllib.request

sys.stdout.reconfigure(encoding='utf-8')

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

STRONG = [
    {"id": "F20260311_005040", "ra": 244.723315, "dec": -19.256528, "sigma": 146.85},
    {"id": "F20260311_014935", "ra": 80.18, "dec": -23.0, "sigma": 48.19},
    {"id": "F20260311_022856", "ra": 268.994306, "dec": 51.23059, "sigma": 124.47},
    {"id": "F20260311_044554", "ra": 325.066417, "dec": 76.939167, "sigma": -234.88},
    {"id": "F20260311_072512", "ra": 303.21, "dec": 66.55, "sigma": 8.04},
]

def fetch_csv(url, timeout=30):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'AstroResearch/1.0'})
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as resp:
            raw = resp.read().decode('utf-8', errors='replace')
        if raw.strip():
            reader = csv.DictReader(io.StringIO(raw))
            return list(reader)
        return []
    except Exception as e:
        return [{"_error": str(e)[:200]}]

FILTERS = {'1': 'g', '2': 'r', '3': 'i', '4': 'z', '5': 'y'}

for cand in STRONG:
    ra, dec = cand['ra'], cand['dec']
    print(f"\n{'='*80}")
    print(f"  {cand['id']}  RA={ra}, Dec={dec}  sigma={cand['sigma']}")
    print(f"{'='*80}")

    # 1. Get all Mean objects within 10 arcsec (to find the objIDs)
    url = (f"https://catalogs.mast.stsci.edu/api/v0.1/panstarrs/dr2/mean.csv?"
           f"ra={ra}&dec={dec}&radius={10/3600.0}"
           f"&nDetections.gte=1"
           f"&columns=[objID,raMean,decMean,nDetections,epochMean,"
           f"gMeanPSFMag,rMeanPSFMag,iMeanPSFMag,zMeanPSFMag,yMeanPSFMag,"
           f"gFlags,rFlags,iFlags,zFlags,yFlags,"
           f"ng,nr,ni,nz,ny]"
           f"&pagesize=20")

    print(f"\n  --- Mean catalog (10\") ---")
    means = fetch_csv(url)
    time.sleep(0.5)

    if means and '_error' in means[0]:
        print(f"  ERROR: {means[0]['_error']}")
        continue

    if not means:
        print(f"  EMPTY FIELD - no PS1 cataloged sources within 10\"")
        print(f"  This means even PS1 stacks see nothing here.")
        print(f"  VERDICT: Qwen may have been looking at artifacts/noise in WARP images")
        continue

    print(f"  {len(means)} source(s) found:")
    for m in means:
        oid = m.get('objID', '?')
        nd = m.get('nDetections', '?')
        ng = m.get('ng', '?')
        nr = m.get('nr', '?')
        ni = m.get('ni', '?')
        g = m.get('gMeanPSFMag', '-999')
        r = m.get('rMeanPSFMag', '-999')
        i_mag = m.get('iMeanPSFMag', '-999')
        ram = m.get('raMean', '?')
        decm = m.get('decMean', '?')

        # Distance from target
        try:
            dist_arcsec = ((float(ram) - ra)**2 + (float(decm) - dec)**2)**0.5 * 3600
        except:
            dist_arcsec = 999

        g_str = f"g={g}" if g != '-999.0' and g != '-999' else "g=---"
        r_str = f"r={r}" if r != '-999.0' and r != '-999' else "r=---"
        i_str = f"i={i_mag}" if i_mag != '-999.0' and i_mag != '-999' else "i=---"

        print(f"    objID={oid}  dist={dist_arcsec:.1f}\"  nDet={nd} (g:{ng} r:{nr} i:{ni})")
        print(f"      {g_str}  {r_str}  {i_str}")

    # 2. Get detections for all objects within 5 arcsec
    print(f"\n  --- Epoch detections (5\") ---")
    url_det = (f"https://catalogs.mast.stsci.edu/api/v0.1/panstarrs/dr2/detection.csv?"
               f"ra={ra}&dec={dec}&radius={5/3600.0}"
               f"&columns=[objID,detectID,filterID,obsTime,psfFlux,psfFluxErr,psfMag,psfMagErr,infoFlag]"
               f"&pagesize=200&sort_by=[obsTime]")

    dets = fetch_csv(url_det)
    time.sleep(0.5)

    if dets and '_error' in dets[0]:
        print(f"  ERROR: {dets[0]['_error']}")
        continue

    if not dets:
        print(f"  No individual epoch detections (all stack-only)")
        print(f"  This means the source is too faint for individual WARP exposures")
        print(f"  Qwen used WARP difference imaging which can detect sub-threshold changes")
        continue

    print(f"  {len(dets)} epoch detection(s):")

    # Group by filter
    by_filter = {}
    for d in dets:
        f_name = FILTERS.get(str(d.get('filterID', '?')), '?')
        if f_name not in by_filter:
            by_filter[f_name] = []
        by_filter[f_name].append(d)

    for f_name in ['g', 'r', 'i', 'z', 'y']:
        if f_name not in by_filter:
            continue
        epochs = by_filter[f_name]
        print(f"\n    {f_name}-band ({len(epochs)} epochs):")

        mags = []
        fluxes = []
        for e in epochs:
            mjd = e.get('obsTime', '?')
            mag = e.get('psfMag', '?')
            mag_err = e.get('psfMagErr', '?')
            flux = e.get('psfFlux', '?')
            flux_err = e.get('psfFluxErr', '?')

            try:
                mag_f = float(mag)
                mags.append(mag_f)
            except:
                mag_f = None
            try:
                flux_f = float(flux)
                fluxes.append(flux_f)
            except:
                flux_f = None

            print(f"      MJD={mjd}  mag={mag}+/-{mag_err}  flux={flux}")

        # Variability analysis
        if len(mags) >= 2:
            mag_range = max(mags) - min(mags)
            print(f"    -> Mag range: {min(mags):.3f} to {max(mags):.3f} (delta={mag_range:.3f})")
            if mag_range > 0.5:
                print(f"    -> ** SIGNIFICANT VARIABILITY: {mag_range:.2f} mag **")
            elif mag_range > 0.1:
                print(f"    -> Mild variability: {mag_range:.2f} mag")
            else:
                print(f"    -> Stable (within {mag_range:.3f} mag)")

        if len(fluxes) >= 2:
            flux_range = max(fluxes) / max(min(fluxes), 1e-10)
            if flux_range > 2:
                print(f"    -> ** FLUX RATIO: {flux_range:.1f}x (max/min) **")

    # 3. Forced photometry (stack detections) — deeper
    print(f"\n  --- Stack detections (5\") ---")
    url_stack = (f"https://catalogs.mast.stsci.edu/api/v0.1/panstarrs/dr2/stack.csv?"
                 f"ra={ra}&dec={dec}&radius={5/3600.0}"
                 f"&columns=[objID,primaryDetection,gPSFMag,rPSFMag,iPSFMag,zPSFMag,yPSFMag,"
                 f"gKronMag,rKronMag,iKronMag,zKronMag,yKronMag]"
                 f"&pagesize=10")

    stacks = fetch_csv(url_stack)
    time.sleep(0.5)

    if stacks and '_error' in stacks[0]:
        print(f"  ERROR: {stacks[0]['_error']}")
    elif not stacks:
        print(f"  No stack detections")
    else:
        print(f"  {len(stacks)} stack detection(s):")
        for s in stacks:
            oid = s.get('objID', '?')
            prim = s.get('primaryDetection', '?')
            g = s.get('gPSFMag', '-999')
            r = s.get('rPSFMag', '-999')
            i_m = s.get('iPSFMag', '-999')
            gk = s.get('gKronMag', '-999')

            g_str = f"g={g}" if g not in ('-999.0', '-999') else "g=---"
            r_str = f"r={r}" if r not in ('-999.0', '-999') else "r=---"
            i_str = f"i={i_m}" if i_m not in ('-999.0', '-999') else "i=---"
            gk_str = f"gKron={gk}" if gk not in ('-999.0', '-999') else ""

            print(f"    objID={oid}  primary={prim}  {g_str} {r_str} {i_str} {gk_str}")

    # === SUMMARY for this candidate ===
    n_epochs = len(dets)
    n_sources = len(means)
    has_multiepoch = n_epochs >= 3

    print(f"\n  === CANDIDATE SUMMARY ===")
    print(f"  PS1 sources in field: {n_sources}")
    print(f"  Total epoch detections: {n_epochs}")

    if n_epochs == 0:
        print(f"  STATUS: Sources exist in stacks but NO individual epoch detections")
        print(f"  INTERPRETATION: Faint sources (mag>21) only visible in deep stacks.")
        print(f"  Qwen's compare_images worked on WARP single-epoch images where")
        print(f"  flux differences at the noise level can produce high sigma values")
        print(f"  if the background subtraction differs between epochs.")
        print(f"  RISK: Could be real transient OR noise/artifact in difference imaging")
    elif has_multiepoch:
        print(f"  STATUS: Multi-epoch data available! Can verify variability claim.")
    else:
        print(f"  STATUS: Very few epoch detections ({n_epochs}). Limited light curve data.")

print(f"\n\n{'='*80}")
print("LIGHT CURVE CHECK COMPLETE")
print(f"{'='*80}")
