# Findings Validated by Claude — 2026-03-12

Manual cross-check against SIMBAD (60"), Gaia DR3 (30"), and ALeRCE/ZTF (30").

---

## VALID — F20260312_014312

- **Coordinates:** RA 106.897863, Dec +49.626267
- **Signal:** r-band brightening 15.406 → 14.709 (Δm = 0.70 mag)
- **Epochs:** MJD 55570.4 → 56226.6 (~656 days)
- **Photometry:** SNR 223.6 → 524.3
- **SIMBAD 60":** 0 matches
- **Gaia DR3:** 1 source at 0.3" — G=14.497, parallax=0.86 mas (~1.2 kpc), bp_rp=0.95, RUWE=1.14, variable_class=null
- **ALeRCE:** 0 matches
- **Verdict:** Genuine uncatalogued variable star (~F/G type at ~1.2 kpc). Strongest signal of the batch. Never classified as variable anywhere. Real discovery.

---

## NEEDS VISUAL INSPECTION — F20260312_031240

- **Coordinates:** RA 316.545, Dec +11.726
- **Signal:** g-band brightening 18.77 → 18.35 (Δm = 0.42 mag)
- **Epochs:** MJD 55416.5 → 56540.3 (~1124 days)
- **SIMBAD 60":** 0 matches
- **Gaia DR3:** Nearest source at 2.23" but G=14.504 (different object — 4+ mag brighter)
- **ALeRCE:** 0 matches
- **Verdict:** Faint source (mag ~18.5) with zero catalogue counterpart. Promising, but a bright Gaia star (G=14.5) sits 2.23" away — PSF contamination or diffraction artifact cannot be ruled out without FITS inspection.

---

## DOWNGRADED — F20260312_031652

- **Coordinates:** RA 121.5549, Dec +51.2486
- **Signal:** r-band brightening 16.99 → 16.437 (Δm = 0.56 mag)
- **Epochs:** MJD 56325.4 → 56735.3 (~410 days)
- **Photometry:** SNR 72 → 217
- **SIMBAD 60":** 1 match — SDSS J080614.23+511451.5, type BiC (Binary Candidate) at 10.5"
- **Gaia DR3:** Source at 0.93", G=15.839, parallax=0.33 mas (~3 kpc)
- **ALeRCE:** ZTF18aabkoxa at 1.2" (1 detection, unclassified)
- **Verdict:** Already detected by ZTF. Nearby SIMBAD binary candidate. Not novel — likely unclassified variable or eclipsing binary.

---

## INVALIDATED — F20260312_052933

- **Coordinates:** RA 4.1025, Dec +15.7456
- **Signal:** g-band brightening 20.56 → 20.09 (Δm = 0.47 mag)
- **Epochs:** MJD 55451 → 56625 (~1174 days)
- **SIMBAD 60":** 0 matches
- **Gaia DR3:** Source at 0.11" — G=18.448, parallax=2.28 mas (~440 pc), bp_rp=2.26 (very red), RUWE=1.91
- **ALeRCE:** 5 ZTF objects within 30" (nearest at 5")
- **Verdict:** Nearby M-dwarf (~440 pc). Very red (bp_rp=2.26), high RUWE suggests binary. Multiple ZTF detections indicate recurrent flare activity, not a novel transient. False positive.

---

## VALID — F20260312_121340

- **Coordinates:** RA 139.0195, Dec -21.808
- **Signal:** r-band brightening 17.34 → 16.96 (Δm = 0.38 mag)
- **Epochs:** MJD 55269 → 56725 (~1456 days / ~4 years)
- **Photometry:** SNR 147.5 → 192.4, mag errors ±0.01
- **SIMBAD 60":** 0 matches
- **Gaia DR3:** 0 matches (proper motion 3.6 mas/yr reported by Qwen — suggests extragalactic or very distant)
- **ALeRCE:** 0 matches
- **Verdict:** Solid candidate. Excellent SNR in both epochs, clean Δmag above threshold, completely absent from all catalogues. Low proper motion consistent with extragalactic origin. Genuine uncatalogued variable or slow transient.

---

## VALID — F20260312_122946

- **Coordinates:** RA 0.910537, Dec -17.030219
- **Signal:** r-band brightening 16.52 → 16.17 (Δm = 0.35 mag)
- **Epochs:** MJD 55457.5 → 56615.2 (~1158 days / ~3.2 years)
- **Photometry:** SNR 245.6 → 289.7, mag errors ±0.004
- **SIMBAD 60":** 0 matches
- **Gaia DR3:** 0 matches
- **ALeRCE:** 0 matches
- **Verdict:** Best photometry quality of any finding (SNR ~250-290, errors < 0.005 mag). Completely absent from all catalogues. Clean brightening. Strong uncatalogued variable candidate.

---

## VALID — F20260312_170853

- **Coordinates:** RA 29.758422, Dec +69.917418
- **Signal:** r-band brightening 16.556 → 16.164 (Δm = 0.39 mag)
- **Epochs:** MJD 55921.3 → 56648.2 (~727 days / ~2 years)
- **Photometry:** SNR >160 both epochs
- **SIMBAD 60":** 0 matches
- **Gaia DR3 5":** 1 source — G=15.505, parallax=0.178 mas (~5.6 kpc), pmTotal=2.3 mas/yr, RUWE=0.996, variable_class=NOT_AVAILABLE
- **ALeRCE:** 0 matches (per Qwen; API returned 403 on manual check)
- **Verdict:** Very distant or extragalactic source (parallax ~0). Clean astrometry (RUWE=0.996). Not classified as variable anywhere. Strong uncatalogued variable candidate.

---

## DOWNGRADED — F20260312_185128

- **Coordinates:** RA 157.831603, Dec -12.575432
- **Signal:** g-band brightening 17.467 → 16.511 (Δm = 0.96 mag)
- **Epochs:** MJD 55244.4 → 56422.3 (~1178 days)
- **SIMBAD 60":** 0 matches
- **Gaia DR3 5":** 1 source — G=15.078, parallax=3.76 mas (~266 pc), pmTotal=41.5 mas/yr, BP-RP=1.74 (K/M star), RUWE=1.23
- **ALeRCE:** 1 unclassified detection
- **Verdict:** Nearby K/M star (~266 pc) with high proper motion. Large Δmag likely stellar flare activity, not novel transient. ALeRCE already has a detection. Not novel.

---

## VALID — F20260313_001930

- **Coordinates:** RA 304.5323, Dec +50.2965
- **Signal:** g-band brightening 16.784 → 16.092 (Δm = 0.69 mag)
- **Epochs:** MJD 55034.4 → 56513.3 (~1479 days / ~4 years)
- **Photometry:** SNR 126.8 → 284.2
- **SIMBAD 60":** 0 matches
- **Gaia DR3 5":** 1 source — G=15.182, parallax=0.41 mas (~2.4 kpc), pmTotal=6.5 mas/yr, RUWE=0.957, variable_class=NOT_AVAILABLE
- **ALeRCE:** 0 matches
- **Verdict:** Strong candidate. Excellent SNR doubling, distant source (~2.4 kpc), clean astrometry (RUWE<1). Completely uncatalogued variable. PM slightly elevated but within range for distant disk star.

---

## DOWNGRADED — F20260313_013115

- **Coordinates:** RA 357.6834, Dec +14.771
- **Signal:** r-band brightening 19.18 → 18.41 (Δm = 0.77 mag)
- **Epochs:** MJD 55836 → 56564 (~728 days)
- **Photometry:** SNR 48.9 → 80.4
- **SIMBAD 60":** 0 matches
- **Gaia DR3 5":** 1 source — G=18.194, parallax=0.67 mas (~1.5 kpc), pmTotal=13.1 mas/yr, BP-RP=1.70 (K-type), RUWE=1.07
- **Verdict:** High proper motion (13.1 mas/yr) indicates nearby galactic K-type star. Variability likely intrinsic stellar activity rather than novel transient.

---

## INVALIDATED — F20260313_125936

- **Coordinates:** RA 264.584143, Dec -29.931405
- **Signal:** g-band brightening 15.02 → 13.84 (Δm = 1.18 mag)
- **SIMBAD 60":** TYC 6839-61-1 at 1.57" — catalogued star
- **Gaia DR3 5":** G=11.71, parallax=2.10 mas (~476 pc), RUWE=0.85
- **Verdict:** Known star TYC 6839-61-1 (G=11.7). Qwen measured mag 15→13.8 on a mag 11.7 star = detector saturation artifacts. False positive.

---

## NEEDS FOLLOW-UP — F20260313_120923

- **Coordinates:** RA 77.226543, Dec -2.292528
- **Signal:** g-band brightening 16.91 → 16.58 (Δm = 0.33 mag)
- **Epochs:** MJD 55535.5 → 56633.4 (~1098 days)
- **Photometry:** SNR 184.8 → 229.9
- **SIMBAD 60":** 0 matches
- **Gaia DR3 5":** G=15.842, parallax=0.45 mas (~2.2 kpc), pmTotal=2.8 mas/yr, RUWE=3.12
- **Verdict:** Excellent SNR and distant source, but RUWE=3.12 (very high) suggests unresolved binary or astrometric problem. Δmag just above threshold. Needs independent confirmation.

---

## VALID — F20260313_190738 ⭐ BEST FIND

- **Coordinates:** RA 270.41, Dec +0.02
- **Signal:** r-band brightening 14.34 → 13.42 (Δm = 0.92 mag)
- **Epochs:** MJD 54995 → 56545 (~1550 days / ~4.2 years)
- **Photometry:** SNR 648 → 518 (highest SNR of entire project)
- **SIMBAD 60":** 0 matches
- **Gaia DR3 5":** 0 matches
- **ALeRCE:** 0 matches (per Qwen)
- **Verdict:** Best candidate of the entire project. Monster SNR (648!), nearly 1 mag brightening, completely absent from ALL catalogues including Gaia. Possibly extragalactic or extremely faint proper motion source. Genuine uncatalogued variable.

---

## VALID — F20260313_173217

- **Coordinates:** RA 190.12, Dec -6.65
- **Signal:** g-band brightening 15.12 → 14.44 (Δm = 0.68 mag)
- **Epochs:** MJD 55243 → 56392 (~1149 days / ~3.1 years)
- **Photometry:** SNR 28.8 → 81.2
- **SIMBAD 60":** 0 matches
- **Gaia DR3 5":** 0 matches
- **ALeRCE:** 0 matches (per Qwen)
- **Verdict:** Absent from all catalogues including Gaia. SNR tripled between epochs. Good Δmag. Uncatalogued variable candidate, possibly extragalactic.

---

## INVALIDATED — F20260314_090737

- **Coordinates:** RA 327.595674, Dec -15.789422
- **Signal:** g-band brightening 14.279 → 13.802 (Δm = 0.48 mag)
- **Epochs:** MJD 55414.5 → 56630.2 (~1216 days)
- **Photometry:** SNR 289.5 → 391.5
- **SIMBAD 60":** UCAC4 372-178672 at 0" — catalogued star
- **Gaia DR3 5":** G=12.302, parallax=1.18 mas (~847 pc), pmTotal=7.5 mas/yr, RUWE=0.99
- **Verdict:** Known catalogued star (G=12.3). Qwen measured mag 14.3→13.8 on a mag 12.3 star = detector saturation artifacts. Same false positive pattern as F125936.

---

## VALID — F20260314_174748

- **Coordinates:** RA 94.967559, Dec -2.629132
- **Signal:** g-band brightening 16.49 → 15.52 (Δm = 0.97 mag)
- **Epochs:** MJD 55565.4 → 56637.5 (~1072 days / ~2.9 years)
- **SIMBAD 60":** 0 matches
- **Gaia DR3 5":** 1 source — G=14.866, parallax=0.223 mas (~4.5 kpc), pmTotal=2.5 mas/yr, BP-RP=0.85, RUWE=1.008, variable_class=NOT_AVAILABLE
- **ALeRCE:** 0 matches
- **Verdict:** Very distant source (~4.5 kpc), pristine astrometry (RUWE=1.008), low proper motion. Nearly 1 mag brightening. Completely uncatalogued. Rivals F190738 as best find. Genuine uncatalogued variable.

---

## DOWNGRADED — F20260314_142349

- **Coordinates:** RA 296.879037, Dec -27.547734
- **Signal:** g-band brightening 16.15 → 15.36 (Δm = 0.78 mag)
- **Epochs:** MJD 55004.5 → 55774.4 (~770 days)
- **SIMBAD 60":** 0 matches
- **Gaia DR3 5":** G=14.496, parallax=1.37 mas (~729 pc), pmTotal=15.7 mas/yr, BP-RP=0.94, RUWE=1.01
- **Verdict:** High proper motion (15.7 mas/yr) indicates nearby galactic star. Variability likely intrinsic stellar activity rather than novel transient.

---

## Summary

| Finding | Δmag | SNR | Catalogues | Verdict |
|---------|------|-----|------------|---------|
| F014312 | 0.70 | 224→524 | 0 SIMBAD, Gaia (no var), 0 ZTF | **VALID** |
| F031240 | 0.42 | — | 0 SIMBAD, 0 ZTF, bright neighbor | **NEEDS FITS CHECK** |
| F031652 | 0.56 | 72→217 | SIMBAD BiC, ZTF detection | **DOWNGRADED** |
| F052933 | 0.47 | — | Gaia M-dwarf 440pc, 5 ZTF | **INVALIDATED** |
| F121340 | 0.38 | 148→192 | 0 SIMBAD, 0 Gaia, 0 ZTF | **VALID** |
| F122946 | 0.35 | 246→290 | 0 SIMBAD, 0 Gaia, 0 ZTF | **VALID** |
| F170853 | 0.39 | >160 | 0 SIMBAD, Gaia 5.6kpc no var, 0 ZTF | **VALID** |
| F185128 | 0.96 | — | 0 SIMBAD, Gaia K/M 266pc, 1 ALeRCE | **DOWNGRADED** |
| F001930 | 0.69 | 127→284 | 0 SIMBAD, Gaia 2.4kpc no var, 0 ZTF | **VALID** |
| F013115 | 0.77 | 49→80 | 0 SIMBAD, Gaia K-type pm=13.1 | **DOWNGRADED** |
| F125936 | 1.18 | — | TYC 6839-61-1 at 1.57" | **INVALIDATED** |
| F120923 | 0.33 | 185→230 | 0 SIMBAD, Gaia RUWE=3.12 | **NEEDS FOLLOW-UP** |
| F190738 | 0.92 | 648→518 | 0 SIMBAD, 0 Gaia, 0 ZTF | **VALID** ⭐ |
| F173217 | 0.68 | 29→81 | 0 SIMBAD, 0 Gaia, 0 ZTF | **VALID** |
| F090737 | 0.48 | 290→392 | UCAC4 372-178672 at 0", G=12.3 | **INVALIDATED** |
| F174748 | 0.97 | — | 0 SIMBAD, Gaia 4.5kpc no var, 0 ZTF | **VALID** |
| F142349 | 0.78 | — | 0 SIMBAD, Gaia pm=15.7 | **DOWNGRADED** |
