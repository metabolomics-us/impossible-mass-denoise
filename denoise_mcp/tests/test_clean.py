"""Unit tests for the cleaning layer: verdicts, explanations, payload shape and the caps.

Peaks are real ones from `tests/massbank_aces_su_256.json` (positive mode). Known facts used below:
59.01379 is possible; 59.96718, 98.92952, 60.5127 and 214.24738 are removed; 98.92952 is possible
with halogens; 214.24738 is possible when a 13C label is declared.
"""
from __future__ import annotations

import json
import re

import pytest

import denoise
from denoise_service.classify import Options, classify
from denoise_mcp import clean
from denoise_mcp.limits import LimitError, Limits

POS = "pos"
PEAKS = [(59.01379, 854985.0), (59.96718, 769600.0), (98.92952, 7968.0),
         (60.5127, 279474.0), (214.24738, 48274.0)]
REMOVED = {59.96718, 98.92952, 60.5127, 214.24738}


def test_verdicts_are_the_rest_services_own():
    """Same input through `denoise_service.classify` must decide the same partition."""
    expected = classify(PEAKS, Options(mode=POS))
    res = clean.clean_spectrum(PEAKS, POS)
    assert [p.mz for p in res.kept] == [mz for (mz, _ri), ok in zip(PEAKS, expected.possible) if ok]
    assert {r.mz for r in res.removed} == {mz for (mz, _ri), ok in zip(PEAKS, expected.possible)
                                           if not ok}


def test_the_partition_and_the_input_order_survive_and_intensity_is_never_rescaled():
    res = clean.clean_spectrum(PEAKS, POS)
    assert [p.mz for p in res.kept] == [59.01379]
    assert [r.mz for r in res.removed] == [59.96718, 98.92952, 60.5127, 214.24738]  # input order
    assert {r.mz for r in res.removed} == REMOVED
    intensities = {p.mz: p.intensity for p in res.kept} | {r.mz: r.intensity for r in res.removed}
    assert intensities == {mz: ri for mz, ri in PEAKS}


def test_summary_reports_the_numbers_a_reader_needs_first():
    res = clean.clean_spectrum(PEAKS, POS)
    s = clean.summary(res)
    assert s["total"] == len(PEAKS)
    assert s["kept"] == 1 and s["removed"] == len(REMOVED)
    assert s["mode"] == POS and s["halogens"] is False
    assert s["labels"] == {"deuterium": 0, "carbon13": 0, "nitrogen15": 0}
    assert s["fingerprint"] == denoise.fingerprint()
    assert s["intensity_in"] == sum(ri for _mz, ri in PEAKS)
    assert s["intensity_removed"] == pytest.approx(sum(ri for mz, ri in PEAKS if mz in REMOVED))
    assert 0.0 < s["intensity_removed_fraction"] < 1.0
    assert s["removed_above_table"] == 0
    assert s["formulas_computed"] == 0 and s["formulas_skipped"] == 0


def test_the_summary_alone_is_small_whatever_the_spectrum():
    """A summary is the answer that always fits; it must not grow with the peak count."""
    small = clean.summary(clean.clean_spectrum(PEAKS, POS))
    big = clean.summary(clean.clean_spectrum(PEAKS * 200, POS))
    assert set(small) == set(big)
    assert big["total"] == 1000 and big["kept"] == 200
    assert len(json.dumps(big)) < 1024


def test_every_removal_carries_a_reason_a_distance_and_a_sentence():
    res = clean.clean_spectrum(PEAKS, POS)
    for r in res.removed:
        assert r.reasons, r.mz
        assert r.narrative.endswith(".")
        assert "m/z %.4f" % r.mz in r.narrative
    by_mz = {r.mz: r for r in res.removed}
    assert "possible-as-doubly-charged" in by_mz[59.96718].reasons
    assert "needs-halogen-alphabet" in by_mz[98.92952].reasons
    assert by_mz[60.5127].reasons == ["no-legal-ion-nearby"]
    assert by_mz[60.5127].nearest_legal_mz is None
    assert by_mz[98.92952].nearest_legal_mz > 98.92952
    assert by_mz[98.92952].nearest_legal_mDa > 0


def test_kept_peaks_have_no_reasons_and_no_formula_unless_asked():
    res = clean.clean_spectrum(PEAKS, POS)
    assert res.kept == [clean.Peak(59.01379, 854985.0, None, None)]


def test_explanations_off_keeps_the_verdicts_and_drops_the_why():
    res = clean.clean_spectrum(PEAKS, POS, explanations=False)
    assert {r.mz for r in res.removed} == REMOVED
    assert all(r.reasons == [] and r.narrative == "" for r in res.removed)
    assert all(p.formula is None and p.model is None for p in res.kept)
    assert clean.summary(res)["removed"] == len(REMOVED)


def test_nearest_only_reports_the_distance_and_skips_the_chemistry():
    res = clean.clean_spectrum(PEAKS, POS, nearest_only=True)
    by_mz = {r.mz: r for r in res.removed}
    assert by_mz[98.92952].reasons == ["nearest-legal-above"]
    assert by_mz[60.5127].reasons == ["no-legal-ion-nearby"]


def test_the_halogen_option_changes_the_chemistry_not_just_the_explanation():
    plain = clean.clean_spectrum(PEAKS, POS)
    with_hal = clean.clean_spectrum(PEAKS, POS, halogens=True)
    assert 98.92952 in {r.mz for r in plain.removed}
    assert 98.92952 in {p.mz for p in with_hal.kept}
    assert clean.summary(with_hal)["halogens"] is True


def test_declaring_a_label_keeps_the_peak_that_sits_one_label_step_above_a_legal_mass():
    assert 214.24738 in {r.mz for r in clean.clean_spectrum(PEAKS, POS).removed}
    kept = clean.clean_spectrum(PEAKS, POS, carbon13=1)
    assert 214.24738 in {p.mz for p in kept.kept}
    assert clean.summary(kept)["labels"]["carbon13"] == 1


def test_the_precursor_is_kept_whatever_the_filter_thinks_of_it():
    res = clean.clean_spectrum(PEAKS, POS, precursor_mz=59.96718)
    assert 59.96718 in {p.mz for p in res.kept}
    assert clean.summary(res)["precursor_mz"] == 59.96718


def test_formula_witnesses_are_opt_in_capped_and_reported():
    peaks = [(59.01379, 854985.0), (61.98832, 132052.0), (67.01904, 187315.0),
             (89.0239, 5000.0), (118.0863, 4000.0)]
    res = clean.clean_spectrum(peaks, POS, formulas=True, max_formulas=2)
    s = clean.summary(res)
    assert s["formulas_computed"] == 2
    assert s["formulas_skipped"] == len(res.kept) - 2
    with_formula = [p for p in res.kept if p.formula]
    assert len(with_formula) == 2
    assert [p.mz for p in with_formula] == sorted(p.mz for p in with_formula)   # lightest first
    assert all(re.fullmatch(r"[CHNOPS][A-Z0-9]*", p.formula) for p in with_formula)


def test_a_formula_budget_larger_than_the_spectrum_reports_what_it_actually_covered():
    res = clean.clean_spectrum([(59.01379, 1.0)], POS, formulas=True, max_formulas=5)
    s = clean.summary(res)
    assert s["formulas_computed"] == 1 and s["formulas_skipped"] == 0


def test_model_flags_are_opt_in_and_name_the_model_that_kept_each_peak():
    res = clean.clean_spectrum(PEAKS, POS, model_flags=True, max_models=1)
    assert [p.model for p in res.kept] == [p.model for p in res.kept if p.model is not None]
    assert res.kept[0].model in clean.explain.KEPT_MODELS
    assert clean.clean_spectrum(PEAKS, POS).kept[0].model is None


def test_payload_holds_the_clean_list_back_unless_asked():
    res = clean.clean_spectrum(PEAKS, POS)
    small = clean.payload(res)
    assert "kept_omitted" in small and "kept" not in small
    assert "include_kept=true" in small["kept_omitted"]
    assert "include_kept=true would add 1 peak" in small["kept_omitted"]
    full = clean.payload(res, include_kept=True)
    assert [p["mz"] for p in full["kept"]] == [59.01379]
    assert "kept_omitted" not in full
    assert {r["mz"] for r in full["removed"]} == REMOVED
    assert full["removed"][0]["reasons"]


def test_the_payload_without_kept_peaks_is_smaller_than_the_payload_with_them():
    res = clean.clean_spectrum(PEAKS * 40, POS)
    assert (len(json.dumps(clean.payload(res)))
            < len(json.dumps(clean.payload(res, include_kept=True))))


# ------------------------------------------------------- the answer an agent can actually read

# Mostly noise: enough removals that the entry list alone is far bigger than any agent harness
# reads, and enough kept peaks that the opt-in list is too. Measured, not guessed: the spectrum
# below removes 2883 of 5000 peaks.
NOISY, NEG = [(200.0 + i * 0.0137, 5.0) for i in range(5000)], "neg"


def _bytes(answer):
    """Size as the server would measure it: compact JSON, the way JSON-RPC goes out."""
    return len(json.dumps(answer, separators=(",", ":")))


def _answer(res, **kw):
    kw.setdefault("max_removed", Limits().max_result_peaks)
    kw.setdefault("max_bytes", Limits().max_answer_bytes)
    return clean.payload(res, **kw)


def test_an_answer_fits_the_budget_an_agent_reads_without_losing_the_counts():
    res = clean.clean_spectrum(NOISY, NEG)
    answer = _answer(res)
    assert _bytes(answer) <= Limits().max_answer_bytes, _bytes(answer)
    # The counts stay the whole truth however the entries are cut.
    assert answer["summary"]["total"] == 5000
    assert answer["summary"]["removed"] == len(res.removed) > len(answer["removed"]) > 0
    assert answer["removed_truncated"], "an answer that was cut has to say it was"
    assert "max_answer_bytes" in answer["removed_truncated"]


def test_the_budget_drops_a_sentence_before_it_drops_an_ion():
    """The why is the reason codes plus a distance before it is the sentence: cut the sentence."""
    res = clean.clean_spectrum(NOISY, NEG)
    answer = _answer(res)
    sentences = [bool(r["narrative"]) for r in answer["removed"]]
    assert not all(sentences), "nothing was cut, so this proves nothing"
    assert sentences[:sum(sentences)] == [True] * sum(sentences), "narratives were cut from the middle"
    for r in answer["removed"]:
        assert r["reasons"], "an entry without reasons is not an explanation"
        assert r["nearest_legal_mz"] is not None
    assert "narrative" in answer["removed_truncated"].lower()
    # Asked for less chemistry, the same spectrum's answer needs no such cutting of codes.
    brief = _answer(clean.clean_spectrum(NOISY, NEG, nearest_only=True))
    assert _bytes(brief) <= Limits().max_answer_bytes


def test_an_opt_in_peak_list_is_cut_to_the_budget_and_says_which_part_arrived():
    res = clean.clean_spectrum(NOISY, NEG)
    answer = _answer(res, include_kept=True)
    assert _bytes(answer) <= Limits().max_answer_bytes, _bytes(answer)
    assert len(answer["kept"]) < len(res.kept)
    assert answer["kept"] == [{"mz": p.mz, "intensity": p.intensity, "formula": p.formula,
                               "model": p.model} for p in res.kept[:len(answer["kept"])]]
    assert answer["kept_truncated"] and "kept_omitted" not in answer
    assert answer["summary"]["kept"] == len(res.kept)
    # One budget, two lists the caller asked for: neither is allowed to starve the other.
    assert answer["removed"] and answer["kept"], (len(answer["removed"]), len(answer["kept"]))


def test_the_budget_never_touches_the_summary():
    res = clean.clean_spectrum(NOISY, NEG)
    assert _answer(res)["summary"] == clean.payload(res)["summary"]


def test_a_request_without_a_budget_still_gets_the_whole_answer():
    """`max_bytes=None` is what the pure layer did before a budget existed, and unit tests read it."""
    res = clean.clean_spectrum(NOISY, NEG)
    whole = clean.payload(res, include_kept=True, max_removed=None, max_bytes=None)
    assert len(whole["removed"]) == len(res.removed)
    assert len(whole["kept"]) == len(res.kept)
    assert whole["removed_truncated"] is None and "kept_truncated" not in whole
    assert _bytes(whole) > Limits().max_answer_bytes, "the budget would not have been needed"


def test_too_many_peaks_is_refused_with_the_number_that_broke():
    with pytest.raises(LimitError, match="too many peaks \\(3\\); maximum 2"):
        clean.clean_spectrum(PEAKS[:3], POS, limits=Limits(max_peaks=2))


@pytest.mark.parametrize("bad, message", [
    ([], "non-empty"),
    ([(0.0, 1.0)], "finite number > 0"),
    ([(-1.0, 1.0)], "finite number > 0"),
    ([(float("nan"), 1.0)], "finite number > 0"),
    ([(100.0, -1.0)], "finite number >= 0"),
    ([(100.0, float("inf"))], "finite number >= 0"),
    ([(100.0, None)], "each peak must be"),
    ([100.0], "each peak must be"),
    ("not peaks", "non-empty"),
])
def test_peak_shapes_the_endpoint_cannot_use_are_rejected(bad, message):
    with pytest.raises(LimitError, match=message):
        clean.clean_spectrum(bad, POS)


@pytest.mark.parametrize("mode", ["POS", "positive", "", None, 1])
def test_a_mode_the_filter_does_not_have_is_rejected_with_the_filters_own_words(mode):
    with pytest.raises(LimitError, match="pos"):
        clean.clean_spectrum(PEAKS, mode)


def test_label_and_precursor_arguments_are_checked_before_any_work():
    with pytest.raises(LimitError, match="deuterium"):
        clean.clean_spectrum(PEAKS, POS, deuterium=13)
    with pytest.raises(LimitError, match="lookups per ion"):
        clean.clean_spectrum(PEAKS, POS, deuterium=9, carbon13=9, nitrogen15=9)
    with pytest.raises(LimitError, match="halogens must be"):
        clean.clean_spectrum(PEAKS, POS, halogens="yes")
    with pytest.raises(LimitError, match="precursor_mz"):
        clean.clean_spectrum(PEAKS, POS, precursor_mz=-5)


def test_a_request_whose_explanations_would_run_long_is_refused_with_the_way_out():
    tight = Limits(max_explanation_work_us=250)
    with pytest.raises(LimitError, match="nearest_only=true"):
        clean.clean_spectrum(PEAKS, POS, limits=tight)
    # the same spectrum is fine once the expensive part is not asked for
    assert clean.clean_spectrum(PEAKS, POS, limits=tight, nearest_only=True).removed


def test_progress_lines_are_one_per_peak_and_machine_readable():
    seen = []
    res = clean.clean_spectrum(PEAKS, POS, on_explained=lambda kind, item: seen.append((kind, item)))
    assert [kind for kind, _ in seen].count("kept") == len(res.kept)
    assert [kind for kind, _ in seen].count("removed") == len(res.removed)
    lines = [clean.progress_line(kind, item) for kind, item in seen]
    assert all("\n" not in line for line in lines)
    kept_line = clean.progress_line("kept", res.kept[0])
    assert kept_line == "KEPT 59.0138 854985.0"
    removed_line = clean.progress_line("removed", res.removed[0])
    assert removed_line.startswith("REMOVED ") and "nearest-legal-" in removed_line
    assert (clean.progress_line("spectrum", clean.Progress(2, 5, 10, 3))
            == "SPECTRUM index=2 total=5 kept=10 removed=3")
    with pytest.raises(ValueError, match="streams as"):
        clean.progress_line("removed", res.kept[0])


def test_a_cleaning_run_is_deterministic():
    first = clean.clean_spectrum(PEAKS, POS, formulas=True, max_formulas=1)
    second = clean.clean_spectrum(PEAKS, POS, formulas=True, max_formulas=1)
    assert clean.payload(first) == clean.payload(second)


def test_a_peak_above_the_table_ceiling_is_never_removed():
    """Above the ceiling the model answers possible for every mass; a removal there is a fault."""
    peaks = [(2500.0, 100.0), (1700.5, 90.0), (59.01379, 1.0)]
    res = clean.clean_spectrum(peaks, POS)
    assert clean.summary(res)["removed_above_table"] == 0
    assert 2500.0 in {p.mz for p in res.kept}
