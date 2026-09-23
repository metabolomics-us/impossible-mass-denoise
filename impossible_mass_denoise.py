#!/usr/bin/env python
"""
Formula-free "impossible mass" denoising for MS/MS fragment spectra.

Method: J. Meija, "Mathematical tools in analytical mass spectrometry", Anal Bioanal Chem 385
(2006) 486-499, section 2.1 (Diophantine equations).

For each fragment peak of exact m/z, ask the Diophantine (subset-sum / "coin") question: does ANY
non-negative integer element composition sum to this mass within tolerance, subject to valence?
If no composition exists, the mass is impossible under the configured element alphabet and the
peak is flagged as noise.

  sum_i n_i * m_i = M   (n_i integer >= 0)                 [Meija Eq. 1]
  DBE = C - (H+D+F+Cl+Br+I)/2 + (N+P)/2 + 1  >= -0.5       [valence, Meija 2.2]

It needs only the peak m/z and an element alphabet - no precursor formula, adduct or candidate
structure - so it applies to unannotated spectra, where subformula-of-precursor denoising (which
requires the precursor formula) cannot run. The discriminating signal is the mass defect: organic
ions occupy narrow mass-defect bands, and a peak outside every band has no composition.

A composition must also be chemically legal: SENIOR valence rules, Seven-Golden-Rules element
ratios, per-element caps, and a union of an ion-tolerant and a neutral-strict validity model.
Precomputed lookup tables (CHNOPS, and CHNOPS + halogens) answer the deployed configuration at
microsecond speed; any other configuration falls back to the solver.
"""
import os
import json
import hashlib
import inspect
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
# (research-tree demo paths removed from the packaged copy)

# monoisotopic masses
MASS = dict(C=12.0, H=1.0078250319, D=2.0141017779, N=14.0030740052, O=15.9949146221,
            P=30.97376151, S=31.97207069, Cl=34.96885271, F=18.99840322, Na=22.98976928,
            K=38.96370649, Br=78.9183376, I=126.904473)
M_E = 0.00054858
# valence contribution to DBE: monovalent -> -1/2, trivalent (N,P) -> +1/2, C -> +1
# O, S divalent -> 0.  Na monovalent.
DBE_COEF = dict(C=1.0, H=-0.5, D=-0.5, F=-0.5, Cl=-0.5, Br=-0.5, I=-0.5, Na=-0.5, K=-0.5,
                N=0.5, P=0.5, O=0.0, S=0.0)

# practical caps (Seven Golden Rules + sanity) to bound the search
CAPS = dict(N=20, O=27, P=6, S=8, Cl=10, F=16, Br=4, I=3, Na=2, K=2)
# valences for the SENIOR valence check (Meija 2.2 / Seven Golden Rules)
VAL = dict(C=4, H=1, D=1, N=3, O=2, P=3, S=2, Cl=1, F=1, Br=1, I=1, Na=1, K=1)


def _cfree_ok(h, d, n, o, p, s, cl, f, br, i, na=0, k=0):
    """Carbon-free ion acceptance. Pure RELAXATION of the original flat 6-atom cap:
      * <=6 atoms  -> accept (unchanged original behaviour: small simple anions like CN-, HS-,
        halides, OH-; these gave the good specificity baseline and must not regress).
      * >6 atoms   -> accept ONLY the valence-sane inorganic OXOANION families real fragments
        produce (phosphate/sulfate/nitrate/polyphosphate: H2PO4-, HSO4-, NO3-, P2O7...), kept
        tight (O-bearing, proton-poor, <=3 backbone heteroatoms) so the all-heteroatom junk
        witnesses (e.g. H8N5OS) the old cap blocked stay blocked. Adds phosphate recovery without
        removing any prior acceptance -> can only lower NIST false positives, never raise them."""
    if na + k > 1:                               # an organic ion carries at most one alkali
        return False
    n_atoms = h + d + n + o + p + s + cl + f + br + i + na + k
    if n_atoms <= 6:
        return True
    hetero = n + p + s
    if hetero > 3 or (f + cl + br + i) > 2:
        return False
    if o < 1 or o > 4 * (p + s) + 3 * n + 1:   # oxoanion must carry O, bounded by P/S/N valence
        return False
    if (h + d) > hetero + 2:                    # oxoanions are proton-poor
        return False
    return True


def _valid(c, h, d, n, o, p, s, cl, f, br=0, i=0, na=0, k=0):
    """SENIOR valence rule + Seven-Golden-Rules element ratios. Prunes the exotic
    all-heteroatom compositions the raw Diophantine solve would otherwise accept."""
    counts = dict(C=c, H=h, D=d, N=n, O=o, P=p, S=s, Cl=cl, F=f, Br=br, I=i, Na=na, K=k)
    present = {k: v for k, v in counts.items() if v > 0}
    if not present:
        return False
    # SENIOR: sum of valences >= 2*max_valence and >= 2*(n_atoms - 1)
    sum_val = sum(v * VAL[k] for k, v in present.items())
    max_val = max(VAL[k] for k in present)
    n_atoms = sum(present.values())
    if sum_val < 2 * max_val or sum_val < 2 * (n_atoms - 1):
        return False
    # Seven Golden Rules element ratios (D counts as H)
    heff = h + d
    if c > 0:
        r = heff / c
        if r > 6 or r < 0.1:
            return False
        if n / c > 4 or o / c > 3 or p / c > 2 or s / c > 3 or (f + cl + br + i) / c > 3 \
                or (na + k) / c > 1:
            return False
    else:
        if not _cfree_ok(h, d, n, o, p, s, cl, f, br, i, na, k):
            return False
    return True


def _valid_strict(c, h, d, n, o, p, s, cl, f, br=0, i=0, na=0, k=0):
    """CONSISTENTLY STRICT validity test, applied to a NEUTRALIZED composition (the ion's
    ionizing +/-H already undone by the caller). One uniform standard for every peak: the
    formula must be a valid *even-electron, closed-shell neutral molecule* -- no half-unit
    ion slack anywhere.

    Adds, over the lenient _valid():
      * integer DBE >= 0   (a half-integer DBE == odd-electron radical -> rejected). This is
        exactly equivalent to SENIOR's even-valence-sum (parity / handshake) rule, which the
        lenient path silently dropped: DBE = (sum_val - 2*n_atoms)/2 + 1, so integer DBE
        <=> sum_val even.
      * the full SENIOR triple (connectivity + no-isolated-atom + parity) rather than the
        lenient pair.
    Keeps the Seven-Golden-Rules element-ratio pruning."""
    dbe = c - (h + d + f + cl + br + i + na + k) / 2.0 + (n + p) / 2.0 + 1
    if dbe < 0:
        return False
    if abs(dbe - round(dbe)) > 1e-9:          # half-integer DBE = radical -> strict reject
        return False
    counts = dict(C=c, H=h, D=d, N=n, O=o, P=p, S=s, Cl=cl, F=f, Br=br, I=i, Na=na, K=k)
    present = {k: v for k, v in counts.items() if v > 0}
    if not present:
        return False
    sum_val = sum(v * VAL[k] for k, v in present.items())
    max_val = max(VAL[k] for k in present)
    n_atoms = sum(present.values())
    # SENIOR (i) no isolated atom, (ii) connected, (iii) even valence sum (parity)
    if sum_val < 2 * max_val or sum_val < 2 * (n_atoms - 1) or sum_val % 2 != 0:
        return False
    heff = h + d
    if c > 0:
        r = heff / c
        if r > 6 or r < 0.1:
            return False
        if n / c > 4 or o / c > 3 or p / c > 2 or s / c > 3 or (f + cl + br + i) / c > 3 \
                or (na + k) / c > 1:
            return False
    else:
        if not _cfree_ok(h, d, n, o, p, s, cl, f, br, i, na, k):
            return False
    return True


_CACHE = {}

# ---------------------------------------------------------------------------
# Precomputed possible-mass table (built by build_possible_mass_table.py, wired 2026-09-04)
#
# possible() is a pure function of m/z for a fixed (mode, config), so the DEPLOYED default
# config is enumerated once onto a 1 mDa grid and looked up instead of re-solved per peak
# (~150 ms -> ~14 us). The table is consulted ONLY when the call matches the config it was
# built for AND the chemistry fingerprint stored in the file matches this module's validity
# functions -- a stale or foreign table can never silently outvote the solver. Any other
# call (deuterated, halogens, alkali, single-model, sub-2-mDa tolerance, m/z above the table)
# falls through to the Diophantine search unchanged.
# Packaged copy: the tables ship beside this file. The research-tree paths are the fallback,
# so this module behaves identically when run from the research repository.
_HERE = Path(__file__).resolve().parent
TABLE_PATH = (_HERE / "possible_mass_table.npz" if (_HERE / "possible_mass_table.npz").exists()
              else ROOT / "data" / "results/denoise" / "possible_mass_table.npz")
TABLE_CONFIG = dict(d_max=0, use_halogens=False, alkali=False, union=True)
TABLE_MIN_TOL = 0.002        # below 2 mDa the 1 mDa grid is too coarse -> solver
USE_TABLE = not os.environ.get("IMD_NO_TABLE")   # IMD_NO_TABLE=1 forces the solver (parity checks)
_TABLE = None
_TABLE_TRIED = False
_TABLE_STATUS = "not loaded"
_TCACHE = {}

# Halogen-aware twin (2026-09-23): same chemistry, same fingerprint, Cl/F/Br/I in the alphabet.
# Built by build_possible_mass_table_halogen.py. Kept as a separate file and separate globals so
# the deployed CHNOPS path cannot be affected by its presence or absence.
HALOGEN_TABLE_PATH = (_HERE / "possible_mass_table_halogen.npz"
                      if (_HERE / "possible_mass_table_halogen.npz").exists()
                      else ROOT / "data" / "results/denoise" / "possible_mass_table_halogen.npz")
HALOGEN_TABLE_CONFIG = dict(d_max=0, use_halogens=True, alkali=False, union=True)
_HTABLE = None
_HTABLE_TRIED = False
_HTABLE_STATUS = "not loaded"


def chemistry_fingerprint():
    """Hash of the validity chemistry (rules + element constants). Stored in the table at
    build time and re-checked at load, so editing a rule invalidates the table instead of
    quietly disagreeing with it."""
    src = "".join(inspect.getsource(f) for f in (_cfree_ok, _valid, _valid_strict))
    src += repr(sorted(CAPS.items())) + repr(sorted(VAL.items()))
    src += repr(sorted(MASS.items())) + repr(M_E)
    return hashlib.sha256(src.encode()).hexdigest()[:16]


def _read_table(src, config, builder):
    """Shared loader body. Returns (table dict or None, human-readable status)."""
    try:
        import numpy as np
    except ImportError:
        return None, "unusable: numpy not importable"
    if not src.exists():
        return None, f"unusable: {src} missing (run {builder})"
    z = np.load(src, allow_pickle=False)
    have = set(z.files)
    need = {"bins_neg", "bins_pos", "off_lo_neg", "off_hi_neg", "off_lo_pos", "off_hi_pos",
            "bin_da", "max_mz", "fingerprint", "config"}
    if not need <= have:
        return None, f"unusable: {src.name} predates the fingerprint/config header -> rebuild"
    fp_file, fp_code = str(z["fingerprint"]), chemistry_fingerprint()
    if fp_file != fp_code:
        return None, (f"unusable: chemistry fingerprint {fp_file} != {fp_code} "
                      "(validity rules changed) -> rebuild")
    cfg = json.loads(str(z["config"]))
    if cfg != config:
        return None, f"unusable: table config {cfg} != {config}"
    t = dict(neg=z["bins_neg"], pos=z["bins_pos"],
             lo_neg=z["off_lo_neg"], hi_neg=z["off_hi_neg"],
             lo_pos=z["off_lo_pos"], hi_pos=z["off_hi_pos"],
             bin=float(z["bin_da"]), max_mz=float(z["max_mz"]))
    return t, (f"loaded {src.name}: {t['bin']*1000:.0f} mDa grid to "
               f"{t['max_mz']:.0f} Da, fingerprint {fp_file}")


def load_table(path=None):
    """Load the precomputed CHNOPS table, or return None and record why in table_status()."""
    global _TABLE, _TABLE_TRIED, _TABLE_STATUS
    if path is None and _TABLE_TRIED:
        return _TABLE
    _TABLE_TRIED = True
    src = Path(path) if path else TABLE_PATH
    _TABLE, _TABLE_STATUS = _read_table(src, TABLE_CONFIG, "build_possible_mass_table.py")
    return _TABLE


def load_halogen_table(path=None):
    """Load the halogen-aware table, or return None and record why in halogen_table_status()."""
    global _HTABLE, _HTABLE_TRIED, _HTABLE_STATUS
    if path is None and _HTABLE_TRIED:
        return _HTABLE
    _HTABLE_TRIED = True
    src = Path(path) if path else HALOGEN_TABLE_PATH
    _HTABLE, _HTABLE_STATUS = _read_table(src, HALOGEN_TABLE_CONFIG,
                                          "build_possible_mass_table_halogen.py")
    return _HTABLE


def table_status():
    """Human-readable state of the lookup table (loads it on first call)."""
    load_table()
    return _TABLE_STATUS


def halogen_table_status():
    """Human-readable state of the halogen-aware table (loads it on first call)."""
    load_halogen_table()
    return _HTABLE_STATUS


def _table_lookup(mz, tol, mode, d_max, use_halogens, alkali, union):
    """Table verdict for a table-eligible call; None when the call is out of scope.

    EXACT, not approximate. Each 1 mDa bin stores whether a valid composition falls in it
    plus that bin's lowest and highest composition mass (uint16 offsets, 1.5e-8 Da),
    so the +/-tol test is done on real masses, never on bin indices. Sufficiency: a bin is
    1 mDa wide and the query window is 2*tol >= 4 mDa, so a window overlapping a bin's
    [lowest, highest] span must contain one of those two endpoints -- checking the two
    extremes of every set bin in the window decides the question exactly."""
    if not USE_TABLE or not union or d_max or alkali:
        return None
    if mode not in ("neg", "pos") or tol < TABLE_MIN_TOL:
        return None
    t = load_halogen_table() if use_halogens else load_table()
    if t is None:
        return None
    if not 0 < mz <= t["max_mz"] - tol:
        return None
    import numpy as np
    bins, b = t[mode], t["bin"]
    lo = max(int((mz - tol) / b) - 1, 0)
    hi = min(int((mz + tol) / b) + 2, bins.size)
    window = bins[lo:hi]
    if not window.any():
        return False
    idx = np.flatnonzero(window) + lo
    step = b / 65535.0
    m_lo = idx * b + t["lo_" + mode][idx] * step
    m_hi = idx * b + t["hi_" + mode][idx] * step
    return bool(((m_hi >= mz - tol) & (m_lo <= mz + tol)).any())


def possible(mz, tol=0.005, mode="neg", d_max=0, use_halogens=False, strict=False, union=True,
             alkali=False, need_formula=False):
    """Cached wrapper around the Diophantine feasibility test (m/z rounded for the key;
    huge speedup on large runs where fragment masses recur).

    Three validity models (a candidate composition matching the ion mass must also be
    chemically legal):
      union=True (DEFAULT): a peak is possible if EITHER the lenient ion-tolerant check OR the
        strict +/-H-neutralized valid-neutral check accepts it, so it is flagged impossible only
        when BOTH reject it (no chemical explanation either way). This is the most conservative
        flagging, and it fixes the false-flag classes each single model has: saturated
        quaternary-amine cations (lenient-only) and acylium/radical fragments (strict-only).
      union=False, strict=False: lenient single model (DBE>=-0.5 slack, SENIOR pair, on the ion).
        Over-flags saturated N-cations (choline/TMAO/carnitine base peaks).
      union=False, strict=True: neutralize the ion (undo +/-H), require a valid even-electron
        neutral (integer DBE>=0 + full SENIOR incl. parity). Over-flags acylium/radical cations.
    union overrides strict, so for a single model you MUST pass union=False.

    Deployed-default calls are answered by the precomputed mass table when it is available
    (see load_table); those return (verdict, None) because the table stores reachable MASSES,
    not witness formulas -- True with a None formula means "not computed", False still means
    "no composition exists". Pass need_formula=True to force the solver and get the witness."""
    key = (round(mz, 5), tol, mode, d_max, use_halogens, strict, union, alkali)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    # _TCACHE is the table fast path and is keyed ONLY by (m/z, tol, mode), so it is valid
    # exclusively for calls that match the config the table was built for. Consulting it for a
    # single-model or wider-alphabet call returns the UNION/CHNOPS answer for that mass and
    # silently overrides the requested model (found 2026-09-16: reordering the NIST23 bench to
    # score union first made lenient and strict report union's numbers verbatim).
    # Each alphabet has its own table now (CHNOPS, and CHNOPS+halogens since 2026-09-23), so
    # use_halogens no longer disqualifies a call - but it MUST be in the fast-path key, or a
    # halogen-aware answer would be served to a CHNOPS call for the same mass (and vice versa):
    # exactly the cross-config leak described above.
    _table_eligible = (union
                       and d_max == TABLE_CONFIG["d_max"]
                       and alkali == TABLE_CONFIG["alkali"])
    if not need_formula and _table_eligible:
        tkey = (round(mz, 5), tol, mode, bool(use_halogens))
        hit = _TCACHE.get(tkey)
        if hit is not None:
            return hit
        verdict = _table_lookup(mz, tol, mode, d_max, use_halogens, alkali, union)
        if verdict is not None:
            hit = (verdict, None)
            _TCACHE[tkey] = hit
            return hit
    hit = _possible_uncached(mz, tol, mode, d_max, use_halogens, strict, union, alkali)
    _CACHE[key] = hit
    return hit


M_P = 1.00727646688   # proton mass, for the doubly-charged interpretation below


def possible_z2(mz, tol=0.005, mode="neg", **kw):
    """possible() extended with a DOUBLY-CHARGED interpretation. OPT-IN, not the deployed rule.

    A peak is accepted if it is a possible z=1 ion, OR if the z=1 ion of the same neutral,
    [M+H]+ = 2*m/z - m_p (positive) / [M-H]- = 2*m/z + m_p (negative), is possible at twice the
    tolerance (the m/z error doubles when mapped back to z=1). Motivation (2026-09-18,
    diagnose_charge_state_nist23.py): on NIST23 the z=1 filter's false positives are dominated
    by [M+2H]2+ / [M-2H]2- fragments of heavy compounds -- 95% of flagged peaks (99% of flagged
    intensity) in the 650-1700 Da compound band, 51% (75%) in the <=650 band. Cost: above
    ~335 Da the mapped mass lands where every composition mass is occupied, so nothing can be
    flagged there any more, and power at 100-335 Da drops (audit_z2_rule.py quantifies it).
    Returns (verdict, witness_or_None, z) with z in {1, 2, None}."""
    ok, form = possible(mz, tol=tol, mode=mode, **kw)
    if ok:
        return True, form, 1
    mz1 = 2.0 * mz - M_P if mode == "pos" else 2.0 * mz + M_P
    ok2, form2 = possible(mz1, tol=2.0 * tol, mode=mode, **kw)
    if ok2:
        return True, form2, 2
    return False, None, None


def _possible_uncached(mz, tol=0.005, mode="neg", d_max=0, use_halogens=False, strict=False,
                       union=True, alkali=False):
    """Diophantine feasibility: is there >=1 valid element composition for this ion m/z?

    Returns (True, formula_str) or (False, None). Short-circuits on first hit."""
    # ion atoms mass = m/z -/+ electron  (anion has an extra e-, cation is missing one)
    target = mz - M_E if mode == "neg" else mz + M_E
    mH, mC = MASS["H"], MASS["C"]
    c_max = int(target / mC) + 1
    els = ["Cl", "F", "Br", "I"] if use_halogens else []
    if alkali:
        els = els + ["Na", "K"]
    # iterate heavy atoms with mass pruning; solve H (densest) as the free integer
    for c in range(c_max + 1):
        mc = c * mC
        if mc - tol > target:
            break
        for n in range(min(CAPS["N"], int((target - mc) / MASS["N"]) + 1) + 1):
            mn = mc + n * MASS["N"]
            if mn - tol > target:
                break
            for o in range(min(CAPS["O"], int((target - mn) / MASS["O"]) + 1) + 1):
                mo = mn + o * MASS["O"]
                if mo - tol > target:
                    break
                for p in range(min(CAPS["P"], int((target - mo) / MASS["P"]) + 1) + 1):
                    mp = mo + p * MASS["P"]
                    if mp - tol > target:
                        break
                    for s in range(min(CAPS["S"], int((target - mp) / MASS["S"]) + 1) + 1):
                        ms = mp + s * MASS["S"]
                        if ms - tol > target:
                            break
                        # halogens
                        for cl in range(_cap("Cl", els, target, ms) + 1):
                            mcl = ms + cl * MASS["Cl"]
                            if mcl - tol > target:
                                break
                            for f in range(_cap("F", els, target, mcl) + 1):
                                mf = mcl + f * MASS["F"]
                                if mf - tol > target:
                                    break
                                for br in range(_cap("Br", els, target, mf) + 1):
                                    mbr = mf + br * MASS["Br"]
                                    if mbr - tol > target:
                                        break
                                    for i in range(_cap("I", els, target, mbr) + 1):
                                        mi = mbr + i * MASS["I"]
                                        if mi - tol > target:
                                            break
                                        for na in range(_cap("Na", els, target, mbr + i * MASS["I"]) + 1):
                                            mna = mbr + i * MASS["I"] + na * MASS["Na"]
                                            if mna - tol > target:
                                                break
                                            for k in range(_cap("K", els, target, mna) + 1):
                                                mk = mna + k * MASS["K"]
                                                if mk - tol > target:
                                                    break
                                                for d in range(d_max + 1):
                                                    md = mk + d * MASS["D"]
                                                    rem = target - md
                                                    if rem < -tol:
                                                        break
                                                    h = round(rem / mH)
                                                    if h < 0:
                                                        continue
                                                    mass = md + h * mH
                                                    if abs(mass - target) <= tol:
                                                        lenient_ok = strict_ok = False
                                                        # strict: neutralize the singly-charged ion ([M-H]-
                                                        # -> M has one MORE protium; [M+H]+ -> one FEWER)
                                                        # and require a valid even-electron neutral.
                                                        if strict or union:
                                                            h_neu = h + 1 if mode == "neg" else h - 1
                                                            strict_ok = (h_neu >= 0 and
                                                                         _valid_strict(c, h_neu, d, n, o, p, s, cl, f, br, i, na, k))
                                                        # lenient: DBE>=-0.5 slack + SENIOR pair on the ion
                                                        if (not strict) or union:
                                                            dbe = (c - (h + d + f + cl + br + i + na + k) / 2.0
                                                                           + (n + p) / 2.0 + 1)
                                                            lenient_ok = (dbe >= -0.5 and
                                                                          _valid(c, h, d, n, o, p, s, cl, f, br, i, na, k))
                                                        if lenient_ok or strict_ok:
                                                            return True, _fmt(c, h, d, n, o, p, s, cl, f, br, i, na, k)
    return False, None


ALKALI_ADDUCT_TOKENS = ("+na", "+2na", "+k", "+2k", "-2h+na", "+na-2h", "+k-2h", "-h+na", "-h+k")


def alkali_from_adduct(adduct):
    """Should the fragment alphabet include Na/K for a spectrum with this precursor adduct?

    Fragments are built from the precursor ion's atoms, so a fragment can only carry an alkali
    if the precursor did. Enabling Na/K unconditionally is measurably WRONG: on the Min Level-1
    set -- verified compounds containing no halogen and no alkali adduct -- a widened alphabet
    still "explains" 7.3% of the peaks the filter removes, i.e. it is pure permissiveness.
    Returns None when the adduct is unknown, so the caller picks the policy."""
    if not adduct or not str(adduct).strip():
        return None
    a = str(adduct).strip().lower().replace(" ", "")
    if "na" not in a and "k" not in a:
        return False
    # guard against element symbols inside a name-like token (e.g. "[M+ACN+H]+")
    return any(t in a for t in ALKALI_ADDUCT_TOKENS) or a.endswith("+na") or a.endswith("+k")


def _cap(el, els, target, running):
    if el not in els:
        return 0
    return min(CAPS[el], int((target - running) / MASS[el]) + 1)


def _fmt(c, h, d, n, o, p, s, cl, f, br=0, i=0, na=0, k=0):
    parts = [("C", c), ("H", h), ("D", d), ("N", n), ("O", o), ("P", p),
             ("S", s), ("Cl", cl), ("F", f), ("Br", br), ("I", i), ("Na", na), ("K", k)]
    return "".join(f"{e}{cnt}" if cnt > 1 else e for e, cnt in parts if cnt > 0)
