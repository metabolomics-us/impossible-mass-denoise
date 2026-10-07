"""
Impossible-mass denoising — the one entry point to integrate.

    from denoise import denoise_spectrum
    kept = denoise_spectrum(peaks, mode="neg")     # peaks = [(mz, intensity), ...]

Everything else in this package supports that call. `impossible_mass_denoise.py` holds the
validated filter and is unmodified from the research tree apart from a package-relative table path
and the removal of a command-line demo that read internal data files.
"""
from __future__ import annotations

import impossible_mass_denoise as _imd

__all__ = ["denoise_spectrum", "is_possible", "fingerprint", "table_status",
           "halogen_table_status", "clear_cache", "cache_size"]

_MODES = ("neg", "pos")
D_SHIFT = 2.01410177812 - 1.00782503223   # mass a deuterium label adds over hydrogen (issue #1)
# A peak within 20 mDa of precursor_mz counts as the precursor (issue #2). Wider than the filter's
# 5 mDa because a stored precursor m/z and the measured peak can differ by 10 mDa or more in
# uncalibrated spectra.
PRECURSOR_TOL = 0.020


def _check_mode(mode):
    """The underlying module treats any string that is not exactly "neg" as positive, so a typo
    like "negative" or "NEG" would silently score the wrong polarity AND drop off the fast path.
    Fail loudly instead."""
    if mode not in _MODES:
        raise ValueError(f"mode must be one of {_MODES}, got {mode!r}")


def _check_deuterium(deuterium):
    if isinstance(deuterium, bool) or not isinstance(deuterium, int) or deuterium < 0:
        raise ValueError(f"deuterium must be a non-negative int, got {deuterium!r}")


def is_possible(mz: float, mode: str = "neg", halogens: bool = False, *,
                deuterium: int = 0) -> bool:
    """True when some composition in the alphabet could produce this ion m/z in this polarity.

    mode is "neg" or "pos". The alphabet is C/H/N/O/P/S, plus Cl/F/Br/I when halogens=True.
    False means no chemically legal composition exists within tolerance - impossible under THAT
    alphabet, singly charged, monoisotopic. It does not prove the peak is not a real ion.

    deuterium=n is for deuterium-labelled internal standards (issue #1). The peak is also
    accepted when taking off between 1 and n deuterium labels (1.0063 Da each) leaves a possible
    ion, so fragments that kept all, some or none of the labels survive. n is the standard's
    label count, 9 for a D9 standard.
    """
    _check_mode(mode)
    _check_deuterium(deuterium)
    mz = float(mz)
    return any(_imd.possible(mz - k * D_SHIFT, mode=mode, d_max=0, union=True,
                             use_halogens=bool(halogens))[0]
               for k in range(deuterium + 1) if mz - k * D_SHIFT > 0)


def denoise_spectrum(peaks, mode: str = "neg", halogens: bool = False, *, deuterium: int = 0,
                     precursor_mz: float | None = None):
    """Return the peaks that survive the filter, in the order given.

    halogens=False (the default) is the validated configuration. halogens=True keeps fragments
    that need Cl/F/Br/I to explain them - bromide and chloride ions, for example - at the cost of
    rejecting fewer noise peaks overall; see the README before switching it on.

    deuterium=n keeps the deuterated ions of a deuterium-labelled internal standard, as described
    in is_possible (issue #1). Pass the standard's label count from the library entry or its name;
    the filter does not infer it from the peaks. Each label adds a lookup and makes the filter a
    little more permissive, so use it only for that standard's spectra.

    peaks is any iterable of (mz, intensity) pairs; the return is a list of the same pairs.
    Intensity is passed through untouched - this filter removes peaks, it never rescales them.
    An empty result is possible and legitimate: every peak in that spectrum was impossible.

    precursor_mz keeps the precursor whatever its verdict (issue #2): any peak within 20 mDa of
    this m/z. Pass the precursor m/z you know from the library entry or the scan header; the
    filter does not guess it from the peaks. Off by default.
    """
    _check_mode(mode)
    _check_deuterium(deuterium)
    prec = None if precursor_mz is None else float(precursor_mz)
    return [p for p in peaks
            if (prec is not None and abs(p[0] - prec) <= PRECURSOR_TOL)
            or is_possible(p[0], mode, halogens, deuterium=deuterium)]


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
