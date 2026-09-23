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


def _check_mode(mode):
    """The underlying module treats any string that is not exactly "neg" as positive, so a typo
    like "negative" or "NEG" would silently score the wrong polarity AND drop off the fast path.
    Fail loudly instead."""
    if mode not in _MODES:
        raise ValueError(f"mode must be one of {_MODES}, got {mode!r}")


def is_possible(mz: float, mode: str = "neg", halogens: bool = False) -> bool:
    """True when some composition in the alphabet could produce this ion m/z in this polarity.

    mode is "neg" or "pos". The alphabet is C/H/N/O/P/S, plus Cl/F/Br/I when halogens=True.
    False means no chemically legal composition exists within tolerance - impossible under THAT
    alphabet, singly charged, monoisotopic. It does not prove the peak is not a real ion.
    """
    _check_mode(mode)
    return bool(_imd.possible(float(mz), mode=mode, d_max=0, union=True,
                              use_halogens=bool(halogens))[0])


def denoise_spectrum(peaks, mode: str = "neg", halogens: bool = False):
    """Return the peaks that survive the filter, in the order given.

    halogens=False (the default) is the validated configuration. halogens=True keeps fragments
    that need Cl/F/Br/I to explain them - bromide and chloride ions, for example - at the cost of
    rejecting fewer noise peaks overall; see the README before switching it on.

    peaks is any iterable of (mz, intensity) pairs; the return is a list of the same pairs.
    Intensity is passed through untouched - this filter removes peaks, it never rescales them.
    An empty result is possible and legitimate: every peak in that spectrum was impossible.
    """
    _check_mode(mode)
    return [p for p in peaks if is_possible(p[0], mode, halogens)]


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
