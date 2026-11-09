"""Why an ion was removed, and which validity model kept the ones that stayed. Python 3.9 compatible.

`denoise.is_possible` is a boolean: it answers "does some legal composition produce this mass?" and
says nothing about why the answer was no. That is the one thing a curator has to open a terminal
for, and it is what this module supplies - using only functions the package already exports, so an
explanation can never disagree with the verdict it explains.

Every reason is a stable code with a documented meaning (`REASON_MEANING`, also served as the
`denoise://reason-codes` resource and copied to `reason-codes.json`). Narration is built from the
codebook, never improvised per ion: when nothing can be computed, the answer says so instead of
inventing a cause.

What the codes mean, read off this codebase:

* `no-legal-ion-nearby`       no allowed mass lies within `NEAREST_WINDOW_MDA` of the ion at all.
* `legal-within-NmDa`         an allowed mass exists within N mDa - the ion sits in the gap that
                              makes the model discriminating rather than broken. The tightest band
                              only; within 30 mDa already implies within 100 mDa.
* `needs-halogen-alphabet`    allowed only if Cl/F/Br/I are in the alphabet (halogens=False refuses).
* `possible-as-doubly-charged` explainable as z=2; the deployed model is singly charged.
* `consistent-with-label-X`   sits k x X above an allowed mass: what a 2H/13C/15N-labelled ion, or
                              an M+1 isotope peak, looks like. Only meaningful when the sample
                              carries that label - a natural-abundance 13C peak is not the analyte.
* `nearest-legal-{above,below}` signed distance to the closest allowed mass, always given when one exists.

Above `TABLE_MAX_MZ` the tables are silent in the other direction: `denoise.is_possible` answers
*possible* there (a valid composition stepped up by CH2 reaches any higher mass), so no removal ever
needs a ceiling reason. `TABLE_MAX_MZ` is still reported, because "possible above 1700 Da" is the
model's statement, not a measurement.

Costs, measured cold on the public fixture (unique masses): a verdict 21 us, a halogen lookup 21 us,
a z=2 lookup 17 us, a label lookup 20 us, the nearest-mass grid scan under 1 us, a formula witness
7.9 ms, and a single validity model (which the precomputed table cannot answer) 3.9 ms. That is why
formulas and model flags are opt-in and capped per request.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

import denoise
import impossible_mass_denoise as imd

from .limits import Limits

# How far to look for the closest allowed mass. The widest single thing the model can place is well
# under this, so "no legal ion nearby" means an genuinely empty neighbourhood, not "looked too short".
NEAREST_WINDOW_MDA = 400.0

# Distance bands, narrowest first; only the tightest one that holds the ion is reported.
NEAREST_BUCKETS_MDA = (5.0, 10.0, 30.0, 100.0, 300.0)

# Label steps we can name, taken from the filter's own shift constants: a labelled peak sits an
# exact whole number of these above the unlabelled mass.
LABEL_STEPS: Tuple[Tuple[str, float], ...] = (
    ("deuterium", denoise.D_SHIFT),
    ("carbon13", denoise.C13_SHIFT),
    ("nitrogen15", denoise.N15_SHIFT),
)

# How many of one label to test when naming the count in a sentence; beyond 4 it is a stretch.
LABEL_COUNT_MAX = 6

REASON_MEANING: Dict[str, str] = {
    "no-legal-ion-nearby":
        "no mass allowed by the selected model lies within %g mDa of this ion" % NEAREST_WINDOW_MDA,
    "needs-halogen-alphabet":
        "possible only if the halogen alphabet (Cl, F, Br, I) is allowed; the default model excludes it",
    "possible-as-doubly-charged":
        "explainable as a doubly charged ion (z=2); the deployed model assumes z=1",
    "nearest-legal-above":
        "m/z of the closest allowed mass, which lies above this ion",
    "nearest-legal-below":
        "m/z of the closest allowed mass, which lies below this ion",
}
for _step, _delta in LABEL_STEPS:
    REASON_MEANING["consistent-with-label-%s" % _step] = (
        "sits above an allowed mass by a whole number of %s labels (%.4f Da each), which is what a "
        "%s-labelled ion or its M+1 isotope peak looks like; declare the standard's label count to "
        "have the filter accept it" % (_step, abs(_delta), _step))
for _bucket in NEAREST_BUCKETS_MDA:
    REASON_MEANING["legal-within-%gmDa" % _bucket] = (
        "an allowed mass exists within %g mDa, so this ion falls in a gap that makes the model "
        "discriminating rather than broken" % _bucket)

REASON_CODES: Tuple[str, ...] = tuple(sorted(REASON_MEANING))

# Reasons that state chemistry. The distance codes are context, not a diagnosis.
STRONG_CODES: Tuple[str, ...] = tuple(
    c for c in REASON_CODES
    if c not in ("nearest-legal-above", "nearest-legal-below", "no-legal-ion-nearby"))

# Which validity model accepted a possible ion. The published default is the union of both.
KEPT_MODELS = ("both-models", "lenient-only", "strict-only")

# Work estimate per explanation step, microseconds, measured cold (see module docstring). Used to
# refuse a request whose explanations would take minutes before any of it is computed.
WORK_US = {"nearest": 1, "halogen": 21, "z2": 17, "label": 20, "label_count": 20, "verdict": 21}


# One validator for mode strings, shared with every other check on this surface.
_MODES = Limits()


# The tolerance the deployed filter asks the table with: `denoise.is_possible` calls
# `imd.possible` without overriding its default. Verdicts and distances must use the same number.
DEPLOYED_TOL = 0.005


def table_max_mz(halogens: bool = False) -> float:
    """The mass the precomputed tables stop at; above it the model answers possible, not unknown."""
    table = imd.load_halogen_table() if halogens else imd.load_table()
    if table is None:
        raise ValueError("the %s table is unavailable: %s"
                         % ("halogen" if halogens else "CHNOPS",
                            imd.halogen_table_status() if halogens else imd.table_status()))
    return float(table["max_mz"])


# The nearest-mass answer is read off the precomputed grid, so this surface needs that table; if it
# cannot load, say so at import instead of serving a surface that quietly falls back to the solver
# for every question (denoise itself degrades to the solver, and that is right for a library).
_TABLE = imd.load_table()
if _TABLE is None:
    raise RuntimeError("denoise_mcp needs the precomputed possible-mass table: %s"
                       % imd.table_status())
TABLE_MAX_MZ = float(_TABLE["max_mz"])

# The precomputed grid: one bin per mDa, each bin holding whether a legal composition falls in it
# plus that composition's lowest and highest mass (uint16 offsets at 1.5e-8 Da), which is what makes
# the nearest-mass question answerable exactly rather than to the nearest bin.
_GRID_KEYS = {"pos": ("pos", "lo_pos", "hi_pos"), "neg": ("neg", "lo_neg", "hi_neg")}


def _grid(mode: str, halogens: bool):
    _MODES.mode(mode)
    table = imd.load_halogen_table() if halogens else imd.load_table()
    if table is None:
        raise ValueError("the %s table is not available: %s"
                         % ("halogen" if halogens else "CHNOPS",
                            imd.halogen_table_status() if halogens else imd.table_status()))
    return table


def nearest_legal(mz: float, mode: str, halogens: bool = False,
                  window_mda: float = NEAREST_WINDOW_MDA) -> Optional[float]:
    """Closest mass the model allows near `mz`; None when the neighbourhood is empty.

    Exact, not bin-rounded: a set bin carries the lowest and highest composition mass inside it, and
    the closer endpoint is returned, so the answer is a real composition mass rather than a bin
    centre. Above the table ceiling the model answers possible for every mass, so the nearest allowed
    mass is the ion itself.
    """
    table = _grid(mode, halogens)
    mz = float(mz)
    if mz <= 0.0:
        return None
    ceiling = float(table["max_mz"])
    if mz > ceiling - DEPLOYED_TOL:
        return mz
    grid_key, lo_key, hi_key = _GRID_KEYS[mode]
    grid, lo, hi = table[grid_key], table[lo_key], table[hi_key]
    bin_size = float(table["bin"])                      # 1 mDa, the same grid the filter reads
    step = bin_size / 65535.0                            # what one uint16 offset is worth in Da
    centre = mz / bin_size
    low = max(0, int(centre - window_mda / 1000.0 / bin_size))
    high = min(grid.size - 1, int(centre + window_mda / 1000.0 / bin_size) + 1)
    if high < low:
        return None
    hits = np.flatnonzero(grid[low:high + 1]) + low
    if not hits.size:
        return None
    starts = hits.astype(np.float64) * bin_size
    candidates = np.concatenate([starts + lo[hits].astype(np.float64) * step,
                                 starts + hi[hits].astype(np.float64) * step])
    return float(candidates[int(np.argmin(np.abs(candidates - mz)))])


def distance_to_nearest(mz: float, mode: str, halogens: bool = False,
                        window_mda: float = NEAREST_WINDOW_MDA
                        ) -> Tuple[Optional[float], Optional[float]]:
    """(nearest allowed m/z, signed offset in mDa); positive means the allowed mass is above the ion."""
    near = nearest_legal(mz, mode, halogens, window_mda)
    if near is None:
        return None, None
    return near, (near - float(mz)) * 1000.0


def _label_hits(mz: float, mode: str, halogens: bool) -> List[str]:
    """Which heavy-isotope labels would put an allowed mass an exact whole number below the peak."""
    hits = []
    for name, _delta in LABEL_STEPS:
        try:
            if denoise.is_possible(mz, mode, halogens, **{name: 1}):
                hits.append("consistent-with-label-%s" % name)
        except ValueError:
            pass
    return hits


def label_count(mz: float, mode: str, name: str, halogens: bool = False) -> int:
    """How many of that label the peak sits above an allowed mass (0 when none fits)."""
    for k in range(1, LABEL_COUNT_MAX + 1):
        try:
            if denoise.is_possible(float(mz), mode, halogens, **{name: k}):
                return k
        except ValueError:
            return 0
    return 0


def removal_reasons(mz: float, mode: str, *, halogens: bool = False,
                    nearest_only: bool = False, window_mda: float = NEAREST_WINDOW_MDA
                    ) -> List[str]:
    """Reasons this ion was removed. An empty list means the ion is possible: nothing to explain.

    `nearest_only` gives just the distance statement - the cheap answer for a big spectrum.
    """
    _MODES.mode(mode)
    mz = float(mz)
    if denoise.is_possible(mz, mode, halogens):
        return []

    near, mda = distance_to_nearest(mz, mode, halogens, window_mda)
    if near is None:
        return ["no-legal-ion-nearby"]
    codes = ["nearest-legal-above" if mda > 0 else "nearest-legal-below"]
    if nearest_only:
        return codes
    for bucket in NEAREST_BUCKETS_MDA:          # sorted narrowest first; tightest band only
        if abs(mda) <= bucket:
            codes.append("legal-within-%gmDa" % bucket)
            break
    if not halogens and denoise.is_possible(mz, mode, True):
        codes.append("needs-halogen-alphabet")
    if imd.possible_z2(mz, mode=mode)[0]:
        codes.append("possible-as-doubly-charged")
    codes += _label_hits(mz, mode, halogens)
    return codes


def model_flag(mz: float, mode: str, halogens: bool = False) -> Optional[str]:
    """Which validity model accepted this ion, or None when neither does.

    "lenient-only" and "strict-only" are the bands where the two published models disagree - the
    peaks a reviewer should look at first, because one model calls them impossible. The precomputed
    table answers the union only, so this call runs the solver: 3.9 ms per ion, 200x a verdict. Ask
    for it deliberately (`model_flags=true`), not by default.
    """
    _MODES.mode(mode)
    lenient = bool(imd.possible(mz, mode=mode, use_halogens=bool(halogens),
                                strict=False, union=False)[0])
    strict = bool(imd.possible(mz, mode=mode, use_halogens=bool(halogens),
                               strict=True, union=False)[0])
    if lenient and strict:
        return "both-models"
    if lenient:
        return "lenient-only"
    if strict:
        return "strict-only"
    return None


def narrate(mz: float, mode: str, codes: Sequence[str], halogens: bool = False) -> str:
    """Sentences for these codes, assembled from the codebook."""
    if not codes:
        return ("no reason recorded: this ion is possible under the selected model, or "
                "explanations were turned off")
    parts = ["m/z %.4f (%s polarity)" % (float(mz), "negative" if mode == "neg" else "positive")]
    for code in codes:
        if code in ("nearest-legal-above", "nearest-legal-below"):
            near, mda = distance_to_nearest(mz, mode, halogens)
            parts.append("the closest allowed mass is %.4f, %s this ion by %.1f mDa"
                         % (near, "above" if mda > 0 else "below", abs(mda)))
        elif code.startswith("consistent-with-label-"):
            name = code[len("consistent-with-label-"):]
            k = label_count(mz, mode, name, halogens) or 1
            delta = abs(dict(LABEL_STEPS)[name])
            parts.append("sits %d x %.4f Da above an allowed mass, the signature of a %s-labelled "
                         "ion (or its M+%d isotope peak)" % (k, delta, name, k))
        elif code == "needs-halogen-alphabet":
            parts.append("it needs a halogen (Cl, F, Br or I) to be explainable, and this request "
                         "did not allow the halogen alphabet")
        elif code == "possible-as-doubly-charged":
            parts.append(REASON_MEANING[code])
        else:
            parts.append(REASON_MEANING[code])
    return "; ".join(parts) + "."


def explanation_cost_us(deuterium: int, carbon13: int, nitrogen15: int,
                        nearest_only: bool = False) -> int:
    """Estimated microseconds to explain one removed peak under these options.

    Estimates exist so a request that would take minutes is refused before any of it is computed,
    with the numbers in the message - not so the answer is exact.
    """
    if nearest_only:
        return WORK_US["verdict"] + WORK_US["nearest"]
    named = sum(1 for k in (deuterium, carbon13, nitrogen15) if k)
    return (WORK_US["verdict"] + WORK_US["nearest"] + WORK_US["halogen"] + WORK_US["z2"]
            + named * (WORK_US["label"] + WORK_US["label_count"]))
