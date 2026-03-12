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

## Summary

| Finding | Δmag | SNR | Catalogues | Verdict |
|---------|------|-----|------------|---------|
| F014312 | 0.70 | 224→524 | 0 SIMBAD, Gaia (no var), 0 ZTF | **VALID** |
| F031240 | 0.42 | — | 0 SIMBAD, 0 ZTF, bright neighbor | **NEEDS FITS CHECK** |
| F031652 | 0.56 | 72→217 | SIMBAD BiC, ZTF detection | **DOWNGRADED** |
| F052933 | 0.47 | — | Gaia M-dwarf 440pc, 5 ZTF | **INVALIDATED** |
