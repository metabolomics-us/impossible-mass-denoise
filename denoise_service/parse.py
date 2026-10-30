"""Parse pasted MS/MS peak text into (m/z, intensity) pairs.

The service accepts what people actually paste: the standard `mz:intensity` form, two-column
lines, and whole MSP records (header lines included). The parser is strict about numbers because
a silently misread peak would change the denoising result without anyone noticing, and every
error names the 1-based line and the offending token so the caller can fix the input.

Spectrum contents are never logged here; errors quote only the single bad token.
"""
import math
import re
from typing import List, Tuple

__all__ = ["parse_spectrum", "SpectrumParseError"]

# A decimal or scientific number, or nan/inf so those are reported as non-finite rather than as
# "not a number" (float() would accept them, which is why they need an explicit check).
_NUMBER = re.compile(r"^[+-]?(?:(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?|nan|inf|infinity)$",
                     re.IGNORECASE)
_SEPARATORS = re.compile(r"[\s;,]+")
# An MSP header such as "Name: X" or "Num Peaks: 5": starts with a letter and contains a colon.
# A leading nan:/inf: is a (bad) peak, not a header, so it must reach the number checks.
_NONFINITE_PEAK = re.compile(r"^[+-]?(?:nan|inf|infinity):", re.IGNORECASE)


class SpectrumParseError(ValueError):
    """Raised for unparseable spectrum text; the message names the line and the bad token."""


def _is_header(line: str) -> bool:
    return line[0].isalpha() and ":" in line and not _NONFINITE_PEAK.match(line)


def _number(token: str, line_no: int) -> float:
    if not _NUMBER.match(token):
        raise SpectrumParseError("line %d: %r is not a number" % (line_no, token))
    return float(token)


def _peak(mz: float, intensity: float, token: str, line_no: int) -> Tuple[float, float]:
    if not (math.isfinite(mz) and math.isfinite(intensity)):
        raise SpectrumParseError("line %d: non-finite value in %r" % (line_no, token))
    if mz <= 0:
        raise SpectrumParseError("line %d: m/z must be greater than zero in %r" % (line_no, token))
    if intensity < 0:
        raise SpectrumParseError("line %d: intensity must not be negative in %r"
                                 % (line_no, token))
    return (mz, intensity)


def parse_spectrum(text: str) -> List[Tuple[float, float]]:
    """Return the peaks in `text` as (mz, intensity) tuples, in input order."""
    peaks = []  # type: List[Tuple[float, float]]
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or _is_header(line):
            continue
        loose = []  # plain numbers on this line, to be paired in order
        for token in _SEPARATORS.split(line):
            if not token:
                continue
            if ":" in token:
                parts = token.split(":")
                if len(parts) != 2:
                    raise SpectrumParseError("line %d: %r is not an mz:intensity pair"
                                             % (line_no, token))
                peaks.append(_peak(_number(parts[0], line_no), _number(parts[1], line_no),
                                   token, line_no))
            else:
                loose.append((_number(token, line_no), token))
        if len(loose) % 2:
            raise SpectrumParseError("line %d: odd number of values; last value %r has no partner"
                                     % (line_no, loose[-1][1]))
        for i in range(0, len(loose), 2):
            (mz, t1), (inten, t2) = loose[i], loose[i + 1]
            peaks.append(_peak(mz, inten, "%s %s" % (t1, t2), line_no))
    if not peaks:
        raise SpectrumParseError("no peaks found")
    return peaks
