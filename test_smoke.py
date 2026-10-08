"""Smoke and regression tests: run `python test_smoke.py`. Exits non-zero on any failure.

Checks the tables and chemistry fingerprint, a panel of 44 textbook ions, impossible masses,
spectrum handling, table/solver agreement and throughput. It also carries one case for each
failure class found in the LC-BinBase audit (issues #1 to #5). Each line prints one of:

  PASS   the check holds.
  FAIL   the check does not hold. The run fails.
  XFAIL  a known failure from the audit that is not fixed yet. Reported; the run still passes.
  XPASS  an expected failure now passes. The run fails, so the change that fixes a case also
         turns it into a regular check.

Fixture masses are exact monoisotopic masses calculated from formulas, never copied from
measured spectra.
"""
import inspect
import json
import os
import re
import subprocess
import sys
import time

from denoise import (denoise_spectrum, is_possible, count_deuterium, fingerprint, table_status,
                     halogen_table_status, cache_size, clear_cache)

EXPECTED_FINGERPRINT = "5aae4ec26694da4d"   # same chemistry for both tables
fails = []
xfails = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
    if not ok:
        fails.append(name)


def expect_failure(issue, name, ok, detail=""):
    """A failure class from the audit that is not fixed yet. Passing is an error (XPASS)."""
    label = f"{name} ({issue})"
    if ok:
        print(f"  XPASS  {label}  now passes: make it a regular check")
        fails.append(label)
    else:
        print(f"  XFAIL  {label}{('  ' + detail) if detail else ''}")
        xfails.append(label)


def supports(func, *options):
    """True when func accepts every named keyword option (fixes add options to the API)."""
    params = inspect.signature(func).parameters
    return all(option in params for option in options)


# exact ion m/z from a formula: atom masses minus an electron for a cation, plus one for an anion
ATOM = {"C": 12.0, "H": 1.00782503223, "D": 2.01410177812, "N": 14.00307400443,
        "O": 15.99491461957, "P": 30.97376199842, "S": 31.9720711744, "Cl": 34.968852682,
        "Br": 78.9183376}
ELECTRON = 0.000548579909065


def ion_mz(formula, mode):
    atoms = sum(ATOM[el] * int(n or 1) for el, n in re.findall(r"([A-Z][a-z]?)(\d*)", formula))
    return atoms - ELECTRON if mode == "pos" else atoms + ELECTRON


print(table_status())
check("chemistry fingerprint", fingerprint() == EXPECTED_FINGERPRINT,
      f"{fingerprint()} (expected {EXPECTED_FINGERPRINT})")
check("precomputed table loaded", "loaded" in table_status())
check("halogen table loaded", "loaded" in halogen_table_status())

# real fragments must survive
for mz, mode in [(59.0138, "neg"), (96.9696, "neg"), (145.0506, "neg"),
                 (60.0808, "pos"), (104.1070, "pos"), (184.0733, "pos")]:
    check(f"keeps a real fragment {mz} {mode}", is_possible(mz, mode))

# The 44 textbook ions named in issue #7, written as ion formulas. Phosphocholine appears twice
# in that panel (once as the PC head group), so 43 ions are distinct.
TEXTBOOK_IONS = [
    ("pos", "choline", "C5H14NO"), ("pos", "betaine [M+H]+", "C5H12NO2"),
    ("pos", "TMAO [M+H]+", "C3H10NO"), ("pos", "creatinine [M+H]+", "C4H8N3O"),
    ("pos", "phosphocholine", "C5H15NO4P"), ("pos", "leucine immonium", "C5H12N"),
    ("pos", "proline immonium", "C4H8N"), ("pos", "carnitine [M+H]+", "C7H16NO3"),
    ("pos", "glycine [M+H]+", "C2H6NO2"), ("pos", "arginine [M+H]+", "C6H15N4O2"),
    ("pos", "dimethylammonium", "C2H8N"), ("pos", "cholesterol [M+H-H2O]+", "C27H45"),
    ("pos", "caffeine [M+H]+", "C8H11N4O2"), ("pos", "palmitoylcarnitine [M+H]+", "C23H46NO4"),
    ("pos", "PC head group", "C5H15NO4P"), ("pos", "ethyl", "C2H5"), ("pos", "propyl", "C3H7"),
    ("pos", "hydronium", "H3O"), ("pos", "ammonium", "H4N"), ("pos", "butyl", "C4H9"),
    ("pos", "CH3O+", "CH3O"),
    ("neg", "malate [M-H]-", "C4H5O5"), ("neg", "fumarate [M-H]-", "C4H3O4"),
    ("neg", "lactate [M-H]-", "C3H5O3"), ("neg", "pyruvate [M-H]-", "C3H3O3"),
    ("neg", "acetate", "C2H3O2"), ("neg", "formate", "CHO2"),
    ("neg", "citrate [M-H]-", "C6H7O7"), ("neg", "succinate [M-H]-", "C4H5O4"),
    ("neg", "glutamate [M-H]-", "C5H8NO4"), ("neg", "palmitate", "C16H31O2"),
    ("neg", "oleate", "C18H33O2"), ("neg", "arachidonate", "C20H31O2"),
    ("neg", "docosahexaenoate", "C22H31O2"), ("neg", "hydrogen sulfate", "HO4S"),
    ("neg", "dihydrogen phosphate", "H2O4P"), ("neg", "metaphosphate", "O3P"),
    ("neg", "SO3 radical anion", "O3S"), ("neg", "glucose [M-H]-", "C6H11O6"),
    ("neg", "glycerol phosphate [M-H]-", "C3H8O6P"), ("neg", "ascorbate [M-H]-", "C6H7O6"),
    ("neg", "cyanide", "CN"), ("neg", "hydroxide", "HO"), ("neg", "C2H4NO-", "C2H4NO"),
]
missed = [f"{name} {ion_mz(formula, mode):.4f} {mode}" for mode, name, formula in TEXTBOOK_IONS
          if not is_possible(ion_mz(formula, mode), mode)]
check("keeps all 44 textbook ions at exact mass",
      len(TEXTBOOK_IONS) == 44 and not missed, f"missed: {missed}" if missed else "")

# masses with no legal CHNOPS composition must go. These sit in the mass-defect gap just above
# a nominal mass, where no combination of C/H/N/O/P/S reaches; about 38% of a 80-600 Da grid
# falls in such gaps, which is the headroom the filter works in.
for mz, mode in [(80.1000, "neg"), (80.1000, "pos"), (120.1400, "neg")]:
    check(f"removes an impossible mass {mz} {mode}", not is_possible(mz, mode))

# the filter removes peaks and never reorders or rescales what it keeps
spec = [(59.0138, 100.0), (80.1000, 12.0), (96.9696, 45.0)]
kept = denoise_spectrum(spec, "neg")
check("removes only the impossible peak", kept == [(59.0138, 100.0), (96.9696, 45.0)],
      f"kept {kept}")

# halogens are opt-in. Bromide is the base peak of many brominated compounds in negative mode;
# the default CHNOPS alphabet cannot explain it, the halogen alphabet can.
BR = 78.9189
check("default (CHNOPS) removes bromide", not is_possible(BR, "neg"))
check("halogens=True keeps bromide", is_possible(BR, "neg", halogens=True))
# ...and the two answers must not leak into each other through the memo cache
check("CHNOPS answer unchanged after a halogen call", not is_possible(BR, "neg"))
# the per-peak test is monoisotopic: on its own, 81Br- has no composition even with halogens
check("is_possible rejects 81Br- even with halogens (monoisotopic alphabet)",
      not is_possible(80.9169, "neg", halogens=True))

# a mistyped polarity must raise, not silently score the other mode
for bad in ("negative", "NEG", "positive", ""):
    try:
        is_possible(100.05, bad)
        check(f"rejects mode={bad!r}", False, "accepted silently")
    except ValueError:
        check(f"rejects mode={bad!r}", True)

# the cache is unbounded by design; clear_cache must actually empty it
denoise_spectrum([(60 + i * 0.13, 1.0) for i in range(500)], "neg")
before = cache_size()
clear_cache()
check("clear_cache empties the memo", cache_size() == 0 and before > 0,
      f"{before} entries -> {cache_size()}")

# The table stores each 1 mDa bin's lowest and highest composition mass, so it must agree with
# the solver at every tolerance it serves (2 mDa and up). IMD_NO_TABLE is read at import, so the
# solver runs in a fresh process. The sample is small because the solver is slow.
import impossible_mass_denoise as imd
choline = ion_mz("C5H14NO", "pos")
parity_masses = [
    (59.0138, "neg", False), (60.0808, "pos", False), (80.1000, "neg", False),
    (120.1400, "neg", False), (184.0733, "pos", False), (247.6368, "pos", False),
    (choline + 0.0019, "pos", False), (choline + 0.0021, "pos", False),
    (78.9188, "neg", True), (80.9165, "neg", True),
]
parity_cases = [(mz, mode, hal, tol)
                for mz, mode, hal in parity_masses for tol in (0.002, 0.005, 0.020)]
clear_cache()
table_verdicts = [imd.possible(mz, tol=tol, mode=mode, use_halogens=hal)[0]
                  for mz, mode, hal, tol in parity_cases]
solver_env = dict(os.environ, IMD_NO_TABLE="1")
solver_code = (
    "import json, sys, impossible_mass_denoise as imd; "
    "assert not imd.USE_TABLE; "
    "cases = json.loads(sys.argv[1]); "
    "print(json.dumps([imd.possible(mz, tol=tol, mode=mode, use_halogens=hal)[0] "
    "for mz, mode, hal, tol in cases]))"
)
solver_verdicts = json.loads(subprocess.check_output(
    [sys.executable, "-c", solver_code, json.dumps(parity_cases)],
    env=solver_env, text=True, timeout=300))
disagree = [case for case, t, s in zip(parity_cases, table_verdicts, solver_verdicts) if t != s]
check("table agrees with the solver at 2, 5 and 20 mDa",
      len(solver_verdicts) == len(parity_cases) and not disagree,
      f"{len(parity_cases)} cases" + (f"; disagreements: {disagree}" if disagree else ""))

# throughput. Measure COLD - masses never asked about before - because repeating one spectrum
# five times mostly measures the memo cache and flatters the result by about 8x.
import random
random.seed(7)
uniq = [(round(random.uniform(50, 1000), 4), 1.0) for _ in range(20000)]
clear_cache()
t0 = time.perf_counter()
denoise_spectrum(uniq, "neg")
cold = (time.perf_counter() - t0) / len(uniq) * 1e6
t0 = time.perf_counter()
denoise_spectrum(uniq, "neg")                 # same masses: all cache hits
warm = (time.perf_counter() - t0) / len(uniq) * 1e6
check("cold lookup under 30 us per peak", cold < 30, f"{cold:.1f} us/peak")
clear_cache()
t0 = time.perf_counter()
denoise_spectrum(uniq, "neg", halogens=True)
cold_h = (time.perf_counter() - t0) / len(uniq) * 1e6
check("halogen cold lookup under 30 us per peak", cold_h < 30, f"{cold_h:.1f} us/peak")
check("warm lookup faster than cold", warm < cold, f"{warm:.1f} us/peak warm")

# --- failure classes from the LC-BinBase audit -------------------------------------------------
# Each case uses ions calculated from formulas. The checks pin what the default filter does; the
# expected failures state the behaviour each issue asks for, through the option it will add.

# #5: above the 1,700 Da table ceiling every mass is possible, and it is answered without the
# solver, which takes seconds per mass that high. The answer is exact as long as each table's top
# 16 Da is fully occupied: stepping up by CH2 (14.0157 Da) from a full window, which keeps a
# composition valid, puts a valid composition within 1 mDa of any higher mass.
for name, table in (("CHNOPS", imd.load_table()), ("halogen", imd.load_halogen_table())):
    for mode in ("neg", "pos"):
        top = table[mode][int((table["max_mz"] - 16) / table["bin"]):
                          int(table["max_mz"] / table["bin"])]
        check(f"{name} table full over its top 16 Da ({mode})", bool(top.all()))
clear_cache()
for mz in (1809.584839, 2068.08):
    for mode in ("neg", "pos"):
        for halogens in (False, True):
            t0 = time.perf_counter()
            ok = is_possible(mz, mode, halogens=halogens)
            ms = (time.perf_counter() - t0) * 1e3
            check(f"{mz} {mode}{' halogens' if halogens else ''} above the ceiling: possible, "
                  "under 10 ms", ok and ms < 10, f"{ms:.2f} ms")

# #1: isotope-labelled internal standards. Deuterium is outside the alphabet, so the default
# removes the precursor of a D9-TMAO standard and both of its deuterated trimethylammonium
# fragments, which is the whole spectrum. Told the standard's label count, a deuterium-aware
# filter should keep those ions and still remove a peak that no labelled composition explains.
TMAO_D9 = [(ion_mz("C3D8N", "pos"), 50.0),     # (CD3)2N=CD2+
           (ion_mz("C3D9N", "pos"), 100.0),    # (CD3)3N radical cation
           (ion_mz("C3HD9NO", "pos"), 30.0)]   # [M+H]+
NOISE = (60.1000, 5.0)    # no composition with up to ten deuterium labels, in either polarity
LABELLED_PRECURSORS = [("D9-TMAO [M+H]+", "C3HD9NO", "pos", 9),
                       ("D9-choline", "C5H5D9NO", "pos", 9),
                       ("D9-betaine [M+H]+", "C5H3D9NO2", "pos", 9),
                       ("D10-leucine [M-H]-", "C6H2D10NO2", "neg", 10),
                       ("FA 18:1-d9 [M-H]-", "C18H24D9O2", "neg", 9)]
check("default removes all three D9-TMAO ions", denoise_spectrum(TMAO_D9, "pos") == [])
check("default removes the precursors of the five labelled standards in #1",
      not any(is_possible(ion_mz(f, mode), mode) for _, f, mode, _ in LABELLED_PRECURSORS))
check("deuterium=9 keeps the D9-TMAO ions and still removes noise",
      denoise_spectrum(TMAO_D9 + [NOISE], "pos", deuterium=9) == TMAO_D9)
check("deuterium= keeps the precursors of the five labelled standards",
      all(is_possible(ion_mz(f, mode), mode, deuterium=n) for _, f, mode, n in LABELLED_PRECURSORS))
check("deuterium=10 still removes a noise mass in both polarities",
      not is_possible(NOISE[0], "pos", deuterium=10) and not is_possible(NOISE[0], "neg", deuterium=10))
for bad in (-1, 1.5, True, "9"):
    try:
        denoise_spectrum(TMAO_D9, "pos", deuterium=bad)
        check(f"rejects deuterium={bad!r}", False, "accepted silently")
    except ValueError:
        check(f"rejects deuterium={bad!r}", True)

LABEL_NAMES = {
    "1_D9-Choline iSTD": 9,
    "1_PC 17:0/22:4-d5 iSTD": 5,
    "1_Cer 18:1 d18:1-d7/18:1 iSTD": 7,
    "1_PS 15:0_18:1 d7_ISTD": 7,
    "1_Sphingosine d17:1 iSTD": 0,
    "1_CUDA iSTD": 0,
    "D3-Serine iSTD": 3,
    "13C6 15N2-Serine iSTD": 0,
    "D3-D5-Serine iSTD": 5,
}
for name, expected in LABEL_NAMES.items():
    check(f"counts D labels in {name!r}", count_deuterium(name) == expected)

# #2 and #4: the precursor. Imatinib [M+2H]2+ is both the precursor and the base peak of its
# spectrum. It is doubly charged, so no singly charged composition explains it. Callers know the
# precursor m/z from the library entry; the filter should not have to guess it.
IMATINIB_2H = (ion_mz("C29H33N7O", "pos") - ELECTRON) / 2      # [M+2H]2+ of C29H31N7O
IMATINIB = [(ion_mz("C3H8N", "pos"), 90.0), (IMATINIB_2H, 100.0)]
check("default removes the doubly charged imatinib precursor, the base peak",
      denoise_spectrum(IMATINIB, "pos") == IMATINIB[:1])
check("precursor_mz= keeps the doubly charged precursor",
      denoise_spectrum(IMATINIB, "pos", precursor_mz=IMATINIB_2H) == IMATINIB)
NOISY = IMATINIB + [(80.1000, 40.0)]           # 80.1000 has no composition
# negative mode: ATP [M-2H]2- has no singly charged composition either
ATP_2H = (ion_mz("C10H14N5O13P3", "neg") + ELECTRON) / 2       # [M-2H]2- of C10H16N5O13P3
check("default removes the doubly charged ATP precursor", not is_possible(ATP_2H, "neg"))
check("precursor_mz= keeps a [M-2H]2- precursor",
      denoise_spectrum([(ion_mz("O3P", "neg"), 60.0), (ATP_2H, 100.0)], "neg",
                       precursor_mz=ATP_2H) == [(ion_mz("O3P", "neg"), 60.0), (ATP_2H, 100.0)])
# no per-peak 2+ rule: the default still treats every peak as singly charged
check("is_possible still rejects the doubly charged imatinib ion", not is_possible(IMATINIB_2H, "pos"))
check("precursor_mz= keeps the precursor and nothing else",
      denoise_spectrum(NOISY, "pos", precursor_mz=IMATINIB_2H) == IMATINIB)
check("precursor_mz= matches the precursor peak within 20 mDa",
      denoise_spectrum(NOISY, "pos", precursor_mz=IMATINIB_2H + 0.015) == IMATINIB
      and denoise_spectrum(NOISY, "pos", precursor_mz=IMATINIB_2H + 0.025) == IMATINIB[:1])

# #3: heavy halogen isotopes. The alphabet holds 35Cl and 79Br only, so on its own a 37Cl or 81Br
# peak has no composition. With halogens on, a heavy peak is kept when its light partner is kept,
# and the rule chains: in CCl3- neither heavy peak has a composition of its own, so M+4 is kept
# only through the M+2 kept before it.
CL_SHIFT = 36.965902602 - ATOM["Cl"]          # 37Cl - 35Cl
BR_SHIFT = 80.9162897 - ATOM["Br"]            # 81Br - 79Br
bromide, chloride, ccl3 = ion_mz("Br", "neg"), ion_mz("Cl", "neg"), ion_mz("CCl3", "neg")
HALOGEN_PATTERNS = [
    ("bromide 79Br/81Br", [(bromide, 100.0), (bromide + BR_SHIFT, 97.0)]),
    ("chloride 35Cl/37Cl", [(chloride, 100.0), (chloride + CL_SHIFT, 32.0)]),
    ("CCl3- M, M+2, M+4", [(ccl3, 100.0), (ccl3 + CL_SHIFT, 96.0), (ccl3 + 2 * CL_SHIFT, 31.0)]),
]
for name, pattern in HALOGEN_PATTERNS:
    kept = denoise_spectrum(pattern, "neg", halogens=True)
    check(f"halogens=True keeps every isotope peak of {name}", kept == pattern,
          f"kept {len(kept)} of {len(pattern)}")
    check(f"default removes every peak of {name}", denoise_spectrum(pattern, "neg") == [])
br_pair = HALOGEN_PATTERNS[0][1]
check("isotope partners work on unsorted input and keep the input order",
      denoise_spectrum(br_pair[::-1], "neg", halogens=True) == br_pair[::-1])
check("81Br- without its light partner is still removed with halogens",
      denoise_spectrum([(59.0138, 100.0), br_pair[1]], "neg", halogens=True) == [(59.0138, 100.0)])

print(f"\n{len(fails)} failure(s), {len(xfails)} expected failure(s)" if fails
      else f"\nall checks passed, {len(xfails)} expected failure(s)")
sys.exit(1 if fails else 0)
