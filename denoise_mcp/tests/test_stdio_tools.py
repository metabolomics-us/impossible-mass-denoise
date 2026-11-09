"""Integration layer: the real server over the real stdio transport.

A child process runs `python -m denoise_mcp --stdio` and a real MCP client session talks to it.
No in-process shortcuts: if the packaging, the entry point, the schemas or the structured output
are wrong, this fails.

Fixture peaks (positive mode, real masses): 181.0707 and 85.0284 are possible; 100.5 and 123.9 are
not, and their reasons differ (one sits near nothing, the other needs a halogen).
"""
from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[2]

TOOL_ARGS = {"mode": "pos",
             "peaks": [[181.0707, 100.0], [100.5, 20.0], [123.9, 5.0], [85.0284, 40.0]]}


@asynccontextmanager
async def session():
    """A live client session, opened and closed inside the calling task.

    Deliberately not an async pytest fixture: the MCP client opens anyio task groups, and a task
    group must be entered and exited by the same task, which a generator fixture does not promise.
    """
    params = StdioServerParameters(command=sys.executable, args=["-m", "denoise_mcp", "--stdio"],
                                   cwd=str(ROOT))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            yield s


def as_json(result):
    assert not result.is_error, result.content
    assert result.structured_content is not None, "no structured content came back: %r" % (result,)
    return result.structured_content


def error_text(result):
    assert result.is_error, "the call should have failed: %r" % (result,)
    return "".join(c.text for c in result.content if c.type == "text")


# ---------------------------------------------------------------- discovery


async def test_tools_are_listed_with_their_own_output_shape():
    async with session() as s:
        tools = {t.name: t for t in (await s.list_tools()).tools}
    assert {"clean_spectrum", "clean_batch", "explain_ion", "service_info"} <= set(tools)
    for name, tool in tools.items():
        assert tool.input_schema["type"] == "object", name
        assert tool.description.strip(), name
        props = set(tool.output_schema.get("properties", {}))
        assert props, "%s publishes no output schema" % name
        assert "result" not in props, "%s wraps its answer in a 'result' envelope" % name
    assert {"removed", "summary"} <= set(tools["clean_spectrum"].output_schema["properties"])


async def test_the_reason_codebook_is_a_readable_resource():
    async with session() as s:
        resource = (await s.read_resource("denoise://reason-codes")).contents[0]
    book = json.loads(resource.text)
    assert set(book["codes"]) >= {"no-legal-ion-nearby", "possible-as-doubly-charged",
                                 "needs-halogen-alphabet"}
    assert all(book["meaning"][c] for c in book["codes"])


async def test_service_info_reports_the_chemistry_the_client_is_trusting():
    import denoise
    async with session() as s:  # the fingerprint is what the client's own table must match
        info = as_json(await s.call_tool("service_info", {}))
    assert info["fingerprint"] == denoise.fingerprint()
    assert info["table_max_mz"] == 1700.0
    assert info["tables"]["chnops"] and info["tables"]["halogen"]
    assert info["protocol"] == "2025-11-25"
    assert info["limits"]["max_peaks"] > 0


# ---------------------------------------------------------------- clean_spectrum


async def test_clean_spectrum_answers_with_a_summary_then_lists_removals():
    async with session() as s:
        out = as_json(await s.call_tool("clean_spectrum", TOOL_ARGS))
    assert out["summary"]["total"] == 4
    assert out["summary"]["kept"] == 2 and out["summary"]["removed"] == 2
    assert [r["mz"] for r in out["removed"]] == [100.5, 123.9]
    assert out["kept"] is None, "the full clean spectrum must be opt-in"
    assert "include_kept=true" in out["kept_omitted"]
    reasons = {r["mz"]: r["reasons"] for r in out["removed"]}
    assert "needs-halogen-alphabet" in reasons[123.9]
    assert "nearest-legal-below" in reasons[100.5]
    for r in out["removed"]:
        assert r["narrative"].endswith(".") and r["nearest_legal_mz"]


async def test_include_kept_returns_the_clean_spectrum_and_model_flags_name_the_model():
    async with session() as s:
        out = as_json(await s.call_tool("clean_spectrum",
                                        dict(TOOL_ARGS, include_kept=True, model_flags=True)))
    assert [p["mz"] for p in out["kept"]] == [181.0707, 85.0284]
    assert all(p["model"] in ("both-models", "strict-only", "lenient-only") for p in out["kept"])
    assert all(p["formula"] is None for p in out["kept"]), "formulas are opt-in too"
    assert out["kept_omitted"] is None


async def test_formula_witnesses_are_opt_in():
    async with session() as s:
        plain = as_json(await s.call_tool("clean_spectrum", dict(TOOL_ARGS, include_kept=True)))
        assert all(p["formula"] is None for p in plain["kept"])
        with_form = as_json(await s.call_tool(
            "clean_spectrum", dict(TOOL_ARGS, include_kept=True, formulas=True)))
    assert all(p["formula"] for p in with_form["kept"])
    assert with_form["summary"]["formulas_computed"] == 2


async def test_halogens_and_labels_change_the_verdict_over_the_wire():
    async with session() as s:
        single = {"mode": "pos", "peaks": [[98.92952, 100.0]]}
        refused = as_json(await s.call_tool("clean_spectrum", single))
        assert refused["summary"]["kept"] == 0
        halogen = as_json(await s.call_tool("clean_spectrum", dict(single, halogens=True)))
        assert halogen["summary"]["kept"] == 1
        assert halogen["summary"]["halogens"] is True


async def test_explain_ion_answers_without_a_spectrum():
    async with session() as s:
        out = as_json(await s.call_tool("explain_ion", {"mz": 100.5, "mode": "pos"}))
        assert out["possible"] is False
        assert out["reasons"] and out["narrative"]
        # nothing removed may report a distance the tolerance would have kept
        assert abs(out["nearest_legal_mDa"]) > 5.0
        assert out["model"] is None, "naming the model is opt-in"
        with_model = as_json(await s.call_tool("explain_ion", {"mz": 181.0707, "mode": "pos",
                                                              "model_flags": True}))
        plain = as_json(await s.call_tool("explain_ion", {"mz": 181.0707, "mode": "pos"}))
    assert plain["possible"] is True and plain["reasons"] == []
    assert plain["model"] is None
    assert with_model["model"] == "both-models"


# ---------------------------------------------------------------- errors are not swallowed


async def test_a_mode_the_filter_does_not_have_reaches_the_client_as_its_own_message():
    async with session() as s:
        text = error_text(await s.call_tool("clean_spectrum", dict(TOOL_ARGS, mode="negative")))
    assert "pos" in text and "neg" in text


async def test_too_many_peaks_reaches_the_client_with_the_number():
    from denoise_mcp import limits
    cap = limits.Limits().max_peaks
    async with session() as s:
        text = error_text(await s.call_tool(
            "clean_spectrum", {"mode": "pos", "peaks": [[100.5, 1.0]] * (cap + 1)}))
    assert str(cap) in text


async def test_a_wrong_type_is_refused_by_the_schema_before_reaching_the_code():
    async with session() as s:
        text = error_text(await s.call_tool("clean_spectrum",
                                            dict(TOOL_ARGS, peaks=[[181.0707, "high"]])))
    assert "validation error" in text


async def test_a_nonexistent_tool_is_refused():
    async with session() as s:
        text = error_text(await s.call_tool("clean_spectrun", TOOL_ARGS))
    assert "Unknown tool" in text


# ---------------------------------------------------------------- streaming


async def test_batch_streams_partial_progress_before_the_result():
    seen = []
    done = {"result": False}

    async def on_progress(progress, total, message):
        if not done["result"]:
            seen.append(message)

    spectra = [{"mode": "pos", "peaks": [[181.0707, 100.0], [100.5, 20.0]]}] * 5
    async with session() as s:
        result = await s.call_tool("clean_batch",
                                   {"spectra": spectra, "stream": True, "explanations": True},
                                   progress_callback=on_progress)
    done["result"] = True
    out = as_json(result)
    assert out["summary"]["spectra"] == 5
    assert len(seen) >= 5, "expected at least one message per spectrum, got %d" % len(seen)
    assert all(m.split()[0] in ("SPECTRUM", "KEPT", "REMOVED") for m in seen)
    assert "index=" in seen[-1] and "total=" in seen[-1]


async def test_batch_without_stream_still_answers():
    spectra = [{"mode": "pos", "peaks": [[181.0707, 100.0]]},
               {"mode": "neg", "peaks": [[112.9855, 100.0]]}]
    async with session() as s:
        out = as_json(await s.call_tool("clean_batch", {"spectra": spectra}))
    assert out["summary"]["spectra"] == 2
    assert out["summary"]["total"] == 2
    assert len(out["per_spectrum"]) == 2
    assert [sp["mode"] for sp in out["per_spectrum"]] == ["pos", "neg"]
    assert out["per_spectrum"][0]["kept_peaks"] is None


async def test_a_batch_that_starts_cleaning_reports_the_work_already_done_when_one_fails():
    """A half-finished batch must not answer with nothing."""
    # the second spectrum is well formed but over the label budget, so the batch stops at it
    spectra = [{"mode": "pos", "peaks": [[181.0707, 100.0]]},
               {"mode": "pos", "peaks": [[100.5, 1.0]], "deuterium": 13}]
    async with session() as s:
        result = await s.call_tool("clean_batch", {"spectra": spectra})
    assert result.is_error
    text = error_text(result)
    assert "spectrum 2 of 2" in text and "1 already cleaned" in text, text
