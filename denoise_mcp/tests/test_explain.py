"""Unit tests for the explanation layer: every reason the filter's boolean hides.

Cases are taken from the public MassBank fixture (`tests/massbank_aces_su_256.json`, cleaned in
positive mode), so the numbers are real spectrum peaks rather than invented ones:

  59.01379   possible, kept
  59.96718   removed, 8.8 mDa below a legal mass, and possible as z=2
  98.92952   removed under CHNOPS, possible once halogens are allowed
  214.24738  removed, sits a whole label step above a legal mass
  60.51270   removed with no legal mass within 400 mDa at all
  68.33223   removed, the closest legal mass is 270 mDa below it
"""
from __future__ import annotations

import pytest

import denoise
from denoise_mcp import explain

POS = "pos"

KEPT = 59.01379
DOUBLY = 59.96718
HALOGEN = 98.92952
LABELLED = 214.24738
EMPTY = 60.5127
BELOW = 68.33223


# ---------------------------------------------------------------- the distance statement


def test_a_possible_ion_is_its_own_nearest_legal_mass():
    near, mda = explain.distance_to_nearest(181.0707, POS)
    assert near == pytest.approx(181.0707, abs=0.0011)   # a real composition mass, not a bin centre
    assert abs(mda) < 1.0
    assert explain.removal_reasons(181.0707, POS) == []


def test_the_nearest_mass_is_a_mass_the_model_itself_accepts():
    """If the distance statement names a mass, that mass must be possible - otherwise the
    explanation points at something the filter would also refuse."""
    for mz in (DOUBLY, HALOGEN, LABELLED, BELOW):
        near = explain.nearest_legal(mz, POS)
        assert near is not None
        assert denoise.is_possible(near, POS), (mz, near)


def test_distance_is_signed_and_says_which_side_the_legal_mass_is_on():
    above = explain.distance_to_nearest(DOUBLY, POS)
    below = explain.distance_to_nearest(BELOW, POS)
    assert above[1] > 0.0
    assert below[1] < 0.0
    assert explain.removal_reasons(DOUBLY, POS)[0] == "nearest-legal-above"
    assert explain.removal_reasons(BELOW, POS)[0] == "nearest-legal-below"


def test_a_window_that_is_too_short_reports_nothing_rather_than_the_further_away_mass():
    assert explain.nearest_legal(BELOW, POS, window_mda=0.0) is None
    assert explain.removal_reasons(BELOW, POS, window_mda=0.0) == ["no-legal-ion-nearby"]
    # and the same ion, looked for properly, has a legal mass 270 mDa away
    near, mda = explain.distance_to_nearest(BELOW, POS)
    assert near == pytest.approx(68.33223 - 0.27018, abs=0.002)


def test_no_legal_ion_nearby_is_stated_as_such():
    assert explain.removal_reasons(EMPTY, POS) == ["no-legal-ion-nearby"]
    assert explain.nearest_legal(EMPTY, POS) is None


def test_the_band_reported_is_the_tightest_one_that_holds():
    codes = explain.removal_reasons(DOUBLY, POS)          # 8.8 mDa away
    assert "legal-within-10mDa" in codes
    assert not [c for c in codes if c in ("legal-within-5mDa",)]
    # 55.6 mDa away reports the 100 mDa band and nothing tighter or looser
    far = explain.removal_reasons(132.86787, POS)
    assert "legal-within-100mDa" in far
    assert "legal-within-30mDa" not in far
    assert "legal-within-300mDa" not in far


# ---------------------------------------------------------------- the chemistry questions


def test_a_peak_that_needs_the_halogen_alphabet_says_so_and_the_answer_changes_when_asked():
    assert not denoise.is_possible(HALOGEN, POS)
    assert denoise.is_possible(HALOGEN, POS, True)
    reasons = explain.removal_reasons(HALOGEN, POS)
    assert "needs-halogen-alphabet" in reasons
    # with halogens allowed the ion is possible, so there is nothing left to explain
    assert explain.removal_reasons(HALOGEN, POS, halogens=True) == []


def test_a_doubly_charged_ion_is_named_as_such():
    assert "possible-as-doubly-charged" in explain.removal_reasons(DOUBLY, POS)


def test_a_whole_label_step_above_a_legal_mass_is_named_with_its_count():
    codes = explain.removal_reasons(LABELLED, POS)
    assert "consistent-with-label-deuterium" in codes
    assert "consistent-with-label-carbon13" in codes
    assert explain.label_count(LABELLED, POS, "deuterium") >= 1
    assert explain.label_count(EMPTY, POS, "deuterium") == 0
    # the count the sentence names is the smallest label mix that makes the ion possible
    k = explain.label_count(LABELLED, POS, "carbon13")
    assert denoise.is_possible(LABELLED, POS, carbon13=k)


def test_a_possible_ion_has_nothing_to_explain_in_any_option_combination():
    for kwargs in ({}, {"halogens": True}, {"nearest_only": True}):
        assert explain.removal_reasons(KEPT, POS, **kwargs) == []


def test_nearest_only_gives_the_distance_and_no_chemistry():
    codes = explain.removal_reasons(HALOGEN, POS, nearest_only=True)
    assert codes == ["nearest-legal-above"]
    assert "needs-halogen-alphabet" not in explain.removal_reasons(HALOGEN, POS, nearest_only=True)


# ---------------------------------------------------------------- the validity model


def test_the_model_that_kept_a_peak_is_reported_when_asked():
    flag = explain.model_flag(KEPT, POS)
    assert flag in explain.KEPT_MODELS
    # a removed ion is refused by both models, which is what "impossible" means here
    assert explain.model_flag(DOUBLY, POS) is None


def test_the_two_published_models_disagree_on_some_peaks():
    """The reason the flag exists: some peaks one model keeps and the other refuses."""
    flags = {explain.model_flag(mz, POS) for mz in (59.01379, 61.98832, 67.01904, 181.0707,
                                                    118.0863, 89.0239, 174.1125)}
    assert flags & {"both-models"}
    assert flags & {"lenient-only", "strict-only"}


# ---------------------------------------------------------------- narration and the codebook


def test_narration_is_a_sentence_built_from_the_codes():
    codes = explain.removal_reasons(DOUBLY, POS)
    line = explain.narrate(DOUBLY, POS, codes)
    assert line.startswith("m/z 59.9672 (positive polarity)")
    assert "closest allowed mass is" in line
    assert "doubly charged" in line
    assert line.endswith(".")


def test_narration_says_when_there_is_nothing_to_explain():
    assert "is possible" in explain.narrate(KEPT, POS, [])


def test_every_reason_code_the_filter_can_emit_has_a_documented_meaning():
    emitted = set()
    for mz in (DOUBLY, HALOGEN, LABELLED, EMPTY, BELOW, 132.86787, 78.06116, 53.85321):
        emitted.update(explain.removal_reasons(mz, POS))
        emitted.update(explain.removal_reasons(mz, "neg"))
    assert emitted, "the fixture should produce at least one removal"
    unknown = emitted - set(explain.REASON_CODES)
    assert not unknown, "undocumented reason codes: %s" % sorted(unknown)
    for code in emitted:
        assert explain.REASON_MEANING[code]


def test_the_codebook_covers_the_codes_it_claims_and_names_no_invented_ones():
    assert set(explain.REASON_CODES) == set(explain.REASON_MEANING)
    # a reason the filter cannot produce must not be advertised
    assert "peak-below-detection-limit" not in explain.REASON_CODES
    assert "above-table-ceiling" not in explain.REASON_CODES


def test_strong_codes_are_the_chemical_ones_not_the_distance():
    assert "needs-halogen-alphabet" in explain.STRONG_CODES
    assert "possible-as-doubly-charged" in explain.STRONG_CODES
    for distance_only in ("nearest-legal-above", "nearest-legal-below", "no-legal-ion-nearby"):
        assert distance_only not in explain.STRONG_CODES


def test_above_the_table_ceiling_the_model_says_possible_not_impossible():
    """No removal reason may be needed above the ceiling: the tables answer possible there."""
    assert explain.TABLE_MAX_MZ == explain.table_max_mz() == explain.table_max_mz(halogens=True)
    for mz in (explain.TABLE_MAX_MZ + 1.0, explain.TABLE_MAX_MZ + 250.0):
        assert denoise.is_possible(mz, POS)
        assert explain.removal_reasons(mz, POS) == []
        assert explain.nearest_legal(mz, POS) == mz


def test_the_deployed_tolerance_is_the_one_the_filter_uses():
    """Distances are measured with the same tolerance the verdict was computed with."""
    assert explain.DEPLOYED_TOL == 0.005
    # anything closer than that would have been kept, so no removal may report a smaller distance
    for mz in (DOUBLY, HALOGEN, LABELLED, BELOW, 132.86787):
        _near, mda = explain.distance_to_nearest(mz, POS)
        assert abs(mda) > explain.DEPLOYED_TOL * 1000.0


# ---------------------------------------------------------------- cost accounting


def test_the_work_estimate_grows_with_the_options_that_cost():
    plain = explain.explanation_cost_us(0, 0, 0)
    labelled = explain.explanation_cost_us(9, 0, 0)
    cheap = explain.explanation_cost_us(0, 0, 0, nearest_only=True)
    assert plain > cheap
    assert labelled > plain


def test_the_estimate_matches_a_measured_explanation_to_within_an_order_of_magnitude():
    """An estimate used to refuse requests has to be roughly the truth, not a made-up number."""
    import time
    start = time.perf_counter()
    n = 200
    for step in range(n):
        explain.removal_reasons(HALOGEN + step * 0.0001, POS)
    measured_us = (time.perf_counter() - start) * 1e6 / n
    assert 0.1 * explain.WORK_US["verdict"] <= measured_us <= 10 * explain.explanation_cost_us(0, 0, 0)
