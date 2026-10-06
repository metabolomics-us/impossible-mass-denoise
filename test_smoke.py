"""Smoke test: run `python test_smoke.py`. Exits non-zero on any failure.

Checks the table loaded, the chemistry fingerprint matches the validated build, obviously-real
masses survive, an obviously-impossible one is removed, the opt-in options (protection, isotope
labels, double charge, halogen isotopes) do what they say, and throughput is in the expected range.
"""
import sys
import time

from denoise import (denoise_spectrum, is_possible, fingerprint, table_status,
                     halogen_table_status, cache_size, clear_cache)

EXPECTED_FINGERPRINT = "5aae4ec26694da4d"   # same chemistry for both tables
fails = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
    if not ok:
        fails.append(name)


print(table_status())
check("chemistry fingerprint", fingerprint() == EXPECTED_FINGERPRINT,
      f"{fingerprint()} (expected {EXPECTED_FINGERPRINT})")
check("precomputed table loaded", "loaded" in table_status())
check("halogen table loaded", "loaded" in halogen_table_status())

# real fragments must survive
for mz, mode in [(59.0138, "neg"), (96.9696, "neg"), (145.0506, "neg"),
                 (60.0808, "pos"), (104.1070, "pos"), (184.0733, "pos")]:
    check(f"keeps a real fragment {mz} {mode}", is_possible(mz, mode))

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

# protection (issue #2) is off by default; when asked for, the precursor and the top peaks are
# kept whatever their verdict. D9-TMAO [M+H]+ at 85.1322 and its fragment C3D8N+ at 66.1153 are
# real ions that need deuterium, which is outside the alphabet.
TMAO = [(58.0651, 30.0), (66.1153, 20.0), (85.1322, 100.0)]
check("protection off by default: impossible precursor removed",
      denoise_spectrum(TMAO, "pos") == [(58.0651, 30.0)])
check("precursor_mz keeps the precursor and nothing else",
      denoise_spectrum(TMAO, "pos", precursor_mz=85.1322) == [(58.0651, 30.0), (85.1322, 100.0)])
check("keep_top=1 keeps an impossible base peak",
      denoise_spectrum([(80.1000, 100.0), (60.0808, 50.0)], "pos", keep_top=1)
      == [(80.1000, 100.0), (60.0808, 50.0)])
check("keep_top=1 keeps every peak tied for the base peak",
      denoise_spectrum([(80.1000, 100.0), (85.1322, 100.0), (60.0808, 50.0)], "pos", keep_top=1)
      == [(80.1000, 100.0), (85.1322, 100.0), (60.0808, 50.0)])
check("keep_mz keeps a listed mass",
      denoise_spectrum([(59.0138, 100.0), (120.1400, 12.0)], "neg", keep_mz=[120.14])
      == [(59.0138, 100.0), (120.1400, 12.0)])
for bad in (-1, 1.5, True):
    try:
        denoise_spectrum(TMAO, "pos", keep_top=bad)
        check(f"rejects keep_top={bad!r}", False, "accepted silently")
    except ValueError:
        check(f"rejects keep_top={bad!r}", True)

# isotope labels (issue #1) are opt-in. labels= gives the standard's label counts, and a peak is
# kept if taking off up to that many label shifts leaves a possible ion. The precursors of the five
# standards named in the issue, plus the deuterated trimethylammonium fragments of D9-TMAO.
LABELLED = [(85.1322, "pos", {"D": 9}),     # D9-TMAO [M+H]+
            (66.1153, "pos", {"D": 9}),     # C3D8N+, fragment of D9-TMAO and D9-betaine
            (68.1294, "pos", {"D": 9}),     # C3D9N radical cation, fragment of D9-TMAO
            (113.1635, "pos", {"D": 9}),    # D9-choline M+
            (127.1427, "pos", {"D": 9}),    # D9-betaine [M+H]+
            (140.1501, "neg", {"D": 10}),   # D10-leucine [M-H]-
            (290.3051, "neg", {"D": 9})]    # FA 18:1-d9 [M-H]-
for mz, mode, lab in LABELLED:
    d, k = is_possible(mz, mode), is_possible(mz, mode, labels=lab)
    check(f"labels={lab} keeps labelled ion {mz} {mode}", k and not d, f"default {d}, labels {k}")
TMAO_D9 = [(66.1153, 40.0), (68.1294, 30.0), (85.1322, 100.0)]
check("default empties the D9-TMAO spectrum", denoise_spectrum(TMAO_D9, "pos") == [])
check("labels={'D': 9} keeps the whole D9-TMAO spectrum",
      denoise_spectrum(TMAO_D9, "pos", labels={"D": 9}) == TMAO_D9)
check("labels still remove a noise mass", not is_possible(60.1, "pos", labels={"D": 9})
      and not is_possible(60.1, "neg", labels={"D": 10}))
for bad in ({"2H": 9}, {"D": -1}, {"D": 1.5}, {"D": True}, 9, "D"):
    try:
        is_possible(100.05, "pos", labels=bad)
        check(f"rejects labels={bad!r}", False, "accepted silently")
    except ValueError:
        check(f"rejects labels={bad!r}", True)

# doubly charged ions (issue #4) are opt-in. Imatinib [M+2H]2+ and ATP [M-2H]2- have no singly
# charged composition at their m/z; allow_z2 tests the singly charged ion of the same molecule.
for mz, mode in [(247.6367, "pos"), (252.4906, "neg")]:
    d, z = is_possible(mz, mode), is_possible(mz, mode, allow_z2=True)
    check(f"allow_z2 keeps doubly charged ion {mz} {mode}", z and not d, f"default {d}, allow_z2 {z}")
check("allow_z2 still removes a noise mass", not is_possible(60.1, "pos", allow_z2=True)
      and not is_possible(60.1, "neg", allow_z2=True))

# halogens are opt-in. Bromide is the base peak of many brominated compounds in negative mode;
# the default CHNOPS alphabet cannot explain it, the halogen alphabet can.
BR = 78.9189
check("default (CHNOPS) removes bromide", not is_possible(BR, "neg"))
check("halogens=True keeps bromide", is_possible(BR, "neg", halogens=True))
# ...and the two answers must not leak into each other through the memo cache
check("CHNOPS answer unchanged after a halogen call", not is_possible(BR, "neg"))
# the per-peak test stays monoisotopic: on its own, 81Br- has no composition even with halogens
check("is_possible still rejects 81Br- with halogens (monoisotopic alphabet)",
      not is_possible(80.9169, "neg", halogens=True))
# heavy halogen isotopes (issue #3): with halogens on, denoise_spectrum keeps a 37Cl or 81Br peak
# whose light partner it keeps. The rule chains: in CCl3- (trichloroacetate minus CO2) neither
# heavy peak has a composition of its own, so M+4 is kept only through the M+2 kept before it.
# The default removes them all, and a heavy-isotope peak without a kept partner is still removed.
for name, spec in [("bromide pair (issue #3 masses)", [(78.9188, 97.0), (80.9165, 100.0)]),
                   ("chloride pair", [(34.9694, 100.0), (36.9665, 32.0)]),
                   ("CCl3- isotope chain", [(116.9071, 100.0), (118.9042, 96.0), (120.9012, 31.0)])]:
    check(f"halogens=True keeps the {name}", denoise_spectrum(spec, "neg", halogens=True) == spec)
    check(f"default removes the {name}", denoise_spectrum(spec, "neg") == [])
check("partner rule works on unsorted input and keeps input order",
      denoise_spectrum([(80.9165, 100.0), (78.9188, 97.0)], "neg", halogens=True)
      == [(80.9165, 100.0), (78.9188, 97.0)])
check("81Br- without its partner is still removed with halogens",
      denoise_spectrum([(59.0138, 100.0), (80.9165, 50.0)], "neg", halogens=True)
      == [(59.0138, 100.0)])

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
clear_cache()
t0 = time.perf_counter()
denoise_spectrum(uniq, "neg", labels={"D": 9}, allow_z2=True)    # up to 20 lookups per peak
cold_o = (time.perf_counter() - t0) / len(uniq) * 1e6
check("labels + allow_z2 cold lookup under 60 us per peak", cold_o < 60, f"{cold_o:.1f} us/peak")
check("warm lookup faster than cold", warm < cold, f"{warm:.1f} us/peak warm")

print(f"\n{len(fails)} failure(s)" if fails else "\nall checks passed")
sys.exit(1 if fails else 0)
