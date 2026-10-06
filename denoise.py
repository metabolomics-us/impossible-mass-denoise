"""
Impossible-mass denoising — the one entry point to integrate.

    from denoise import denoise_spectrum
    kept = denoise_spectrum(peaks, mode="neg")     # peaks = [(mz, intensity), ...]

Everything else in this package supports that call. `impossible_mass_denoise.py` holds the
validated filter and is unmodified from the research tree apart from a package-relative table path
and the removal of a command-line demo that read internal data files.
"""
from __future__ import annotations

import bisect

import impossible_mass_denoise as _imd

__all__ = ["denoise_spectrum", "is_possible", "fingerprint", "table_status",
           "halogen_table_status", "clear_cache", "cache_size"]

_MODES = ("neg", "pos")
FILTER_TOL = 0.005         # the tolerance the tables were built and validated for
PROTECT_TOL = FILTER_TOL   # protected m/z are matched within the same 5 mDa window
# mass gained per label, heavy minus light isotope (issue #1)
LABEL_SHIFT = {"D": 2.01410178 - 1.00782503,
               "13C": 13.00335484 - 12.0,
               "15N": 15.00010890 - 14.00307401}
# 37Cl - 35Cl and 81Br - 79Br (issue #3)
HEAVY_HALOGEN_SPACING = (36.96590260 - 34.96885268, 80.91628970 - 78.91833710)


def _check_mode(mode):
    """The underlying module treats any string that is not exactly "neg" as positive, so a typo
    like "negative" or "NEG" would silently score the wrong polarity AND drop off the fast path.
    Fail loudly instead."""
    if mode not in _MODES:
        raise ValueError(f"mode must be one of {_MODES}, got {mode!r}")


def _label_shifts(labels):
    """Every total mass shift from no label up to the given counts: {"D": 9} gives 10 shifts."""
    if labels is None:
        return [0.0]
    if not hasattr(labels, "items"):
        raise ValueError(f"labels must be a dict such as {{'D': 9}}, got {labels!r}")
    shifts = [0.0]
    for name, n in labels.items():
        if name not in LABEL_SHIFT:
            raise ValueError(f"labels keys must be among {tuple(LABEL_SHIFT)}, got {name!r}")
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise ValueError(f"label count for {name} must be a non-negative int, got {n!r}")
        shifts = [s + j * LABEL_SHIFT[name] for s in shifts for j in range(n + 1)]
    return shifts


def is_possible(mz: float, mode: str = "neg", halogens: bool = False, *,
                allow_z2: bool = False, labels=None) -> bool:
    """True when some composition in the alphabet could produce this ion m/z in this polarity.

    mode is "neg" or "pos". The alphabet is C/H/N/O/P/S, plus Cl/F/Br/I when halogens=True.
    False means no chemically legal composition exists within tolerance - impossible under THAT
    alphabet, singly charged, monoisotopic. It does not prove the peak is not a real ion.

    Opt-in widenings, both off by default:
      allow_z2  also accept the peak as a doubly charged ion (issue #4): its singly charged
                equivalent, 2*m/z - proton in positive mode or + proton in negative, is tested
                at twice the tolerance (the mapping of impossible_mass_denoise.possible_z2).
                Costly: above about 276 m/z (273 in positive mode) that equivalent lands where
                every mass is possible at 10 mDa, so nothing there can be removed any more.
      labels    maximum label counts of an isotope-labelled standard, e.g. {"D": 9}; keys "D",
                "13C", "15N" (issue #1). The peak is accepted if taking off up to that many label
                mass shifts gives a possible ion. Each label combination is one more lookup.
    """
    _check_mode(mode)
    mz = float(mz)
    kw = dict(mode=mode, d_max=0, union=True, use_halogens=bool(halogens))
    shifts = _label_shifts(labels)
    if any(_imd.possible(mz - s, tol=FILTER_TOL, **kw)[0] for s in shifts if mz - s > 0):
        return True
    if allow_z2:
        # Only reached for peaks the singly charged test rejects, all below 578 m/z, so the
        # mapped mass stays under about 1,160 Da, inside the table.
        z1 = 2.0 * mz - _imd.M_P if mode == "pos" else 2.0 * mz + _imd.M_P
        return any(_imd.possible(z1 - s, tol=2 * FILTER_TOL, **kw)[0] for s in shifts if z1 - s > 0)
    return False


def _near(sorted_mz, target):
    """Is any value of the sorted list within PROTECT_TOL of target?"""
    i = bisect.bisect_left(sorted_mz, target - PROTECT_TOL)
    return i < len(sorted_mz) and sorted_mz[i] <= target + PROTECT_TOL


def denoise_spectrum(peaks, mode: str = "neg", halogens: bool = False, *,
                     precursor_mz: float | None = None, keep_top: int = 0, keep_mz=(),
                     allow_z2: bool = False, labels=None):
    """Return the peaks that survive the filter, in the order given.

    halogens=False (the default) is the validated configuration. halogens=True keeps fragments
    that need Cl/F/Br/I to explain them - bromide and chloride ions, for example - at the cost of
    rejecting fewer noise peaks overall; see the README before switching it on. With halogens on,
    a 37Cl or 81Br isotope peak is also kept when its light partner, 1.997-1.998 Da below, is in
    the spectrum and kept (issue #3); an isolated heavy-isotope peak is still removed.

    allow_z2 and labels widen the per-peak test exactly as in is_possible (issues #4 and #1).

    peaks is any iterable of (mz, intensity) pairs; the return is a list of the same pairs.
    Intensity is passed through untouched - this filter removes peaks, it never rescales them.
    An empty result is possible and legitimate: every peak in that spectrum was impossible.

    Protection, all off by default (issue #2). A protected peak is kept whatever its verdict:
      precursor_mz  any peak within 5 mDa of this m/z.
      keep_top      the N most intense peaks; 1 keeps the base peak. Peaks tied at the cut-off
                    are all kept, so a base peak is never dropped.
      keep_mz       any peak within 5 mDa of one of these m/z values.
    The filter can remove a real precursor or base peak when its chemistry is outside the
    alphabet (deuterium labels, alkali adducts, double charge). Use precursor_mz=<precursor> and
    keep_top=1 for spectra that feed internal-standard matching or retention-time correction.
    The cost: an impossible base peak that really is an artifact is kept too.
    """
    _check_mode(mode)
    if isinstance(keep_top, bool) or not isinstance(keep_top, int) or keep_top < 0:
        raise ValueError(f"keep_top must be a non-negative int, got {keep_top!r}")
    _label_shifts(labels)                      # validate before touching any peak
    peaks = list(peaks)
    targets = [float(m) for m in keep_mz]
    if precursor_mz is not None:
        targets.append(float(precursor_mz))
    cutoff = None
    if keep_top and peaks:
        cutoff = sorted((p[1] for p in peaks), reverse=True)[min(keep_top, len(peaks)) - 1]

    def protected(p):
        return ((cutoff is not None and p[1] >= cutoff)
                or any(abs(p[0] - t) <= PROTECT_TOL for t in targets))

    keep = [protected(p) or is_possible(p[0], mode, halogens, allow_z2=allow_z2, labels=labels)
            for p in peaks]
    if halogens:
        # Walk up in m/z so a heavy-isotope peak can lean on a partner kept just before it, and a
        # chain such as two chlorines (M, M+2, M+4) carries through.
        kept_mz = []
        for i in sorted(range(len(peaks)), key=lambda j: peaks[j][0]):
            mz = peaks[i][0]
            if not keep[i] and any(_near(kept_mz, mz - d) for d in HEAVY_HALOGEN_SPACING):
                keep[i] = True
            if keep[i]:
                bisect.insort(kept_mz, mz)
    return [p for p, k in zip(peaks, keep) if k]


def fingerprint() -> str:
    """Hash of the chemistry (masses, valences, caps). Log it: two runs that disagree on this
    are not running the same filter."""
    return _imd.chemistry_fingerprint()


def table_status() -> str:
    """Whether the precomputed CHNOPS table loaded, and what it covers."""
    return _imd.table_status()


def halogen_table_status() -> str:
    """Whether the precomputed halogen-aware table loaded, and what it covers."""
    return _imd.halogen_table_status()


def cache_size() -> int:
    """Number of memoised verdicts held in this process."""
    return len(_imd._CACHE) + len(_imd._TCACHE)


def clear_cache() -> None:
    """Drop the memoised verdicts.

    The filter caches every (m/z, polarity) it has been asked about, which makes repeated masses
    free but grows without bound in a long-lived worker: roughly 26 MB of resident memory per
    100,000 distinct masses. Call this periodically in a persistent service - it costs only the
    re-lookups, which are microseconds.
    """
    _imd._CACHE.clear()
    _imd._TCACHE.clear()
