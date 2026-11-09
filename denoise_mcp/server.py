"""The MCP surface for the impossible-mass filter: clean spectra, explain ions, stream progress.

Why this layer exists: `denoise.is_possible` answers yes or no. A curator removing a peak wants the
why, and an agent wants it without opening a terminal. The verdicts themselves come from
`denoise_service.classify` - the same call the REST service makes - so the two surfaces cannot
drift apart; everything here adds explanation, shape and streaming on top.

Needs Python >= 3.10 (the MCP SDK) and its own virtualenv: the production analysis nodes are
Python 3.9, and the pure layer (`limits.py`, `explain.py`, `clean.py`) stays 3.9-compatible so the
same cleaning code runs there.

Answers stay small on purpose: a summary plus the removed peaks, with the clean peak list behind
`include_kept`. Agent harnesses truncate tool results around 20 KiB, and a truncated spectrum is
worse than a summary that tells you how to ask for the rest.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Annotated, Any, Dict, List, Literal, Optional, Tuple

import anyio
import denoise
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from . import clean, explain
from .limits import LimitError, Limits

PROTOCOL = "2025-11-25"
INSTRUCTIONS = (
    "Remove impossible peaks from a mass spectrum, and explain every removal. "
    "A peak is possible when some composition of C/H/N/O/P/S (plus F/Cl/Br/I if you allow "
    "halogens) has exactly that mass under the selected electron count and charge. "
    "Use clean_spectrum for one spectrum, clean_batch for many; both stream progress as they work. "
    "Results come back as a summary plus the removed peaks with reason codes; pass include_kept "
    "only when you need the clean peak list itself."
)

MODE = Literal["pos", "neg"]
PeakPair = Tuple[float, float]


# ---------------------------------------------------------------- output shapes (flat, typed)


class Summary(BaseModel):
    mode: MODE
    halogens: bool
    labels: Dict[str, int]
    precursor_mz: Optional[float]
    fingerprint: str = Field(description="Build fingerprint of the chemistry tables in use.")
    table_max_mz: float = Field(description="Highest mass the precomputed tables cover. Above it the "
                                            "model answers possible for every mass, so nothing is "
                                            "reported impossible there.")
    total: int
    kept: int
    removed: int
    intensity_in: float
    intensity_removed: float
    intensity_removed_fraction: float
    removed_above_table: int = Field(description="Removed peaks heavier than table_max_mz; always 0, "
                                                 "because above the ceiling the model says possible.")
    formulas_computed: int
    formulas_skipped: int = Field(description="Kept peaks left without a formula because formulas "
                                               "are capped per request.")


class SpectrumResult(Summary):
    kept_peaks: Optional[List["KeptPeak"]] = Field(
        description="Only with include_kept=true; otherwise null.", default=None)
    kept_truncated: Optional[str] = Field(
        description="Set when this spectrum's kept peaks were left out of the answer; says how many "
                    "and why.", default=None)


class RemovedPeak(BaseModel):
    mz: float
    intensity: float
    reasons: List[str] = Field(description="Reason codes; see the denoise://reason-codes resource.")
    narrative: str = Field(description="The same reasons in sentences.")
    nearest_legal_mz: Optional[float]
    nearest_legal_mDa: Optional[float] = Field(description="Signed offset of the nearest legal "
                                                           "mass; positive means it sits above "
                                                           "this ion.")


class KeptPeak(BaseModel):
    mz: float
    intensity: float = Field(description="Passed through untouched - the filter never rescales.")
    formula: Optional[str] = Field(description="Only with formulas=true; null when the per-request "
                                               "formula budget was reached.")
    model: Optional[str] = Field(description="Only with model_flags=true: both-models, lenient-only "
                                             "or strict-only. lenient-only and strict-only are where "
                                             "the two published models disagree - look at those "
                                             "first.")


class CleanResult(BaseModel):
    summary: Summary
    removed: List[RemovedPeak] = Field(description="The removals, in acquisition order, capped at "
                                                    "max_result_peaks; the summary counts the whole "
                                                    "spectrum.")
    kept: Optional[List[KeptPeak]] = None
    kept_omitted: Optional[str] = None
    kept_truncated: Optional[str] = Field(
        description="Set when include_kept=true and the peak list had to be shortened to fit "
                    "max_answer_bytes.", default=None)
    removed_truncated: Optional[str] = Field(
        description="Set when removals were left out of this answer; says how many and how to get "
                    "the rest.", default=None)
    answer_bytes: int = Field(
        description="Size of this answer as JSON, measured with this field at 0. A reply bigger "
                    "than max_answer_bytes is fitted to it, and what was given up is written into "
                    "removed_truncated, kept_truncated or answer_oversized.", default=0)
    answer_oversized: Optional[str] = Field(
        description="Set when the answer could not be fitted inside max_answer_bytes at all; the "
                    "summary is the part that cannot be shortened.", default=None)


class SpectrumSpec(BaseModel):
    """One spectrum in a batch."""

    peaks: Annotated[List[PeakPair], Field(description="(m/z, intensity) pairs, in the order the "
                                                        "acquisition reported them.")]
    mode: MODE
    halogens: bool = False
    deuterium: int = 0
    carbon13: int = 0
    nitrogen15: int = 0
    precursor_mz: Optional[float] = None


class BatchSummary(BaseModel):
    spectra: int
    total: int
    kept: int
    removed: int
    intensity_removed_fraction: float
    fingerprint: str
    table_max_mz: float


class BatchResult(BaseModel):
    summary: BatchSummary
    per_spectrum: List[SpectrumResult] = Field(
        description="One summary per input spectrum, in order. Removals stream as progress lines "
                    "instead of being repeated here.")
    answer_bytes: int = Field(
        description="Size of the summary and per_spectrum lists as JSON, measured with this field "
                    "at 0; the fit reserves room for the number itself.", default=0)
    answer_oversized: Optional[str] = Field(
        description="Set when the answer is bigger than max_answer_bytes, the size an agent harness "
                    "reads before cutting the rest; says how many spectra fit whole.", default=None)


class IonExplanation(BaseModel):
    mz: float
    mode: MODE
    possible: bool
    reasons: List[str]
    narrative: str
    nearest_legal_mz: Optional[float]
    nearest_legal_mDa: Optional[float]
    model: Optional[str] = Field(description="Only with model_flags=true: both-models, lenient-only "
                                             "or strict-only; null for a peak no model accepts.",
                                default=None)
    table_max_mz: float
    fingerprint: str


class ServiceInfo(BaseModel):
    fingerprint: str
    table_max_mz: float
    tables: Dict[str, str] = Field(description="What each precomputed table reports: loaded or not, "
                                               "and what it covers.")
    cache_entries: int = Field(description="Memoised verdicts held in this process; the service "
                                           "drops them periodically.")
    limits: Dict[str, int]
    tools: List[str]
    protocol: str


KeptPeak.model_rebuild()
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True,
                            openWorldHint=False)


def _clean_answer_bytes(answer: Dict[str, Any]) -> int:
    """Bytes this answer costs on the wire, optional fields and all.

    `clean.payload` fits an answer to a byte budget, and the fitting has to measure what the
    transport actually sends: this model, not the dict it was built from. Two optional fields
    serialized as null are around 40 bytes the dict does not contain, enough to put an answer that
    measured as fitting past the budget. Compact, because that is how JSON-RPC goes out.
    """
    return len(json.dumps(CleanResult(**answer).model_dump(mode="json"),
                          separators=(",", ":")))


def _limits_dict(lim: Limits) -> Dict[str, int]:
    return {"max_peaks": lim.max_peaks, "max_batch": lim.max_batch, "max_label": lim.max_label,
            "max_label_lookups": lim.max_label_lookups,
            "max_request_lookups": lim.max_request_lookups,
            "max_formula_ions": lim.max_formula_ions, "max_model_ions": lim.max_model_ions,
            "max_explanation_work_us": lim.max_explanation_work_us,
            "max_result_peaks": lim.max_result_peaks,
            "max_answer_bytes": lim.max_answer_bytes}


async def _in_thread(work, *args):
    """Cleaning is CPU work; keep the loop free to deliver progress while it runs."""
    return await anyio.to_thread.run_sync(work, *args)


def security_for(allowed_hosts: Optional[List[str]] = None):
    """DNS-rebinding protection for a hosted server, or None to leave it off.

    On a public host the Host header is the only thing separating "the MCP endpoint" from "whatever
    DNS says tomorrow": with no allowlist a browser on the operator's laptop can be pointed at an
    attacker's domain that resolves to 127.0.0.1 and be served this endpoint. Setting an allowlist
    makes the server refuse any other Host with HTTP 421.
    """
    if not allowed_hosts:
        return None
    from mcp.server.transport_security import TransportSecuritySettings
    return TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                     allowed_hosts=list(allowed_hosts))


def build_server(limits: Optional[Limits] = None, name: str = "impossible-mass-denoise",
                 version: str = "1.0.0") -> MCPServer:
    """The configured server. Host it with `serve(...)`, or run it over stdio."""
    lim = limits or Limits()
    server = MCPServer(name=name, version=version, instructions=INSTRUCTIONS)

    @server.tool(
        name="clean_spectrum",
        title="Clean one spectrum and explain every removal",
        description=(
            "Remove peaks that no possible ion can produce, and say why each one went. Returns a "
            "summary plus the removed peaks with reason codes and plain-language narration; the "
            "clean peak list is opt-in with include_kept so the answer stays readable for large "
            "spectra. Set stream=true to receive KEPT/REMOVED lines while the work proceeds. "
            "halogens=true admits F/Cl/Br/I (bromide and chloride ions) but rejects fewer noise "
            "peaks overall; deuterium/carbon13/nitrogen15 declare the label count of a "
            "heavy-isotope internal standard; precursor_mz keeps the precursor whatever its verdict."),
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def clean_spectrum(
        peaks: Annotated[List[PeakPair], Field(description="(m/z, intensity) pairs in acquisition "
                                                            "order, at most max_peaks of them.")],
        mode: Annotated[MODE, Field(description="pos for [M+H]+ / [M+Na]+ style, neg for [M-H]-.")],
        halogens: bool = False,
        deuterium: int = 0,
        carbon13: int = 0,
        nitrogen15: int = 0,
        precursor_mz: Optional[float] = None,
        explanations: Annotated[bool, Field(description="Compute reason codes for removals "
                                                        "(default true).")] = True,
        formulas: Annotated[bool, Field(description="Also return a formula witness for kept peaks: "
                                                    "about 500x the cost of a verdict, so it is "
                                                    "capped per request.")] = False,
        nearest_only: Annotated[bool, Field(description="Report only the distance to the nearest "
                                                        "legal mass instead of every reason.")] = False,
        model_flags: Annotated[bool, Field(description="Say which validity model kept each peak: "
                                                       "about 200x the cost of a verdict, so it is "
                                                       "capped per request.")] = False,
        include_kept: Annotated[bool, Field(description="Return the cleaned peak list too.")] = False,
        stream: Annotated[bool, Field(description="Emit progress notifications while cleaning.")] = False,
        min_progress_interval_ms: Annotated[
            int, Field(ge=0, le=5000, description="Throttle progress lines to at most one per "
                                                  "interval; 0 sends every line.")] = 0,
        ctx: Context = None,
    ) -> CleanResult:
        def work(on_explained):
            return clean.clean_spectrum(peaks, mode, halogens=halogens, deuterium=deuterium,
                                        carbon13=carbon13, nitrogen15=nitrogen15,
                                        precursor_mz=precursor_mz, explanations=explanations,
                                        formulas=formulas, model_flags=model_flags,
                                        nearest_only=nearest_only,
                                        limits=lim, on_explained=on_explained)
        try:
            res = await (_streamed(work, ctx, min_interval_ms=min_progress_interval_ms)
                         if (stream and ctx is not None) else _in_thread(work, None))
        except LimitError as exc:
            raise ToolError(str(exc)) from None
        return CleanResult(**clean.payload(res, include_kept=include_kept,
                                           max_removed=lim.max_result_peaks,
                                           max_bytes=lim.max_answer_bytes,
                                           measure=_clean_answer_bytes))

    @server.tool(
        name="clean_batch",
        title="Clean many spectra, reporting progress as each one finishes",
        description=(
            "Clean a list of spectra in one request. Progress is reported as each spectrum "
            "finishes (SPECTRUM index=.. total=.. kept=.. removed=..) and, unless explanations "
            "is false, as each peak is explained, so a long batch is never a silent wait. The "
            "result carries one summary per spectrum; which peaks went is in the progress lines, "
            "so explanations=false leaves only the counts."),
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def clean_batch(
        spectra: Annotated[List[SpectrumSpec], Field(description="Up to max_batch spectra.")],
        explanations: Annotated[bool, Field(description="Narrate each peak as it is explained, in "
                                                        "the progress lines.")] = True,
        formulas: bool = False,
        model_flags: bool = False,
        nearest_only: bool = False,
        include_kept: Annotated[bool, Field(description="Return the cleaned peaks of every "
                                                        "spectrum in the result.")] = False,
        stream: Annotated[bool, Field(description="Emit progress notifications while working.")] = True,
        min_progress_interval_ms: Annotated[int, Field(ge=0, le=5000)] = 0,
        ctx: Context = None,
    ) -> BatchResult:
        total = len(spectra)
        done = {"spectra": 0, "kept": 0, "removed": 0}

        # Checked before any work, so a batch that cannot be answered is refused as a whole rather
        # than "stopped on spectrum 1 of 3" - which would describe work that never started.
        try:
            lim.batch(spectra)
        except LimitError as exc:
            raise ToolError(str(exc)) from None

        def work(on_explained):
            out = []
            for spec in spectra:
                res = clean.clean_spectrum(spec.peaks, spec.mode, halogens=spec.halogens,
                                           deuterium=spec.deuterium, carbon13=spec.carbon13,
                                           nitrogen15=spec.nitrogen15,
                                           precursor_mz=spec.precursor_mz,
                                           explanations=explanations, formulas=formulas,
                                           model_flags=model_flags,
                                           nearest_only=nearest_only, limits=lim,
                                           on_explained=on_explained if explanations else None)
                done["spectra"] += 1
                done["kept"] += len(res.kept)
                done["removed"] += len(res.removed)
                if on_explained is not None:
                    on_explained("spectrum", clean.Progress(index=done["spectra"], total=total,
                                                            kept=len(res.kept),
                                                            removed=len(res.removed)))
                out.append(res)
            return out

        try:
            res_list = await (_streamed(work, ctx, total=total,
                                        min_interval_ms=min_progress_interval_ms)
                              if (stream and ctx is not None) else _in_thread(work, None))
        # Either way, say how far the batch got: work already done is worth reporting even when
        # the request as a whole is refused, and "it failed" alone makes the caller redo it all.
        except LimitError as exc:
            raise ToolError("clean_batch stopped on spectrum %d of %d (%d already cleaned): %s"
                            % (done["spectra"] + 1, total, done["spectra"], exc)) from None
        except ToolError:
            raise
        except Exception as exc:            # a fault, not a rejection: name where it happened
            raise ToolError("clean_batch failed on spectrum %d of %d (%d already cleaned): %s"
                            % (done["spectra"] + 1, total, done["spectra"], exc)) from None

        per_spectrum = []
        for res in res_list:
            data = clean.summary(res)
            if include_kept:
                peaks = [{"mz": p.mz, "intensity": p.intensity, "formula": p.formula,
                          "model": p.model} for p in res.kept]
                # A batch asks for the peaks of every spectrum at once, so the per-spectrum list is
                # capped as well; without this, 200 spectra is tens of megabytes.
                if len(peaks) > lim.max_result_peaks:
                    data["kept_truncated"] = (
                        "%d of %d kept peaks returned (the first %d, in acquisition order; "
                        "max_result_peaks=%d per spectrum); the summary counts cover the whole "
                        "spectrum." % (lim.max_result_peaks, len(peaks), lim.max_result_peaks,
                                       lim.max_result_peaks))
                    peaks = peaks[:lim.max_result_peaks]
                data["kept_peaks"] = peaks
            per_spectrum.append(SpectrumResult(**data))
        intensity_in = sum(r.intensity_in for r in res_list)
        intensity_out = sum(r.intensity_removed for r in res_list)
        batch_summary = BatchSummary(spectra=len(res_list),
                                     total=sum(r.peaks_in for r in res_list),
                                     kept=done["kept"], removed=done["removed"],
                                     intensity_removed_fraction=(intensity_out / intensity_in)
                                     if intensity_in > 0 else 0.0,
                                     fingerprint=res_list[0].fingerprint,
                                     table_max_mz=res_list[0].table_max_mz)
        # A batch is the one answer whose size the caller cannot shape per spectrum: 200 summaries
        # alone is around 80 KB, which an agent harness cuts without saying so. Nothing here can be
        # dropped without losing the point of the call, so measure it and say what would fit whole.
        # Measured as the model that goes out, so the null fields are counted too.
        measured = len(json.dumps(
            BatchResult(summary=batch_summary, per_spectrum=per_spectrum,
                        answer_bytes=0, answer_oversized=None).model_dump(mode="json"),
            separators=(",", ":")))
        note = None
        if measured > lim.max_answer_bytes:
            each = measured / max(1, len(per_spectrum))
            note = ("this answer is %d bytes, past max_answer_bytes=%d, which is how much an agent "
                    "harness reads before cutting the rest without saying so; spectra like these "
                    "cost about %.0f bytes each, so about %d fit whole in one answer - call with "
                    "fewer at a time to read all of it"
                    % (measured, lim.max_answer_bytes, each,
                       max(1, int(lim.max_answer_bytes // each))))
        return BatchResult(summary=batch_summary, per_spectrum=per_spectrum,
                           answer_bytes=measured, answer_oversized=note)

    @server.tool(
        name="explain_ion",
        title="Explain one ion",
        description=("Answer for a single m/z: is it possible under the model, and if not why not - "
                     "the halogen alphabet, a doubly charged ion, a heavy-isotope label, a gap in "
                     "legal mass space, or simply beyond the tables. Cheaper than clean_spectrum "
                     "when you have one ion. model_flags=true also names the validity model that accepts "
            "it, which is where the two published models disagree."),
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def explain_ion(
        mz: Annotated[float, Field(description="The ion's m/z.")],
        mode: MODE,
        halogens: bool = False,
        nearest_only: Annotated[bool, Field(description="Report only the distance to the nearest "
                                                        "legal mass.")] = False,
        model_flags: Annotated[bool, Field(description="Also say which validity model accepts this "
                                                       "ion (about 200x the cost of a verdict).")]
        = False,
    ) -> IonExplanation:
        try:
            lim.mode(mode)
            value = float(mz)
            if value <= 0.0:
                raise LimitError("mz must be a finite number > 0")
        except LimitError as exc:
            raise ToolError(str(exc)) from None

        def work():
            codes = explain.removal_reasons(value, mode, halogens=halogens,
                                            nearest_only=nearest_only)
            near, mda = explain.distance_to_nearest(value, mode, halogens)
            model = explain.model_flag(value, mode, halogens) if model_flags else None
            return codes, near, mda, model

        codes, near, mda, model = await _in_thread(work)
        return IonExplanation(
            mz=value, mode=mode, possible=(not codes), reasons=list(codes),
            narrative=explain.narrate(value, mode, codes, halogens),
            nearest_legal_mz=near, nearest_legal_mDa=mda, model=model,
            table_max_mz=explain.TABLE_MAX_MZ, fingerprint=denoise.fingerprint())

    @server.tool(
        name="service_info",
        title="What this service is running",
        description=("The chemistry tables' fingerprint and ceiling, their load state, and every "
                     "limit that will get a request rejected. Read it before a large request."),
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def service_info() -> ServiceInfo:
        status = await _in_thread(lambda: (denoise.table_status(), denoise.halogen_table_status(),
                                           denoise.cache_size()))
        return ServiceInfo(fingerprint=denoise.fingerprint(),
                           table_max_mz=explain.TABLE_MAX_MZ,
                           tables={"chnops": status[0], "halogen": status[1]},
                           cache_entries=status[2],
                           limits=_limits_dict(lim),
                           tools=["clean_spectrum", "clean_batch", "explain_ion", "service_info"],
                           protocol=PROTOCOL)

    @server.resource(uri="denoise://reason-codes",
                     name="Reason codebook",
                     title="Every reason code and what it means",
                     description="The meanings the reason codes in clean results refer to.",
                     mime_type="application/json")
    def reason_codes() -> str:
        return Path(__file__).with_name("reason-codes.json").read_text()

    return server


def serve(server: MCPServer, *, host: str, port: int, path: str = "/mcp", stateless: bool = False,
          json_response: bool = False, session_idle_timeout: Optional[float] = None,
          allowed_hosts: Optional[List[str]] = None) -> None:
    """Block, serving `server` over streamable HTTP. Thin over the SDK's own `run`."""
    kwargs: Dict[str, Any] = dict(host=host, port=port, streamable_http_path=path,
                                  stateless_http=stateless, json_response=json_response,
                                  transport_security=security_for(allowed_hosts))
    if session_idle_timeout is not None:
        # Passing None to the SDK means "never time out"; only set it when the operator asked.
        kwargs["session_idle_timeout"] = session_idle_timeout
    server.run("streamable-http", **kwargs)


async def _streamed(work, ctx: Context, total: Optional[int] = None,
                    min_interval_ms: int = 0):
    """Run `work(on_explained)` in a worker thread, forwarding its events as progress notifications.

    The worker blocks when the buffer fills, so a 5000-peak spectrum cannot outrun the socket; the
    caller sees each peak as it is explained rather than the whole answer at the end.
    """
    send, receive = anyio.create_memory_object_stream(max_buffer_size=64)
    box: Dict[str, Any] = {}

    def emit(kind, item):
        # Called in the worker thread; schedule the send back onto the event loop.
        anyio.from_thread.run(forward, clean.progress_line(kind, item))

    async def forward(line: str) -> None:
        await send.send(line)

    async def producer():
        async with send:
            box["result"] = await anyio.to_thread.run_sync(work, emit)

    async with anyio.create_task_group() as tg:
        tg.start_soon(producer)
        counter = 0
        last = 0.0
        gap = min_interval_ms / 1000.0
        async with receive:
            async for line in receive:
                counter += 1
                now = time.monotonic()
                if gap and (now - last) < gap and not line.startswith("SPECTRUM "):
                    continue        # thin the per-peak lines; a finished spectrum always reports
                last = now
                await ctx.report_progress(progress=counter, total=total, message=line)
    return box["result"]
