"""A cleaned spectrum as an answer: verdicts, reasons, and a payload that stays small.

Verdicts come from `denoise_service.classify` - the same call the REST service makes - so the two
surfaces cannot disagree about what survived. Everything after the verdict (reasons, validity model,
formula witness, summary) is computed here.

Two shapes matter to a caller: `summary()` is the small answer that always fits, and `payload()` adds
the removed peaks. The clean peak list is opt-in (`include_kept`), because agent harnesses truncate
tool results around 20 KiB and a 5000-peak spectrum is 160 KiB: a truncated payload is worse than a
summary that says how to ask for more.

Works on Python 3.9 with no web or MCP libraries, so the same code runs on the production nodes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import denoise
import impossible_mass_denoise as imd
from denoise_service.classify import Options, classify

from . import explain
from .limits import LimitError, Limits

PeakT = Tuple[float, float]
OnEvent = Callable[[str, Any], None]


@dataclass(frozen=True)
class Peak:
    mz: float
    intensity: float
    formula: Optional[str] = None
    model: Optional[str] = None


@dataclass(frozen=True)
class Removed:
    mz: float
    intensity: float
    reasons: List[str] = field(default_factory=list)
    narrative: str = ""
    nearest_legal_mz: Optional[float] = None
    nearest_legal_mDa: Optional[float] = None


@dataclass(frozen=True)
class Progress:
    """One line of batch progress: which spectrum, out of how many, with running counts."""

    index: int
    total: int
    kept: int
    removed: int


@dataclass
class Result:
    mode: str
    halogens: bool
    labels: Dict[str, int]
    precursor_mz: Optional[float]
    fingerprint: str
    table_max_mz: float
    peaks_in: int
    kept: List[Peak] = field(default_factory=list)
    removed: List[Removed] = field(default_factory=list)
    formulas_computed: int = 0
    formulas_skipped: int = 0
    intensity_in: float = 0.0
    intensity_removed: float = 0.0

    @property
    def removed_above_table(self) -> int:
        """Removed peaks heavier than the table ceiling. Should always be zero.

        Above the ceiling the model answers possible for every mass (a valid composition stepped up
        by CH2 reaches any higher mass), so a removal there means the tables and the solver disagree
        - worth counting, and worth alerting on.
        """
        return sum(1 for r in self.removed if r.mz > self.table_max_mz)


def clean_spectrum(peaks: Any, mode: str, *, halogens: bool = False, deuterium: int = 0,
                   carbon13: int = 0, nitrogen15: int = 0, precursor_mz: Any = None,
                   explanations: bool = True, formulas: bool = False,
                   model_flags: bool = False, nearest_only: bool = False,
                   max_formulas: Optional[int] = None, max_models: Optional[int] = None,
                   limits: Optional[Limits] = None,
                   on_explained: Optional[OnEvent] = None) -> Result:
    """Clean `peaks` and explain what came out. Raises `LimitError` for anything unusable.

    `formulas` asks for a witness composition per kept peak (7.9 ms per ion against a 21 us verdict,
    and uncomputable above the table ceiling); `model_flags` says which validity model kept it
    (3.9 ms per ion - the precomputed table answers only the union of the two models). Both are
    opt-in and capped per request, and both report how many peaks they covered.

    `on_explained(kind, item)` is called once per peak - "kept" with a `Peak`, "removed" with a
    `Removed` - after the verdicts are settled, which is the slow part of a large request. Pass it to
    stream the answer while the work finishes; it may be called from a worker thread.
    """
    lim = limits or Limits()
    pairs = lim.peaks(peaks)
    m = lim.mode(mode)
    lim.labels(halogens, deuterium, carbon13, nitrogen15)
    prec = lim.precursor(precursor_mz)

    if explanations and not nearest_only:
        work = explain.explanation_cost_us(deuterium, carbon13, nitrogen15) * len(pairs)
        if work > lim.max_explanation_work_us:
            raise LimitError("explaining %d peaks under these options costs an estimated %d us, over "
                             "the %d us allowed; ask for nearest_only=true or explanations=false, or "
                             "split the request" % (len(pairs), work, lim.max_explanation_work_us))

    # One call, one code path, one set of verdicts - the REST service's own.
    verdicts = classify(pairs, Options(mode=m, halogens=bool(halogens), deuterium=deuterium,
                                       carbon13=carbon13, nitrogen15=nitrogen15,
                                       precursor_mz=prec))

    res = Result(mode=m, halogens=bool(halogens),
                 labels={"deuterium": deuterium, "carbon13": carbon13, "nitrogen15": nitrogen15},
                 precursor_mz=prec, fingerprint=denoise.fingerprint(),
                 table_max_mz=explain.TABLE_MAX_MZ, peaks_in=len(pairs),
                 intensity_in=sum(p[1] for p in pairs))

    if not explanations:
        res.kept = [Peak(mz, ri) for (mz, ri), ok in zip(pairs, verdicts.possible) if ok]
        res.removed = [Removed(mz, ri) for (mz, ri), ok in zip(pairs, verdicts.possible) if not ok]
        res.intensity_removed = sum(r.intensity for r in res.removed)
        return res

    kept_mz = [mz for (mz, _ri), ok in zip(pairs, verdicts.possible) if ok]
    witnesses = _formula_witnesses(kept_mz, m, bool(halogens), formulas,
                                   lim.max_formula_ions if max_formulas is None else max_formulas,
                                   explain.TABLE_MAX_MZ)
    if formulas:
        res.formulas_computed = len(witnesses)
        res.formulas_skipped = len(kept_mz) - len(witnesses)
    models = {} if not model_flags else _model_flags(kept_mz, m, bool(halogens),
                                                     lim.max_model_ions if max_models is None
                                                     else max_models)

    for (mz, ri), ok in zip(pairs, verdicts.possible):
        if ok:
            peak = Peak(mz, ri, witnesses.get(round(mz, 6)), models.get(round(mz, 6)))
            res.kept.append(peak)
            if on_explained is not None:
                on_explained("kept", peak)
        else:
            codes = explain.removal_reasons(mz, m, halogens=bool(halogens), nearest_only=nearest_only)
            near, mda = explain.distance_to_nearest(mz, m, bool(halogens))
            out = Removed(mz, ri, codes, explain.narrate(mz, m, codes, bool(halogens)), near, mda)
            res.removed.append(out)
            if on_explained is not None:
                on_explained("removed", out)

    res.intensity_removed = sum(r.intensity for r in res.removed)
    return res


def _formula_witnesses(masses: Sequence[float], mode: str, halogens: bool, want: bool,
                       budget: int, ceiling: float) -> Dict[float, str]:
    """Formulas for the lightest `budget` kept ions, deduplicated by m/z.

    A witness costs about 7.9 ms against a 21 us verdict, and above the table ceiling the solver takes
    seconds per mass, so witnesses are opt-in, capped, and never asked for above the ceiling. Which
    ions got one is reported, so a reader never mistakes "no formula" for "no composition exists".
    """
    if not want or budget <= 0:
        return {}
    out: Dict[float, str] = {}
    for mz in sorted(masses):
        if len(out) >= budget or mz > ceiling:
            break
        key = round(mz, 6)
        if key in out:
            continue
        verdict, formula = imd.possible(float(mz), mode=mode, use_halogens=halogens,
                                        need_formula=True)
        out[key] = formula
    return out


def _model_flags(masses: Sequence[float], mode: str, halogens: bool,
                 budget: int) -> Dict[float, Optional[str]]:
    """Which validity model kept each of the lightest `budget` kept ions, deduplicated by m/z."""
    out: Dict[float, Optional[str]] = {}
    for mz in sorted(masses):
        if len(out) >= budget:
            break
        key = round(mz, 6)
        if key not in out:
            out[key] = explain.model_flag(mz, mode, halogens)
    return out


def summary(res: Result) -> Dict[str, Any]:
    """The numbers first. Small enough to read on its own, whatever the spectrum size."""
    total = res.intensity_in
    return {
        "mode": res.mode,
        "halogens": res.halogens,
        "labels": dict(res.labels),
        "precursor_mz": res.precursor_mz,
        "fingerprint": res.fingerprint,
        "table_max_mz": res.table_max_mz,
        "total": res.peaks_in,
        "kept": len(res.kept),
        "removed": len(res.removed),
        "intensity_in": total,
        "intensity_removed": res.intensity_removed,
        "intensity_removed_fraction": (res.intensity_removed / total) if total > 0 else 0.0,
        "removed_above_table": res.removed_above_table,
        "formulas_computed": res.formulas_computed,
        "formulas_skipped": res.formulas_skipped,
    }


def payload(res: Result, include_kept: bool = False,
            max_removed: Optional[int] = None) -> Dict[str, Any]:
    """What to hand back as structured content: summary, removals, and kept peaks only if asked.

    `max_removed` caps how many removals are returned, because a spectrum that is mostly noise
    would otherwise come back as hundreds of kilobytes and be truncated by the caller before it is
    read. The cap never touches `summary`, so the counts stay the truth, and the result carries
    `removed_truncated` saying what was left out.
    """
    out: Dict[str, Any] = {"summary": summary(res)}
    out["removed"] = [{"mz": r.mz, "intensity": r.intensity, "reasons": list(r.reasons),
                       "narrative": r.narrative, "nearest_legal_mz": r.nearest_legal_mz,
                       "nearest_legal_mDa": r.nearest_legal_mDa} for r in res.removed]
    out["removed_truncated"] = None
    if max_removed is not None and len(out["removed"]) > max_removed:
        out["removed_truncated"] = (
            "%d of %d removals returned (the first %d, in acquisition order; max_result_peaks=%d); "
            "the summary counts cover the whole spectrum. Ask with nearest_only=true for shorter "
            "entries, or clean smaller pieces, to read the rest."
            % (max_removed, len(out["removed"]), max_removed, max_removed))
        out["removed"] = out["removed"][:max_removed]
    if include_kept:
        out["kept"] = [{"mz": p.mz, "intensity": p.intensity, "formula": p.formula,
                        "model": p.model} for p in res.kept]
    else:
        out["kept_omitted"] = ("include_kept=true would add %d peak%s (~%d bytes as JSON)"
                               % (len(res.kept), "" if len(res.kept) == 1 else "s",
                                  _json_size(res.kept)))
    return out


def _json_size(kept: List[Peak]) -> int:
    import json
    return len(json.dumps([{"mz": p.mz, "intensity": p.intensity} for p in kept]))


def progress_line(kind: str, item: Any) -> str:
    """One machine-readable line for a progress notification.

    Format, all fields space-separated and never containing a newline:
      KEPT <mz:.4f> <intensity>
      REMOVED <mz:.4f> <intensity> <code>[|<code>...]
      SPECTRUM index=<i> total=<n> kept=<k> removed=<r>
    """
    if isinstance(item, Peak):
        if kind != "kept":
            raise ValueError("a Peak streams as 'kept', not %r" % kind)
        return "KEPT %.4f %s" % (item.mz, repr(float(item.intensity)))
    if isinstance(item, Removed):
        if kind != "removed":
            raise ValueError("a Removed streams as 'removed', not %r" % kind)
        return "REMOVED %.4f %s %s" % (item.mz, repr(float(item.intensity)),
                                       "|".join(item.reasons) or "no-reason-recorded")
    if isinstance(item, Progress):
        if kind != "spectrum":
            raise ValueError("Progress streams as 'spectrum', not %r" % kind)
        return "SPECTRUM index=%d total=%d kept=%d removed=%d" % (
            item.index, item.total, item.kept, item.removed)
    raise ValueError("cannot make a progress line from %r" % (item,))
