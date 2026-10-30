"""Format-preserving cleaning: give a spectrum file back in the SAME format with impossible ions removed.

Cleaning means dropping the ions the filter calls impossible, nothing else. Kept peaks are written
back with their original text (the exact m/z and intensity tokens and their separators), so data
that needs no change is byte-identical. The only rewritten line is the MSP `Num Peaks:` count, and
only when something was removed. All other lines pass through verbatim, in order.

A record that cannot be cleaned (unparseable, over the peak cap, no ion mode) is passed through
unchanged with a short reason; the reason never quotes the user's text. Spectrum contents are never
logged here.
"""
import re
from dataclasses import dataclass, field
from itertools import islice
from typing import Callable, Dict, List, Optional, Tuple

from denoise_service.parse import SpectrumParseError, _is_header, _number, _peak

__all__ = ["ConvertLimitError", "Record", "Prepared", "ConvertResult", "detect_format",
           "split_records", "render_record", "join_records", "prepare", "apply", "convert"]

FORMATS = ("msp", "mgf", "strings", "columns")
_LINE = re.compile(r"[^\r\n]*(?:\r\n|\n|\r)|[^\r\n]+")
_TOKEN = re.compile(r"[^\s;,]+")
_PAIRISH = re.compile(r"^[^:\s]+:[^:\s]+$")
_NUM_PEAKS = re.compile(r"^(\s*num[ _-]?peaks\s*:\s*)(\d+)", re.IGNORECASE)
_NEG_SIGN = re.compile(r"[+-]")
_FIRST_NUMBER = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_NAME_CAP = 200


class ConvertLimitError(ValueError):
    """A request-level limit was exceeded; `reason` is safe to show (no user text)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class _Line:
    """A peak line: leading text, units as (prefix, text) and trailing text (no line ending)."""
    units: List[Tuple[str, str]]
    trail: str
    first: int  # index into the record's peaks of this line's first unit


@dataclass
class Record:
    index: int
    format: str
    lines: List[str]
    kinds: List[str]  # per line: "h" header, "p" peak, "b" blank
    raw: str
    name: Optional[str] = None
    mode: Optional[str] = None
    precursor_mz: Optional[float] = None
    peaks: List[Tuple[str, str]] = field(default_factory=list)  # original token text
    values: List[Tuple[float, float]] = field(default_factory=list)
    error: Optional[str] = None
    peak_lines: Dict[int, _Line] = field(default_factory=dict)

    @property
    def header_lines(self) -> List[str]:
        first = self._peak_span()[0]
        return self.lines[:first]

    @property
    def footer_lines(self) -> List[str]:
        return self.lines[self._peak_span()[1]:]

    def _peak_span(self) -> Tuple[int, int]:
        idx = [i for i, k in enumerate(self.kinds) if k == "p"]
        return (idx[0], idx[-1] + 1) if idx else (len(self.lines), len(self.lines))


@dataclass
class Prepared:
    format: str
    records: List[Record]


@dataclass
class ConvertResult:
    format: str
    text: str
    records: List[Dict[str, object]]
    summary: Dict[str, int]


def _split_lines(text: str) -> List[str]:
    return _LINE.findall(text)


_PAIR = r"[^:\s;,]+:[^:\s;,]+"
_PAIR_LINE = re.compile(r"^[\s;,]*(?:%s[\s;,]+)*%s[\s;,]*$" % (_PAIR, _PAIR))
_TWO_PAIRS = re.compile(r"%s[\s;,]+%s" % (_PAIR, _PAIR))
_NAME_LINE = re.compile(r"^name\s*:", re.IGNORECASE)


def _is_pair_line(stripped: str) -> bool:
    return _PAIR_LINE.match(stripped) is not None


def detect_format(text: str) -> str:
    """One of msp, mgf, strings, columns. Several pair-only lines are strings; one pair per line is
    one spectrum in columns."""
    lines = 0
    msp = False
    pairs = True
    multi = False
    for ln in _split_lines(text):
        s = ln.strip()
        if not s:
            continue
        lines += 1
        if s.upper() == "BEGIN IONS":
            return "mgf"
        if not msp and (_NAME_LINE.match(s) or _NUM_PEAKS.match(s)):
            msp = True
        if pairs:
            if not _is_pair_line(s):
                pairs = False
            elif not multi and _TWO_PAIRS.search(s):
                multi = True
    if msp:
        return "msp"
    return "strings" if lines and pairs and (lines == 1 or multi) else "columns"


def _norm_key(key: str) -> str:
    return re.sub(r"[_\s-]", "", key).lower()


def _mode_of(value: str) -> Optional[str]:
    v = value.strip().lower()
    if v in ("positive", "pos", "p"):
        return "pos"
    if v in ("negative", "neg", "n"):
        return "neg"
    return None


def _precursor_of(value: str) -> Optional[float]:
    m = _FIRST_NUMBER.search(value)
    if not m:
        return None
    v = float(m.group(0))
    return v if v > 0 and v != float("inf") else None


def _metadata(rec: Record) -> None:
    mgf = rec.format == "mgf"
    sep = "=" if mgf else ":"
    charge_mode = None  # type: Optional[str]
    for line, kind in zip(rec.lines, rec.kinds):
        if kind != "h" or sep not in line:
            continue
        key, value = line.strip().split(sep, 1)
        k = _norm_key(key)
        if k == ("title" if mgf else "name"):
            if rec.name is None:
                rec.name = value.strip()[:_NAME_CAP]
        elif k in ("ionmode", "ionization", "ionisation") and rec.mode is None:
            rec.mode = _mode_of(value)
        elif k == ("pepmass" if mgf else "precursormz") and rec.precursor_mz is None:
            rec.precursor_mz = _precursor_of(value)
        elif mgf and k == "charge" and charge_mode is None:
            m = _NEG_SIGN.search(value)
            if m:
                charge_mode = "neg" if m.group(0) == "-" else "pos"
    if rec.mode is None:
        rec.mode = charge_mode


def _is_num(tok: str) -> bool:
    try:
        _number(tok, 0)
    except SpectrumParseError:
        return False
    return True


def _is_annotated(head) -> bool:
    """First two tokens are numbers and the third is text (not a number or a numeric pair)."""
    a, b, c = (m.group(0) for m in head)
    if not (_is_num(a) and _is_num(b)) or _is_num(c):
        return False
    parts = c.split(":")
    return not (len(parts) == 2 and _is_num(parts[0]) and _is_num(parts[1]))


class _OverCap(Exception):
    """A peak line holds more tokens than the peak cap could ever allow."""


def _line_units(body: str, room: Optional[int] = None) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str, float, float]]]:
    """Units of one peak line as ([(prefix, text)], [(mz_tok, int_tok, mz, int)])."""
    head = list(islice(_TOKEN.finditer(body), 3))
    if len(head) == 3 and _is_annotated(head):
        # "mz intensity annotation...": ONE peak; the whole original line is kept or removed.
        first, mz_t, in_t = head[0], head[0].group(0), head[1].group(0)
        mz, inten = _number(mz_t, 0), _number(in_t, 0)
        _peak(mz, inten, "", 0)
        return [(body[:first.start()], body[first.start():_last_end(body)])], \
            [(mz_t, in_t, mz, inten)]
    # Tokenise lazily: past 2*room+2 tokens the line has more than `room` peaks (or is invalid
    # anyway), so a hostile single line is never expanded in full.
    if room is None:
        toks = list(_TOKEN.finditer(body))
    else:
        toks = list(islice(_TOKEN.finditer(body), 2 * room + 3))
        if len(toks) > 2 * room + 2:
            raise _OverCap()
    units = []  # type: List[Tuple[str, str]]
    vals = []  # type: List[Tuple[str, str, float, float]]
    prev_end = 0

    def add(first, last, mz_t, in_t) -> None:
        nonlocal prev_end
        mz, inten = _number(mz_t, 0), _number(in_t, 0)
        _peak(mz, inten, "", 0)
        units.append((body[prev_end:first.start()], body[first.start():last.end()]))
        vals.append((mz_t, in_t, mz, inten))
        prev_end = last.end()

    pairs = [t for t in toks if ":" in t.group(0)]
    if pairs and len(pairs) != len(toks):
        raise SpectrumParseError("", 0, "line mixes mz:intensity pairs and loose values")
    if pairs:
        for t in toks:
            parts = t.group(0).split(":")
            if len(parts) != 2:
                raise SpectrumParseError("", 0, "not an mz:intensity pair")
            add(t, t, parts[0], parts[1])
    else:
        if len(toks) % 2:
            raise SpectrumParseError("", 0, "odd number of values; last value has no partner")
        for i in range(0, len(toks), 2):
            add(toks[i], toks[i + 1], toks[i].group(0), toks[i + 1].group(0))
    return units, vals


def _split_ending(line: str) -> Tuple[str, str]:
    body = line.rstrip("\r\n")
    return body, line[len(body):]


def _parse_peaks(rec: Record, max_peaks: Optional[int]) -> None:
    for n, (line, kind) in enumerate(zip(rec.lines, rec.kinds), start=1):
        if kind != "p":
            continue
        body, _ = _split_ending(line)
        room = None if max_peaks is None else max(max_peaks - len(rec.values), 0)
        try:
            units, vals = _line_units(body, room)
        except _OverCap:
            rec.error = "more than %d peaks" % max_peaks
            return
        except SpectrumParseError as exc:
            rec.error = "%s (line %d of the record)" % (exc.reason, n)
            return
        rec.peak_lines[n - 1] = _Line(units, body[_last_end(body):], len(rec.peaks))
        for mz_t, in_t, mz, inten in vals:
            rec.peaks.append((mz_t, in_t))
            rec.values.append((mz, inten))
        if max_peaks is not None and len(rec.values) > max_peaks:
            rec.error = "more than %d peaks" % max_peaks
            return


def _last_end(body: str) -> int:
    """End of the last token (what follows is only separators and whitespace)."""
    i = len(body)
    while i and (body[i - 1].isspace() or body[i - 1] in ";,"):
        i -= 1
    return i


def _kind_msp(stripped: str) -> str:
    if not stripped:
        return "b"
    return "h" if _is_header(stripped) else "p"


def _make(index: int, fmt: str, lines: List[str], kinds: List[str],
          max_peaks: Optional[int]) -> Record:
    rec = Record(index=index, format=fmt, lines=lines, kinds=kinds, raw="".join(lines))
    _metadata(rec)
    _parse_peaks(rec, max_peaks)
    return rec


def split_records(text: str, fmt: str, max_peaks: Optional[int] = None,
                  max_batch: Optional[int] = None) -> List[Record]:
    """Split `text` into records; every character of `text` belongs to exactly one record.

    With `max_batch`, raises ConvertLimitError as soon as one record too many starts, before any
    record is parsed."""
    lines = _split_lines(text)
    groups = []  # type: List[Tuple[List[str], List[str]]]
    lead = []  # type: List[Tuple[str, str]]  # lines before the first record

    def start() -> Tuple[List[str], List[str]]:
        if max_batch is not None and len(groups) >= max_batch:
            raise ConvertLimitError("too many records: at most %d allowed" % max_batch)
        g = ([], [])  # type: Tuple[List[str], List[str]]
        for ln, kd in lead:
            g[0].append(ln)
            g[1].append(kd)
        del lead[:]
        groups.append(g)
        return g

    if fmt == "mgf":
        inside = False
        for ln in lines:
            s = ln.strip()
            if s.upper() == "BEGIN IONS":
                start()
                inside = True
                kind = "h"
            elif not s:
                kind = "b"
            elif s.upper() == "END IONS":
                kind, inside = "h", False
            elif not inside:
                kind = "h"
            elif s[0].isalpha() and "=" in s:
                kind = "h"
            else:
                kind = "p"
            if not groups:
                lead.append((ln, kind))
            else:
                groups[-1][0].append(ln)
                groups[-1][1].append(kind)
        if lead and not groups:
            start()
    elif fmt == "strings":
        for ln in lines:
            s = ln.strip()
            kind = "p" if s else "b"
            if kind == "p":
                g = start()
                g[0].append(ln)
                g[1].append("p")
            elif groups:
                groups[-1][0].append(ln)
                groups[-1][1].append("b")
            else:
                lead.append((ln, "b"))
    else:
        cur = None  # type: Optional[Tuple[List[str], List[str]]]
        has_peaks = has_name = blank_after_peak = False
        for ln in lines:
            s = ln.strip()
            kind = _kind_msp(s)
            if fmt == "msp" and kind == "h":
                is_name = re.match(r"^name\s*:", s, re.IGNORECASE) is not None
                if cur is not None and ((has_peaks and (blank_after_peak or is_name))
                                        or (is_name and has_name)):
                    cur = None
                if cur is None:
                    cur = start()
                    has_peaks = has_name = blank_after_peak = False
                has_name = has_name or is_name
            elif cur is None:
                if kind == "b" and not groups:
                    lead.append((ln, kind))
                    continue
                if kind == "b":
                    groups[-1][0].append(ln)
                    groups[-1][1].append(kind)
                    continue
                cur = start()
            if kind == "p":
                has_peaks, blank_after_peak = True, False
            elif kind == "b" and has_peaks:
                blank_after_peak = True
            cur[0].append(ln)
            cur[1].append(kind)
        if lead and not groups:
            start()
    return [_make(i, fmt, g[0], g[1], max_peaks) for i, g in enumerate(groups)]


def render_record(rec: Record, keep: List[bool]) -> str:
    """The record's text with the peaks whose `keep` flag is False removed."""
    if rec.error or not rec.peaks or all(keep):
        return rec.raw
    n_kept = sum(keep)
    out = []
    for i, (line, kind) in enumerate(zip(rec.lines, rec.kinds)):
        if kind == "p":
            pl = rec.peak_lines[i]
            body, ending = _split_ending(line)
            kept = [u for j, u in enumerate(pl.units) if keep[pl.first + j]]
            if kept:
                out.append(pl.units[0][0] + kept[0][1]
                           + "".join(p + t for p, t in kept[1:]) + pl.trail + ending)
            elif rec.format == "strings":
                out.append(ending)
        elif kind == "h" and rec.format == "msp":
            m = _NUM_PEAKS.match(line)
            out.append(line if not m else
                       line[:m.start(2)] + str(n_kept) + line[m.end(2):])
        else:
            out.append(line)
    return "".join(out)


def join_records(records_text: List[str], original_text: str, fmt: str) -> str:
    """Records carry their own separators and line endings, so joining is concatenation."""
    return "".join(records_text)


def prepare(text: str, default_mode: Optional[str], default_precursor: Optional[float],
            max_peaks: int, max_batch: int, fmt: Optional[str] = None) -> Prepared:
    fmt = fmt or detect_format(text)
    if fmt not in FORMATS:
        raise ConvertLimitError("format: must be one of msp, mgf, strings, columns")
    records = split_records(text, fmt, max_peaks, max_batch)
    if len(records) > max_batch:
        raise ConvertLimitError("too many records: at most %d allowed" % max_batch)
    for rec in records:
        if rec.mode is None:
            rec.mode = default_mode
        if rec.precursor_mz is None:
            rec.precursor_mz = default_precursor
    return Prepared(fmt, records)


def apply(prep: Prepared, classify: Callable[[List[Tuple[float, float]], str, Optional[float]],
                                              List[bool]]) -> ConvertResult:
    texts, info = [], []
    cleaned = passed = peaks = bad = 0
    for rec in prep.records:
        err = rec.error
        out = rec.raw
        removed = 0
        if err is None and rec.peaks:
            if rec.mode is None:
                err = "no ion mode"
            else:
                try:
                    flags = list(classify(rec.values, rec.mode, rec.precursor_mz))
                    if len(flags) != len(rec.values):
                        raise ValueError("verdict count mismatch")
                    out = render_record(rec, flags)
                    removed = flags.count(False)
                except Exception:
                    err, out, removed = "classification failed", rec.raw, 0
        texts.append(out)
        info.append({"index": rec.index, "name": rec.name, "total": len(rec.peaks),
                     "impossible": removed, "error": err, "mode": rec.mode,
                     "precursor_mz": rec.precursor_mz})
        if err:
            passed += 1
        else:
            cleaned += 1
            peaks += len(rec.peaks)
            bad += removed
    return ConvertResult(prep.format, "".join(texts), info,
                         {"records": len(prep.records), "cleaned": cleaned,
                          "passed_through": passed, "peaks": peaks, "impossible": bad})


def convert(text: str, classify, default_mode: Optional[str] = None,
            default_precursor: Optional[float] = None, max_peaks: int = 5000,
            max_batch: int = 200, fmt: Optional[str] = None) -> ConvertResult:
    return apply(prepare(text, default_mode, default_precursor, max_peaks, max_batch, fmt),
                 classify)
