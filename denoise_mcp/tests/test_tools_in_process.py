"""The tools themselves, through a real client session in this process.

Discovery and the wire are proved elsewhere (`test_stdio_tools.py`, `test_http_hosting.py`). What is
checked here is what each tool returns and what it refuses: verdicts, reason codes, the payload
shape that keeps an agent from being handed 300 KB it will truncate, the progress lines a long clean
emits, and the errors that name the limit a request broke and the cheaper option that would not.

`in_process()` runs the server here rather than in a child process, so this is also the layer that
measures coverage of `server.py`.
"""
from __future__ import annotations

import math

import pytest

from denoise_mcp import explain, gen_codebook
from denoise_mcp.limits import Limits
from denoise_mcp.server import build_server, security_for
from denoise_mcp.tests.in_process import as_json, error_text, in_process

PEAKS = [[181.0707, 100.0], [100.5, 20.0], [123.9, 5.0], [85.0284, 40.0]]
POSSIBLE = [181.0707, 85.0284]
IMPOSSIBLE = [100.5, 123.9]


# ---------------------------------------------------------------- discovery


async def test_the_four_tools_are_offered_and_only_the_expected_arguments_exist():
    async with in_process() as s:
        tools = {t.name: t for t in (await s.list_tools()).tools}
    assert set(tools) == {"clean_spectrum", "clean_batch", "explain_ion", "service_info"}
    for name, tool in tools.items():
        assert tool.input_schema["type"] == "object", name
        assert tool.output_schema is not None, "%s has no output shape" % name
        assert tool.description and len(tool.description) > 80, name
        assert tool.annotations is not None and tool.annotations.read_only_hint is True, name
    assert set(tools["clean_spectrum"].input_schema["properties"]) == {
        "peaks", "mode", "halogens", "deuterium", "carbon13", "nitrogen15", "precursor_mz",
        "explanations", "formulas", "nearest_only", "model_flags", "include_kept", "stream",
        "min_progress_interval_ms"}
    assert set(tools["clean_batch"].input_schema["properties"]) == {
        "spectra", "explanations", "formulas", "model_flags", "nearest_only",
        "include_kept", "stream", "min_progress_interval_ms"}
    assert "ctx" not in tools["clean_spectrum"].input_schema["properties"], \
        "the MCP context must not look like an argument" 


async def test_the_reason_codebook_is_a_resource_and_matches_the_codes_the_code_emits():
    import json

    async with in_process() as s:
        listed = (await s.list_resources()).resources
        assert [r.uri for r in listed] == ["denoise://reason-codes"]
        text = (await s.read_resource("denoise://reason-codes")).contents[0].text

    book = json.loads(text)
    # Compare through JSON: book() holds label steps as tuples, which the resource publishes as lists.
    assert book == json.loads(json.dumps(gen_codebook.book()))
    assert set(book["codes"]) == set(explain.REASON_CODES)
    for code in book["codes"]:
        assert book["meaning"][code], code            # no code ships without a meaning
    assert book["strong"] and set(book["strong"]) <= set(book["codes"])


# ---------------------------------------------------------------- clean_spectrum


async def test_a_clean_reports_the_verdict_and_names_every_removal():
    async with in_process() as s:
        out = as_json(await s.call_tool("clean_spectrum", {"peaks": PEAKS, "mode": "pos"}))
    summary = out["summary"]
    assert summary["total"] == 4 and summary["kept"] == 2 and summary["removed"] == 2
    assert [p["mz"] for p in out["removed"]] == IMPOSSIBLE
    assert summary["intensity_in"] == 165.0
    assert summary["intensity_removed"] == 25.0
    assert summary["intensity_removed_fraction"] == pytest.approx(25.0 / 165.0)
    assert summary["fingerprint"] and summary["table_max_mz"] == 1700.0
    for peak in out["removed"]:
        assert peak["reasons"] and peak["narrative"]
        assert peak["nearest_legal_mz"] is not None
        assert abs(peak["nearest_legal_mDa"]) > 0.0
        assert "m/z" in peak["narrative"]


async def test_the_clean_peak_list_is_opt_in_and_the_summary_says_what_it_would_cost():
    async with in_process() as s:
        brief = as_json(await s.call_tool("clean_spectrum", {"peaks": PEAKS, "mode": "pos"}))
        full = as_json(await s.call_tool("clean_spectrum",
                                         {"peaks": PEAKS, "mode": "pos", "include_kept": True}))
    assert brief["kept"] is None
    assert "include_kept=true" in brief["kept_omitted"]
    assert str(brief["summary"]["kept"]) in brief["kept_omitted"]
    assert full["kept_omitted"] is None
    assert [p["mz"] for p in full["kept"]] == POSSIBLE
    for peak in full["kept"]:
        assert peak["intensity"] > 0 and peak["model"] is None and peak["formula"] is None


# Both masses below were found by scanning masses with this build: 150.8765 is reachable only by an
# ion carrying F/Cl/Br/I, and 120.1188 is reachable only as a labelled version of something legal.
HALOGEN_ONLY = 150.8765
LABELLED_ONLY = 120.1188


async def test_a_peak_that_needs_a_halogen_or_a_label_says_which():
    async with in_process() as s:
        halogen = as_json(await s.call_tool("clean_spectrum", {"peaks": [[HALOGEN_ONLY, 10.0]],
                                                               "mode": "pos"}))
        with_halogen = as_json(await s.call_tool("clean_spectrum",
                                                 {"peaks": [[HALOGEN_ONLY, 10.0]], "mode": "pos",
                                                  "halogens": True}))
        labelled = as_json(await s.call_tool("clean_spectrum", {"peaks": [[LABELLED_ONLY, 10.0]],
                                                                "mode": "pos"}))
        with_label = as_json(await s.call_tool("clean_spectrum",
                                               {"peaks": [[LABELLED_ONLY, 10.0]], "mode": "pos",
                                                "carbon13": 1}))
    assert "needs-halogen-alphabet" in halogen["removed"][0]["reasons"]
    assert "halogen" in halogen["removed"][0]["narrative"].lower()
    assert with_halogen["summary"]["kept"] == 1 and with_halogen["removed"] == []
    assert "consistent-with-label-carbon13" in labelled["removed"][0]["reasons"]
    assert with_label["summary"]["kept"] == 1


async def test_the_precursor_survives_whatever_its_own_verdict():
    async with in_process() as s:
        out = as_json(await s.call_tool("clean_spectrum",
                                        {"peaks": [[100.5, 20.0]], "mode": "pos",
                                         "precursor_mz": 100.5, "include_kept": True}))
    assert out["summary"]["precursor_mz"] == 100.5
    assert out["summary"]["removed"] == 0 and out["summary"]["kept"] == 1
    assert [p["mz"] for p in out["kept"]] == [100.5]
    assert out["removed"] == []


async def test_explanations_off_still_says_what_went_but_not_why():
    async with in_process() as s:
        out = as_json(await s.call_tool("clean_spectrum", {"peaks": PEAKS, "mode": "pos",
                                                           "explanations": False}))
    assert out["summary"]["removed"] == 2 and [p["mz"] for p in out["removed"]] == IMPOSSIBLE
    for peak in out["removed"]:
        assert peak["reasons"] == [] and peak["narrative"] == ""


async def test_nearest_only_reports_the_distance_instead_of_every_reason():
    async with in_process() as s:
        full = as_json(await s.call_tool("clean_spectrum", {"peaks": PEAKS, "mode": "pos"}))
        brief = as_json(await s.call_tool("clean_spectrum", {"peaks": PEAKS, "mode": "pos",
                                                             "nearest_only": True}))
    assert len(brief["removed"][0]["reasons"]) < len(full["removed"][0]["reasons"])
    assert brief["removed"][0]["nearest_legal_mz"] == full["removed"][0]["nearest_legal_mz"]
    assert brief["summary"]["kept"] == full["summary"]["kept"]


async def test_expensive_extras_are_opt_in_and_answer_when_asked():
    async with in_process() as s:
        out = as_json(await s.call_tool("clean_spectrum",
                                        {"peaks": PEAKS, "mode": "pos", "include_kept": True,
                                         "formulas": True, "model_flags": True}))
    kept = out["kept"]
    assert all(p["formula"] for p in kept), "a kept peak under the cap should carry its witness"
    assert all(p["model"] in ("both-models", "strict-only", "lenient-only") for p in kept)
    assert out["summary"]["formulas_computed"] == len(kept)
    assert out["summary"]["formulas_skipped"] == 0


async def test_a_formula_cap_shorter_than_the_spectrum_answers_what_it_afforded_and_says_so():
    # A witness costs ~7.9 ms against a 21 us verdict, so the cap is a latency limit, not a
    # correctness one: the answer is partial, and the summary is what makes that visible.
    async with in_process(limits=Limits(max_formula_ions=1, max_model_ions=1)) as s:
        out = as_json(await s.call_tool("clean_spectrum",
                                        {"peaks": PEAKS, "mode": "pos", "include_kept": True,
                                         "formulas": True, "model_flags": True}))
    assert out["summary"]["kept"] == 2
    assert out["summary"]["formulas_computed"] == 1
    assert out["summary"]["formulas_skipped"] == 1
    assert sum(1 for p in out["kept"] if p["formula"]) == 1
    assert sum(1 for p in out["kept"] if p["model"]) == 1


async def test_an_impossible_peak_above_the_table_ceiling_is_never_reported_impossible():
    # Past the ceiling the tables answer possible for every mass, so the honest answer is "kept".
    async with in_process() as s:
        out = as_json(await s.call_tool("clean_spectrum", {"peaks": [[5000.0, 10.0]],
                                                           "mode": "pos"}))
    assert out["summary"]["kept"] == 1 and out["removed"] == []


# ---------------------------------------------------------------- refusals


async def test_a_request_over_max_peaks_is_refused_with_the_number_named():
    big = [[100.0 + 0.001 * i, 1.0] for i in range(Limits().max_peaks + 1)]
    async with in_process() as s:
        text = error_text(await s.call_tool("clean_spectrum", {"peaks": big, "mode": "pos"}))
    assert "too many peaks" in text and str(Limits().max_peaks) in text


async def test_explanation_work_beyond_the_budget_is_refused_with_the_cheaper_options_named():
    peaks = [[100.5 + 0.0013 * i, 1.0] for i in range(40)]
    async with in_process(limits=Limits(max_explanation_work_us=1000)) as s:
        text = error_text(await s.call_tool("clean_spectrum", {"peaks": peaks, "mode": "pos"}))
    assert "nearest_only=true" in text and "explanations=false" in text
    # The refusal is about the explanations, not the spectrum: the same peaks clean fine without them.
    async with in_process(limits=Limits(max_explanation_work_us=1000)) as s:
        out = as_json(await s.call_tool("clean_spectrum", {"peaks": peaks, "mode": "pos",
                                                          "nearest_only": True}))
    assert out["summary"]["total"] == 40


async def test_bad_chemistry_input_is_refused_not_guessed():
    async with in_process() as s:
        for args in ({"peaks": [], "mode": "pos"},
                    {"peaks": [[100.0, 1.0]], "mode": "sideways"},
                    {"peaks": [[0.0, 1.0]], "mode": "pos"},
                    {"peaks": [[-5.0, 1.0]], "mode": "pos"}):
            text = error_text(await s.call_tool("clean_spectrum", args))
            assert text, args
    async with in_process() as s:
        nan = error_text(await s.call_tool("clean_spectrum",
                                           {"peaks": [[100.0, float("nan")]], "mode": "pos"}))
    assert "intensity" in nan or "not finite" in nan or "number" in nan


async def test_a_peak_list_beyond_the_result_cap_is_reported_not_silently_cut():
    peaks = [[100.5 + 0.001 * i, 1.0] for i in range(6)]
    async with in_process(limits=Limits(max_result_peaks=3)) as s:
        out = as_json(await s.call_tool("clean_spectrum", {"peaks": peaks, "mode": "pos"}))
    assert len(out["removed"]) <= 3
    assert out["summary"]["removed"] == 6, "the count must still be the truth"
    assert "max_result_peaks=3" in out["removed_truncated"]
    assert "nearest_only=true" in out["removed_truncated"]


# ---------------------------------------------------------------- clean_batch


async def test_a_batch_reports_each_spectrum_in_order():
    # The same peaks in both polarities: 123.9 is impossible as [M+H]+ and legal as [M-H]-, so this
    # also proves the batch applies each spectrum's own mode rather than the first one it saw.
    async with in_process() as s:
        out = as_json(await s.call_tool("clean_batch",
                                        {"spectra": [{"mode": "pos", "peaks": PEAKS},
                                                     {"mode": "neg", "peaks": PEAKS}]}))
    summary = out["summary"]
    assert summary["spectra"] == 2 and summary["total"] == 8
    assert summary["kept"] == 5 and summary["removed"] == 3
    first, second = out["per_spectrum"]
    assert (first["mode"], first["kept"], first["removed"]) == ("pos", 2, 2)
    assert (second["mode"], second["kept"], second["removed"]) == ("neg", 3, 1)
    assert first["kept_peaks"] is None, "kept peaks are opt-in here too"
    assert summary["intensity_removed_fraction"] == pytest.approx(45.0 / 330.0)


async def test_a_streamed_batch_says_which_spectrum_is_being_cleaned_right_now():
    seen = []

    async def on_progress(progress, total, message):
        seen.append((progress, total, message))

    async with in_process() as s:
        out = as_json(await s.call_tool(
            "clean_batch",
            {"spectra": [{"mode": "pos", "peaks": PEAKS}] * 3, "explanations": True},
            progress_callback=on_progress))
    spectra_lines = [m for _p, _t, m in seen if m.startswith("SPECTRUM")]
    assert [m.split()[1] for m in spectra_lines] == ["index=1", "index=2", "index=3"]
    assert all(" total=3 " in m for m in spectra_lines)
    assert "kept=2 removed=2" in spectra_lines[0]
    assert any(m.startswith("REMOVED ") for m in [m for _p, _t, m in seen]), \
        "explanations=true should push per-peak lines too"
    assert out["summary"]["spectra"] == 3


async def test_one_line_per_spectrum_when_peak_detail_is_off():
    seen = []

    async def on_progress(progress, total, message):
        seen.append(message)

    async with in_process() as s:
        await s.call_tool("clean_batch", {"spectra": [{"mode": "pos", "peaks": PEAKS}] * 3,
                                          "explanations": False}, progress_callback=on_progress)
    assert len(seen) == 3, seen
    assert all(m.startswith("SPECTRUM") for m in seen)
    assert seen[0].startswith("SPECTRUM index=1")


async def test_a_min_progress_interval_thins_peak_lines_but_never_spectrum_lines():
    # A big spectrum of peaks that are all kept: without an interval this is hundreds of lines, and
    # a client that cannot keep up would be throttled by the socket instead of by the option.
    peaks = [[mz, 1.0] for mz in (181.0707, 85.0284)] * 60
    seen = []

    async def on_progress(progress, total, message):
        seen.append(message)

    async with in_process() as s:
        await s.call_tool("clean_batch",
                          {"spectra": [{"mode": "pos", "peaks": peaks}] * 2, "explanations": True,
                           "min_progress_interval_ms": 250}, progress_callback=on_progress)
    spectra = [m for m in seen if m.startswith("SPECTRUM")]
    assert len(spectra) == 2 and "index=2 total=2" in spectra[-1]
    assert len(seen) < 240, "the interval did not thin: %d lines" % len(seen)
    assert len([m for m in seen if m.startswith("KEPT")]) < 240 - 2


async def test_a_batch_that_stops_part_way_says_how_far_it_got():
    async with in_process() as s:
        text = error_text(await s.call_tool(
            "clean_batch",
            {"spectra": [{"mode": "pos", "peaks": [[181.0707, 100.0]]},
                         {"mode": "pos", "peaks": [[100.5, 1.0]], "deuterium": 13}]}))
    assert "spectrum 2 of 2" in text and "1 already cleaned" in text


async def test_a_batch_bigger_than_max_batch_is_refused_before_any_cleaning():
    spectra = [{"mode": "pos", "peaks": [[181.0707, 100.0]]}] * 3
    async with in_process(limits=Limits(max_batch=2)) as s:
        text = error_text(await s.call_tool("clean_batch", {"spectra": spectra}))
    assert "too many spectra" in text and "already cleaned" not in text


async def test_an_empty_batch_is_refused_rather_than_answering_with_nothing():
    async with in_process() as s:
        text = error_text(await s.call_tool("clean_batch", {"spectra": []}))
    assert "non-empty" in text


# ---------------------------------------------------------------- explain_ion


async def test_explain_ion_answers_one_mz_with_the_same_reasons_a_clean_gives():
    async with in_process() as s:
        bad = as_json(await s.call_tool("explain_ion", {"mz": 100.5, "mode": "pos"}))
        good = as_json(await s.call_tool("explain_ion", {"mz": 181.0707, "mode": "pos"}))
    assert bad["possible"] is False and bad["reasons"] and bad["narrative"]
    assert bad["nearest_legal_mz"] < 100.5 and bad["nearest_legal_mDa"] < 0.0
    assert good["possible"] is True and good["reasons"] == [] and good["narrative"]
    assert good["model"] is None, "naming the model is opt-in"


async def test_explain_ion_names_the_model_only_when_asked():
    async with in_process() as s:
        plain = as_json(await s.call_tool("explain_ion", {"mz": 181.0707, "mode": "pos"}))
        flagged = as_json(await s.call_tool("explain_ion", {"mz": 181.0707, "mode": "pos",
                                                            "model_flags": True}))
    assert plain["model"] is None
    assert flagged["model"] in ("both-models", "strict-only", "lenient-only")


async def test_explain_ion_nearest_only_still_gives_the_distance():
    async with in_process() as s:
        out = as_json(await s.call_tool("explain_ion", {"mz": 100.5, "mode": "pos",
                                                        "nearest_only": True}))
    assert out["nearest_legal_mz"] is not None
    assert out["reasons"] == ["nearest-legal-below"] or "nearest-legal" in out["reasons"][0]


async def test_explain_ion_refuses_input_it_cannot_answer():
    async with in_process() as s:
        assert "mode" in error_text(await s.call_tool("explain_ion", {"mz": 100.0,
                                                                      "mode": "neutral"}))
        assert "mz" in error_text(await s.call_tool("explain_ion", {"mz": -1.0, "mode": "pos"}))


# ---------------------------------------------------------------- service_info


async def test_service_info_states_the_fingerprint_ceiling_and_every_limit():
    async with in_process() as s:
        info = as_json(await s.call_tool("service_info", {}))
    assert info["fingerprint"] and len(info["fingerprint"]) == 16
    assert info["table_max_mz"] == 1700.0
    assert set(info["tables"]) == {"chnops", "halogen"} and all(info["tables"].values())
    assert info["cache_entries"] >= 0
    assert set(info["limits"]) >= {"max_peaks", "max_batch", "max_formula_ions", "max_model_ions",
                                   "max_result_peaks", "max_explanation_work_us"}
    assert info["limits"]["max_peaks"] == Limits().max_peaks
    assert set(info["tools"]) == {"clean_spectrum", "clean_batch", "explain_ion", "service_info"}


async def test_service_info_reports_the_limits_this_server_was_built_with():
    async with in_process(limits=Limits(max_peaks=123)) as s:
        info = as_json(await s.call_tool("service_info", {}))
    assert info["limits"]["max_peaks"] == 123


# ---------------------------------------------------------------- hosting helpers


def test_dns_rebinding_protection_is_only_on_when_a_host_was_named():
    assert security_for(None) is None and security_for([]) is None
    named = security_for(["denoise.mcp.metabolomics.us", "127.0.0.1:8301"])
    assert named.enable_dns_rebinding_protection is True
    assert named.allowed_hosts == ["denoise.mcp.metabolomics.us", "127.0.0.1:8301"]


def test_the_server_builds_without_serving_and_reports_its_name():
    server = build_server()
    assert server.name == "impossible-mass-denoise"
    text = (server.instructions or "").lower()
    assert "impossible" in text and "include_kept" in text
