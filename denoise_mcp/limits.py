"""Limits and input checks for the MCP surface. Python 3.9 compatible, no web libraries.

The numbers mirror `denoise_service/config.py` (a test in `tests/test_deployable.py` compares the
two declarations), and mode/label/ceiling errors are raised by Ziyue's own validators in
`denoise.py`, wrapped here so the MCP layer has one exception type and cannot drift from the
filter's real rules. Peak *shape* checks exist only in this layer, because the filter is a library
that trusts its caller and an internet-facing endpoint cannot.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Sequence, Tuple

from denoise import PRECURSOR_TOL


class LimitError(ValueError):
    """Every rejected request. Message first - a caller has to be able to fix it."""


@dataclass(frozen=True)
class Limits:
    """Same defaults as `denoise_service.config.Config`; both are overridable by deployment."""

    max_peaks: int = 5000
    max_batch: int = 200
    max_label: int = 12
    max_label_lookups: int = 200
    max_request_lookups: int = 200000
    # A formula witness costs about 7.9 ms per ion against a 21 us verdict, and a single validity
    # model costs 3.9 ms (the precomputed table answers only their union). Both are opt-in and
    # capped per request.
    max_formula_ions: int = 50
    max_model_ions: int = 50
    # Estimated microseconds of explanation work allowed per request; see explain.explanation_cost_us.
    max_explanation_work_us: int = 1000000
    # A removal entry is ~300 bytes of JSON, so 5000 of them is a 1.5 MB tool result and agent
    # harnesses truncate results over ~20 KB without saying so. The summary always states the true
    # counts; this caps how many removals come back, and the result says when it did.
    max_result_peaks: int = 250

    def peaks(self, peaks: Any) -> List[Tuple[float, float]]:
        """Validate shape, then return the peaks as float pairs.

        Messages match the REST service's wording where the two overlap, so a caller moving
        between surfaces reads the same complaint.
        """
        if not isinstance(peaks, Sequence) or isinstance(peaks, (str, bytes)) or not peaks:
            raise LimitError("peaks must be a non-empty list")
        if len(peaks) > self.max_peaks:
            raise LimitError("too many peaks (%d); maximum %d" % (len(peaks), self.max_peaks))
        out: List[Tuple[float, float]] = []
        for i, peak in enumerate(peaks):
            if (not isinstance(peak, Sequence) or isinstance(peak, (str, bytes))
                    or len(peak) < 2):
                raise LimitError("each peak must be [mz, intensity]")
            try:
                mz, intensity = float(peak[0]), float(peak[1])
            except (TypeError, ValueError):
                raise LimitError("each peak must be [mz, intensity]") from None
            if not math.isfinite(mz) or mz <= 0.0:
                raise LimitError("peak mz must be a finite number > 0 (peak %d)" % i)
            if not math.isfinite(intensity) or intensity < 0.0:
                raise LimitError("peak intensity must be a finite number >= 0 (peak %d)" % i)
            out.append((mz, intensity))
        return out

    def precursor(self, precursor_mz: Any) -> Optional[float]:
        if precursor_mz is None:
            return None
        try:
            prec = float(precursor_mz)
        except (TypeError, ValueError):
            raise LimitError("precursor_mz must be a finite number > 0") from None
        if not math.isfinite(prec) or prec <= 0.0:
            raise LimitError("precursor_mz must be a finite number > 0")
        return prec

    def labels(self, halogens: Any, deuterium: int, carbon13: int, nitrogen15: int) -> None:
        """Delegate to `denoise`, which owns what a legal label is; the cap is its cap too."""
        if not isinstance(halogens, bool):
            raise LimitError("halogens must be true or false")
        for name, value in (("deuterium", deuterium), ("carbon13", carbon13),
                            ("nitrogen15", nitrogen15)):
            if not isinstance(value, int) or isinstance(value, bool):
                raise LimitError("%s must be an integer in [0, %d]" % (name, self.max_label))
            if value > self.max_label:
                raise LimitError("%s must be an integer in [0, %d]" % (name, self.max_label))
            if value > 0 and self.label_lookups(deuterium, carbon13, nitrogen15) > self.max_label_lookups:
                raise LimitError(
                    "the label mix deuterium=%d carbon13=%d nitrogen15=%d needs %d lookups per ion; "
                    "this surface allows %d" % (deuterium, carbon13, nitrogen15,
                                                self.label_lookups(deuterium, carbon13, nitrogen15),
                                                self.max_label_lookups))
        # The filter's own validators are the authority on label values.
        try:
            from denoise import is_possible
            is_possible(1.0, "pos", bool(halogens), deuterium=deuterium, carbon13=carbon13,
                        nitrogen15=nitrogen15)
        except ValueError as exc:
            raise LimitError(str(exc)) from None

    def mode(self, mode: Any) -> str:
        try:
            from denoise import _check_mode
            _check_mode(mode)
        except ValueError as exc:
            raise LimitError(str(exc)) from None
        return mode

    def label_lookups(self, deuterium: int, carbon13: int, nitrogen15: int) -> int:
        """How many extra table lookups one ion costs under these labels (denoise's own rule)."""
        return (deuterium + 1) * (carbon13 + 1) * (nitrogen15 + 1) - 1

    def batch(self, spectra: Any) -> List[Any]:
        if not isinstance(spectra, Sequence) or isinstance(spectra, (str, bytes)) or not spectra:
            raise LimitError("spectra must be a non-empty list")
        if len(spectra) > self.max_batch:
            raise LimitError("too many spectra (%d); maximum %d" % (len(spectra), self.max_batch))
        return list(spectra)
