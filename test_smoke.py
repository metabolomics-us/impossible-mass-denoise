"""Smoke test: run `python test_smoke.py`. Exits non-zero on any failure.

Checks the table loaded, the chemistry fingerprint matches the validated build, obviously-real
masses survive, an obviously-impossible one is removed, and throughput is in the expected range.
"""
import sys
import time

from denoise import denoise_spectrum, is_possible, fingerprint, table_status

EXPECTED_FINGERPRINT = "5aae4ec26694da4d"
fails = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
    if not ok:
        fails.append(name)


print(table_status())
check("chemistry fingerprint", fingerprint() == EXPECTED_FINGERPRINT,
      f"{fingerprint()} (expected {EXPECTED_FINGERPRINT})")
check("precomputed table loaded", "loaded" in table_status())

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

# throughput: this is a table lookup, it should be microseconds per peak
big = [(50 + i * 0.7311, 1.0) for i in range(2000)]
denoise_spectrum(big[:50], "neg")            # warm the cache
t0 = time.perf_counter()
for _ in range(5):
    denoise_spectrum(big, "neg")
us = (time.perf_counter() - t0) / (5 * len(big)) * 1e6
check("throughput under 50 us per peak", us < 50, f"{us:.1f} us/peak")

print(f"\n{len(fails)} failure(s)" if fails else "\nall checks passed")
sys.exit(1 if fails else 0)
