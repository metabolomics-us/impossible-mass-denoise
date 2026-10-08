"""Public-data and synthetic controls for the audit follow-up (issue #19)."""

import hashlib
import json
from pathlib import Path
import random

from denoise import denoise_spectrum, is_possible
import impossible_mass_denoise as imd

FIXTURE = Path(__file__).parent / "tests" / "massbank_aces_su_256.json"
EXPECTED_SOURCE = "befc8a1e2f2aef899747797c081a5d80fab12fe7"
EXPECTED_DEFAULT_DIGEST = "6a411806a0b1c6f8fb9bde3f66dd644b778218736ca57cf02ffe3d2277fd60b8"

data = json.loads(FIXTURE.read_text())
assert data["source_commit"] == EXPECTED_SOURCE and data["license"] == "CC BY"
assert len(data["records"]) == 256

# The digest is the result from the existing default implementation, recorded before any new
# option is added. Comparing explicit options-off calls catches a wrapper regression; the pinned
# digest also catches a change that affects both default and explicit-off calls together.
default_outputs = []
for accession, mode, peaks in data["records"]:
    default = denoise_spectrum(peaks, mode)
    explicit_off = denoise_spectrum(peaks, mode, halogens=False, deuterium=0,
                                    precursor_mz=None)
    assert default == explicit_off, accession
    default_outputs.append([accession, default])
digest = hashlib.sha256(json.dumps(default_outputs, separators=(",", ":")).encode()).hexdigest()
assert digest == EXPECTED_DEFAULT_DIGEST, f"default-path digest changed: {digest}"
print("PASS: 256 public MassBank spectra retain the pinned default verdicts")

# Negative control: these public spectra contain no D-labelled names/formulas. Restrict to the
# discriminating mass range and to peaks the default rule removes. A fake D count from 1 to 9
# must rescue only a small share of them; the upper bound is deliberately looser than the 14%
# observed in this fixture, so the test is not tailored to one precise number.
removed_unlabelled = [(mz, mode) for _, mode, peaks in data["records"]
                      for mz, _ in peaks if 50 <= mz <= 250 and not is_possible(mz, mode)]
assert len(removed_unlabelled) >= 200
fake_rates = []
for n in range(1, 10):
    rate = sum(is_possible(mz, mode, deuterium=n)
               for mz, mode in removed_unlabelled) / len(removed_unlabelled)
    fake_rates.append(rate)
assert max(fake_rates) <= 0.25, f"fake D rescue exceeded 25%: {fake_rates}"
print(f"PASS: fake D1-D9 rescue at most {max(fake_rates):.1%} of removed unlabelled peaks")

# Draw new CHNOPS formulas and substitutions with a fixed seed for reproducibility. The formulas
# are generated, not a hard-coded list of masses; only chemically accepted base ions enter the
# test. For each, any number of D substitutions up to the supplied label count must survive.
atom = {"C": 12.0, "H": 1.00782503223, "N": 14.00307400443,
        "O": 15.99491461957, "P": 30.97376199842, "S": 31.9720711744}
d_shift = 2.01410177812 - atom["H"]
electron = 0.000548579909065
rng = random.Random(19019)
labelled = []
for _ in range(3000):
    counts = {"C": rng.randint(1, 8), "H": rng.randint(2, 18),
              "N": rng.randint(0, 2), "O": rng.randint(0, 4),
              "P": rng.randint(0, 1), "S": rng.randint(0, 1)}
    mode = rng.choice(("neg", "pos"))
    base_mz = sum(atom[k] * v for k, v in counts.items()) + (electron if mode == "neg" else -electron)
    if not is_possible(base_mz, mode):
        continue
    k = rng.randint(1, min(counts["H"], 9))
    n = rng.randint(k, 9)
    labelled_mz = base_mz + k * d_shift
    if labelled_mz > 250 or is_possible(labelled_mz, mode):
        continue
    labelled.append((labelled_mz, mode, n))
    if len(labelled) == 120:
        break
assert len(labelled) == 120
for mz, mode, n in labelled:
    assert is_possible(mz, mode, deuterium=n), (mz, mode, n)
    assert denoise_spectrum([(mz, 1.0)], mode, deuterium=n) == [(mz, 1.0)]
print("PASS: 120 randomized CHNOPS ions with D substitutions survive")

# Draw masses in genuine gaps. A 20 mDa feasibility query verifies that no composition lies
# within at least that distance; the deployed 5 mDa filter must remove the same masses.
gap_masses = []
for _ in range(3000):
    mz = rng.uniform(80, 250)
    mode = rng.choice(("neg", "pos"))
    if not imd.possible(mz, tol=0.020, mode=mode)[0]:
        gap_masses.append((mz, mode))
    if len(gap_masses) == 120:
        break
assert len(gap_masses) == 120
for mz, mode in gap_masses:
    assert not is_possible(mz, mode), (mz, mode)
    assert denoise_spectrum([(mz, 1.0)], mode) == [], (mz, mode)
print("PASS: 120 randomized masses at least 20 mDa from a composition are removed")
