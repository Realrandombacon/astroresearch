# -*- coding: utf-8 -*-
"""Analyze findings from AstroResearch runs."""
import json, os, re, sys
sys.stdout.reconfigure(encoding='utf-8')

findings_dir = 'findings'
findings = []
for f in sorted(os.listdir(findings_dir)):
    if f.endswith('.json'):
        data = json.load(open(os.path.join(findings_dir, f), encoding='utf-8'))
        findings.append(data)

print(f"Total findings loaded: {len(findings)}")
from collections import Counter
sig_counts = Counter(f.get('significance','?') for f in findings)
for s in ['high', 'medium', 'low']:
    print(f"  {s}: {sig_counts.get(s, 0)}")

# ---- TIER 1: Validated novel transients ----
print()
print("=" * 60)
print("TIER 1: VALIDATED NOVEL TRANSIENT CANDIDATES")
print("(Not in Gaia DR3 + Not in ALeRCE/ZTF)")
print("=" * 60)
tier1 = []
for f in findings:
    if f.get('significance') != 'high':
        continue
    desc = f.get('description', '').lower()
    has_no_gaia = ('not' in desc or '0 sources' in desc) and 'gaia' in desc
    has_no_alerce = ('not' in desc or '0 match' in desc) and ('alerce' in desc or 'ztf' in desc)
    if has_no_gaia and has_no_alerce:
        tier1.append(f)

for f in tier1:
    sigma_match = re.search(r'sigma[=~: ]*(-?\d+\.?\d*)', f.get('description',''))
    sig_str = f' sigma={sigma_match.group(1)}' if sigma_match else ''
    print(f"\n  {f['id']}  RA={f['ra']}, Dec={f['dec']}{sig_str}")
    print(f"    {f['description'][:220]}")

print(f"\n>>> Total Tier 1 (validated novel): {len(tier1)}")

# ---- TIER 2: Highest sigma detections ----
print()
print("=" * 60)
print("TIER 2: HIGHEST SIGMA DETECTIONS (|sigma| > 50)")
print("=" * 60)
tier2 = []
for f in findings:
    if f.get('significance') != 'high':
        continue
    desc = f.get('description', '')
    sigma_match = re.search(r'sigma[=~: ]*(-?\d+\.?\d*)', desc)
    if sigma_match:
        try:
            sig = abs(float(sigma_match.group(1)))
            if sig > 50:
                tier2.append((sig, f))
        except:
            pass

tier2.sort(key=lambda x: x[0], reverse=True)
for sig, f in tier2[:15]:
    print(f"\n  sigma={sig:>8.2f}  {f['id']}  RA={f['ra']}, Dec={f['dec']}")
    print(f"    {f['description'][:200]}")

print(f"\n>>> Total with |sigma| > 50: {len(tier2)}")

# ---- TIER 3: Color anomalies ----
print()
print("=" * 60)
print("TIER 3: COLOR ANOMALIES")
print("=" * 60)
color = [f for f in findings if 'color' in f.get('description','').lower() and f.get('significance') in ('high','medium')]
for f in color[:10]:
    print(f"\n  {f['id']}  RA={f['ra']}, Dec={f['dec']}  [{f['significance']}]")
    print(f"    {f['description'][:200]}")
print(f"\n>>> Total color anomalies: {len(color)}")

# ---- Summary ----
print()
print("=" * 60)
print("SUMMARY")
print("=" * 60)
new_run = [f for f in findings if f.get('timestamp', '') >= '2026-03-10 22:']
print(f"Findings from reflection-loop run: {len(new_run)}")
new_sig = Counter(f.get('significance','?') for f in new_run)
print(f"  high: {new_sig.get('high',0)}, medium: {new_sig.get('medium',0)}, low: {new_sig.get('low',0)}")
new_tier1 = [f for f in tier1 if f.get('timestamp','') >= '2026-03-10 22:']
print(f"  validated novel (Tier 1): {len(new_tier1)}")
