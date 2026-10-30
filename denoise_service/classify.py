"""Per-ion possible/impossible verdicts, computed by Ziyue's own filter.

This is the only module in the service that imports `denoise`. Each peak is tagged with its input
index and the whole spectrum goes through `denoise.denoise_spectrum`; an ion is "possible" exactly
when its index survives. Going through the filter, rather than calling `is_possible` per peak,
means precursor protection, label options and halogen isotope chaining match the research code by
construction instead of by re-implementation.

"Impossible" means not explained by CHNOPS (plus halogens if asked), singly charged, monoisotopic.
Spectrum contents are never logged.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import denoise

__all__ = ["Options", "Classification", "classify", "fingerprint"]


@dataclass(frozen=True)
class Options:
    """Filter settings; `mode` must be exactly "neg" or "pos" (ValueError otherwise)."""
    mode: str
    halogens: bool = False
    deuterium: int = 0
    carbon13: int = 0
    nitrogen15: int = 0
    precursor_mz: Optional[float] = None


@dataclass(frozen=True)
class Classification:
    """`possible[i]` is the verdict for input peak i; `summary` holds the aggregate numbers."""
    possible: List[bool]
    summary: Dict[str, float]


def classify(peaks: Sequence[Tuple[float, float]], options: Options) -> Classification:
    # keep_top is deliberately not used: it protects peaks regardless of chemistry, which would
    # hide exactly the impossibility this service reports.
    triples = [(mz, intensity, i) for i, (mz, intensity) in enumerate(peaks)]
    survivors = denoise.denoise_spectrum(
        triples, options.mode, options.halogens, deuterium=options.deuterium,
        precursor_mz=options.precursor_mz, carbon13=options.carbon13,
        nitrogen15=options.nitrogen15)
    kept = {t[2] for t in survivors}
    possible = [i in kept for i in range(len(triples))]
    total = float(sum(p[1] for p in peaks))
    bad = float(sum(p[1] for p, ok in zip(peaks, possible) if not ok))
    summary = {
        "total": len(possible),
        "impossible": len(possible) - sum(possible),
        "intensity_total": total,
        "intensity_impossible": bad,
        "intensity_impossible_fraction": bad / total if total > 0 else 0.0,
    }
    return Classification(possible=possible, summary=summary)


def fingerprint() -> str:
    """Chemistry hash; two deployments that disagree are not running the same filter."""
    return denoise.fingerprint()
