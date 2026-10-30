"""Tests for the classifier: verdicts must equal Ziyue's denoise_spectrum survivors."""
import random

import pytest

import denoise
from denoise_service.classify import Classification, Options, classify, fingerprint

# Confirmed with denoise.is_possible(mz, "neg"): H2PO4- is possible; 100.070 is the first
# impossible mass found by scanning 100-150 in 0.001 steps.
POSSIBLE_NEG = 96.96910
IMPOSSIBLE_NEG = 100.070


def _spectrum(n=200, seed=7):
    rng = random.Random(seed)
    return [(round(rng.uniform(50, 600), 4), round(rng.uniform(1, 1000), 2)) for _ in range(n)]


def _survivors(peaks, opts, **kw):
    triples = [(mz, i, k) for k, (mz, i) in enumerate(peaks)]
    return {t[2] for t in denoise.denoise_spectrum(triples, opts.mode, **kw)}


def test_tables_are_loaded():
    assert denoise.table_status().startswith("loaded")


def test_known_ion_verdicts():
    result = classify([(POSSIBLE_NEG, 10.0), (IMPOSSIBLE_NEG, 10.0)], Options(mode="neg"))
    assert result.possible == [True, False]


def test_verdicts_follow_input_order_with_duplicates_and_unsorted():
    peaks = [(IMPOSSIBLE_NEG, 1.0), (POSSIBLE_NEG, 2.0), (IMPOSSIBLE_NEG, 3.0), (POSSIBLE_NEG, 4.0)]
    assert classify(peaks, Options(mode="neg")).possible == [False, True, False, True]


def _cases():
    peaks = _spectrum()
    base = Options(mode="neg")
    impossible = [p for k, p in enumerate(peaks) if k not in _survivors(peaks, base)]
    prec = impossible[0][0]
    return peaks, [
        (base, {}),
        (Options(mode="neg", halogens=True), {"halogens": True}),
        (Options(mode="neg", precursor_mz=prec), {"precursor_mz": prec}),
        (Options(mode="neg", deuterium=9), {"deuterium": 9}),
        (Options(mode="neg", carbon13=6, nitrogen15=2), {"carbon13": 6, "nitrogen15": 2}),
        (Options(mode="pos"), {}),
    ]


def test_matches_denoise_spectrum_survivors():
    peaks, cases = _cases()
    prec_case = cases[2]
    # the precursor option must matter, else this case proves nothing
    assert _survivors(peaks, prec_case[0], **prec_case[1]) != _survivors(peaks, cases[0][0])
    for opts, kw in cases:
        got = classify(peaks, opts).possible
        assert {k for k, v in enumerate(got) if v} == _survivors(peaks, opts, **kw)


def test_summary_numbers():
    peaks = [(POSSIBLE_NEG, 30.0), (IMPOSSIBLE_NEG, 10.0), (POSSIBLE_NEG, 60.0)]
    s = classify(peaks, Options(mode="neg")).summary
    assert s["total"] == 3
    assert set(s) == {"total", "impossible", "intensity_total", "intensity_impossible",
                      "intensity_impossible_fraction"}
    assert s["impossible"] == 1
    assert s["intensity_total"] == pytest.approx(100.0)
    assert s["intensity_impossible"] == pytest.approx(10.0)
    assert s["intensity_impossible_fraction"] == pytest.approx(0.1)


def test_zero_total_intensity_fraction_is_zero():
    s = classify([(IMPOSSIBLE_NEG, 0.0)], Options(mode="neg")).summary
    assert s["intensity_impossible_fraction"] == 0.0


def test_empty_peak_list_gives_empty_result():
    r = classify([], Options(mode="neg"))
    assert isinstance(r, Classification)
    assert r.possible == []
    assert r.summary["total"] == 0
    assert r.summary["intensity_impossible_fraction"] == 0.0


def test_invalid_mode_raises_value_error():
    with pytest.raises(ValueError):
        classify([(100.0, 1.0)], Options(mode="negative"))


def test_mz_above_table_ceiling_is_possible():
    assert classify([(1800.123, 5.0)], Options(mode="neg")).possible == [True]


def test_fingerprint():
    assert fingerprint() == "5aae4ec26694da4d"
