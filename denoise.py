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

__all__ = ["denoise_spectrum", "is_possible", "fingerprint", "table_status", "clear_cache",
           "cache_size"]

_MODES = ("neg", "pos")


def _check_mode(mode):
    """The underlying module treats any string that is not exactly "neg" as positive, so a typo
    like "negative" or "NEG" would silently score the wrong polarity AND drop off the fast path.
    Fail loudly instead."""
    if mode not in _MODES:
        raise ValueError(f"mode must be one of {_MODES}, got {mode!r}")


def is_possible(mz: float, mode: str = "neg") -> bool:
    """True when some CHNOPS composition could produce this ion m/z in this polarity.

    mode is "neg" or "pos". False means no chemically legal composition exists within tolerance,
    which is the definition of an impossible mass: the peak cannot be a real fragment ion.
    """
    _check_mode(mode)
    return bool(_imd.possible(float(mz), mode=mode, d_max=0, union=True)[0])


def denoise_spectrum(peaks, mode: str = "neg"):
    """Return the peaks that survive the filter, in the order given.

    peaks is any iterable of (mz, intensity) pairs; the return is a list of the same pairs.
    Intensity is passed through untouched - this filter removes peaks, it never rescales them.
    An empty result is possible and legitimate: every peak in that spectrum was impossible.
    """
    _check_mode(mode)
    return [p for p in peaks if is_possible(p[0], mode)]


def fingerprint() -> str:
    """Hash of the chemistry (masses, valences, caps). Log it: two runs that disagree on this
    are not running the same filter."""
    return _imd.chemistry_fingerprint()


def table_status() -> str:
    """Whether the precomputed mass table loaded, and what it covers."""
    return _imd.table_status()


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
