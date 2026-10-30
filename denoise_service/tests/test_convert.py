"""Format-preserving conversion core, tested with a stand-in verdict function (pure text logic)."""
from pathlib import Path

import pytest

from denoise_service import convert as cv

FIX = Path(__file__).resolve().parent / "fixtures"
GOOD = {181.0707, 85.0284}


def verdicts(peaks, mode, precursor):
    return [mz in GOOD for mz, _ in peaks]


def everything(peaks, mode, precursor):
    return [True] * len(peaks)


def run(text, fn=verdicts, **kw):
    kw.setdefault("default_mode", "pos")
    return cv.convert(text, fn, **kw)


def test_detect_format_msp_mgf_strings_columns():
    assert cv.detect_format((FIX / "sample.msp").read_text()) == "msp"
    assert cv.detect_format((FIX / "sample.mgf").read_text()) == "mgf"
    assert cv.detect_format((FIX / "sample.strings.txt").read_text()) == "strings"
    assert cv.detect_format("100.5:20 181.0707:100") == "strings"
    assert cv.detect_format("100.5:20\n181.0707:100\n") == "columns"
    assert cv.detect_format("100.5 20\n181.0707 100\n") == "columns"
    assert cv.detect_format("Num Peaks: 1\n100 5\n") == "msp"


def test_msp_roundtrip_identity_when_all_possible():
    text = (FIX / "sample.msp").read_text()
    for t in (text, text.replace("\n", "\r\n"), text.rstrip("\n")):
        assert run(t, everything).text == t


def test_msp_removes_impossible_and_rewrites_num_peaks_keeping_metadata():
    res = run((FIX / "sample.msp").read_text())
    assert res.format == "msp"
    assert res.text == ("Name: A\nIonMode: Positive\nPrecursorMZ: 200.5\nNum Peaks: 2\n"
                        "181.0707\t100\n85.0284\t40\n\n"
                        "Name: B\nIon_mode: negative\nNum Peaks: 2\n181.0707 100\n85.0284 40\n")
    assert [r["impossible"] for r in res.records] == [2, 0]
    assert [r["name"] for r in res.records] == ["A", "B"]
    assert res.summary == {"records": 2, "cleaned": 2, "passed_through": 0, "peaks": 6,
                           "impossible": 2}


def test_msp_crlf_keeps_line_endings():
    text = (FIX / "sample.msp").read_text().replace("\n", "\r\n")
    out = run(text).text
    assert "\r\nNum Peaks: 2\r\n181.0707\t100\r\n85.0284\t40\r\n\r\nName: B" in out
    assert "\n" not in out.replace("\r\n", "")


def test_msp_peaks_several_per_line_rebuilt_from_original_tokens():
    text = "Name: X\nNum Peaks: 4\n181.0707 100;  100.5 20; 85.0284 40 123.9 5\n"
    out = run(text).text
    assert out == "Name: X\nNum Peaks: 2\n181.0707 100; 85.0284 40\n"
    out = run("Name: X\nnum_peaks : 3\n  100.5:20,181.0707:1.50e2 ;85.0284:40  \n").text
    assert out == "Name: X\nnum_peaks : 2\n  181.0707:1.50e2 ;85.0284:40  \n"


def test_msp_all_impossible_gives_zero_peaks_and_keeps_headers():
    text = "Name: X\nIonMode: P\nNum Peaks: 2\n100.5 20\n123.9 5\n\nName: Y\nNum Peaks: 1\n85.0284 1\n"
    res = run(text)
    assert res.text == "Name: X\nIonMode: P\nNum Peaks: 0\n\nName: Y\nNum Peaks: 1\n85.0284 1\n"
    assert res.records[0]["error"] is None and res.records[0]["impossible"] == 2


def test_msp_mode_and_precursor_read_from_metadata_over_defaults():
    seen = []

    def spy(peaks, mode, precursor):
        seen.append((mode, precursor))
        return [True] * len(peaks)
    text = ("Name: A\nIonMode: negative\nPrecursor_MZ: 150.25\n100 1\n\n"
            "Name: B\nIonization: p\n100 1\n\n"
            "Name: C\n100 1\n")
    run(text, spy, default_mode="pos", default_precursor=999.0)
    assert seen == [("neg", 150.25), ("pos", 999.0), ("pos", 999.0)]


def test_msp_no_mode_anywhere_is_passed_through_with_error():
    text = "Name: A\nNum Peaks: 1\n100.5 20\n"
    res = run(text, default_mode=None)
    assert res.text == text
    assert res.records[0]["error"] == "no ion mode"
    assert res.summary["passed_through"] == 1 and res.summary["cleaned"] == 0


def test_mgf_cleaning_keeps_blocks_and_pepmass_charge():
    seen = []

    def spy(peaks, mode, precursor):
        seen.append((mode, precursor))
        return verdicts(peaks, mode, precursor)
    res = cv.convert((FIX / "sample.mgf").read_text(), spy, default_mode=None)
    assert res.format == "mgf"
    assert res.text == ("BEGIN IONS\nTITLE=one\nPEPMASS=300.1 1000\nCHARGE=1+\n181.0707 100\n"
                        "85.0284 40\nEND IONS\n\n"
                        "BEGIN IONS\nTITLE=two\nIONMODE=Negative\nCHARGE=1-\nEND IONS\n")
    assert seen == [("pos", 300.1), ("neg", None)]
    assert [r["name"] for r in res.records] == ["one", "two"]


def test_mgf_charge_sign_is_fallback_for_mode():
    text = "BEGIN IONS\nCHARGE=1-\n100.5 1\nEND IONS\n"
    seen = []
    cv.convert(text, lambda p, m, pr: seen.append(m) or [True] * len(p), default_mode="pos")
    assert seen == ["neg"]


def test_strings_one_line_per_spectrum_same_count_and_order():
    text = (FIX / "sample.strings.txt").read_text()
    res = run(text)
    assert res.format == "strings"
    assert res.text == "181.0707:100 85.0284:40\n\n85.0284:7 181.0707:3\n"
    assert res.text.count("\n") == text.count("\n")


def test_strings_all_removed_is_empty_line_not_dropped():
    res = run("100.5:1 123.9:2\n181.0707:5 100.5:1")
    assert res.text == "\n181.0707:5"
    assert len(res.records) == 2


def test_columns_single_spectrum_keeps_kept_lines():
    res = run("181.0707 100\n100.5 20\n85.0284 40\n")
    assert res.format == "columns"
    assert res.text == "181.0707 100\n85.0284 40\n"
    res = run("181.0707:100\n100.5:20\n85.0284:40")
    assert res.format == "columns" and res.text == "181.0707:100\n85.0284:40"


def test_bad_record_among_good_is_passed_through_verbatim_with_reason_only():
    bad = "Name: Bad\nNum Peaks: 2\nsecret1 20\n100.5\n"
    text = "Name: G1\n100.5 1\n85.0284 1\n\n" + bad + "\nName: G2\n123.9 1\n"
    res = run(text)
    assert res.text == "Name: G1\n85.0284 1\n\n" + bad + "\nName: G2\n"
    err = [r["error"] for r in res.records]
    assert err[0] is None and err[2] is None and err[1]
    assert "secret1" not in err[1] and "100.5" not in err[1]
    assert res.summary["passed_through"] == 1 and res.summary["cleaned"] == 2


def test_record_over_max_peaks_is_passed_through_with_error():
    big = "Name: Big\n" + "".join("%d.5 1\n" % i for i in range(100, 110))
    res = run(big + "\nName: S\n100.5 1\n", max_peaks=5)
    assert res.text.startswith(big)
    assert res.records[0]["error"] == "more than 5 peaks"
    assert res.records[1]["error"] is None


def test_more_than_max_batch_records_raises_limit_error():
    text = "".join("Name: n%d\n100 1\n\n" % i for i in range(4))
    with pytest.raises(cv.ConvertLimitError):
        run(text, max_batch=3)
    assert len(run(text, max_batch=4).records) == 4


def test_cleaning_twice_is_idempotent():
    for name in ("sample.msp", "sample.mgf", "sample.strings.txt"):
        once = run((FIX / name).read_text(), default_mode="pos").text
        assert run(once).text == once, name


def test_metadata_with_colons_is_not_mistaken_for_peaks():
    text = ("Name: 123:45\nComment: 100.5:20 and 77:1\nIonMode: positive\nNum Peaks: 2\n"
            "100.5 20\n181.0707 5\nComment: trailing 9:9\n")
    res = run(text)
    assert res.text == ("Name: 123:45\nComment: 100.5:20 and 77:1\nIonMode: positive\n"
                        "Num Peaks: 1\n181.0707 5\nComment: trailing 9:9\n")
    assert res.records[0]["name"] == "123:45" and res.records[0]["total"] == 2


def test_num_peaks_after_peaks_is_rewritten_too():
    res = run("Name: X\n100.5 1\n181.0707 1\nNum Peaks: 2\n")
    assert res.text == "Name: X\n181.0707 1\nNum Peaks: 1\n"


def test_trailing_record_without_newline_and_blank_line_runs_preserved():
    text = "\n\nName: A\n181.0707 1\n\n\n\nName: B\n85.0284 2"
    assert run(text, everything).text == text
    assert run(text).text == text
    text2 = "Name: A\n100.5 1\n181.0707 1\n\n\n\nName: B\n85.0284 2\n123.9 1"
    assert run(text2).text == "Name: A\n181.0707 1\n\n\n\nName: B\n85.0284 2\n"


def test_classifier_failure_never_loses_data():
    def boom(peaks, mode, precursor):
        raise RuntimeError("secret 100.5")
    text = "Name: A\n100.5 1\n"
    res = run(text, boom)
    assert res.text == text and res.records[0]["error"] == "classification failed"


def test_mgf_line_with_several_pairs_judges_every_pair():
    text = "BEGIN IONS\nIONMODE=pos\n181.0707:10 100.5:3\nEND IONS\n"
    res = run(text)
    assert res.text == "BEGIN IONS\nIONMODE=pos\n181.0707:10\nEND IONS\n"
    assert (res.records[0]["total"], res.records[0]["impossible"]) == (2, 1)


def test_mgf_loose_line_with_several_pairs_rebuilt_from_original_tokens():
    text = "BEGIN IONS\nIONMODE=pos\n181.0707 10.0 100.5 3 85.0284 4\nEND IONS\n"
    assert run(text).text == "BEGIN IONS\nIONMODE=pos\n181.0707 10.0 85.0284 4\nEND IONS\n"


def test_mgf_unparseable_peak_line_passes_record_through_with_error():
    text = "BEGIN IONS\nIONMODE=pos\n181.0707 10 100.5\nEND IONS\n"
    res = run(text)
    assert res.text == text and res.records[0]["error"]


def test_split_stops_at_max_batch_plus_one_without_parsing_records(monkeypatch):
    made = []
    real = cv._make
    monkeypatch.setattr(cv, "_make", lambda *a, **k: made.append(1) or real(*a, **k))
    with pytest.raises(cv.ConvertLimitError):
        cv.prepare("Name:x\n" * 20000, "pos", None, 5000, 3)
    assert not made
    with pytest.raises(cv.ConvertLimitError):
        cv.prepare("1:1 1:1\n" * 20000, "pos", None, 5000, 3)
    assert not made


def test_peak_line_stops_tokenising_past_max_peaks(monkeypatch):
    seen = []
    real = cv._number
    monkeypatch.setattr(cv, "_number", lambda *a: seen.append(1) or real(*a))
    for text in ("1:1 " * 50000, "1 1 " * 50000, "Name: a\n" + "1:1 " * 50000):
        del seen[:]
        res = run(text, max_peaks=5)
        assert res.records[0]["error"] == "more than 5 peaks"
        assert res.text == text
        assert len(seen) < 100


NIST = ("Name: Glucose\nIonMode: Positive\nPrecursorMZ: 181.0707\nNum Peaks: 4\n"
        '181.0707\t10\t"C6H13O6+/-0.5ppm"\n100.5\t3\t"unknown"\n'
        '85.0284 4 "C4H5O2+ loss of 96"\n123.9\t7\t"x"\n')


def test_msp_annotated_peak_lines_are_one_peak_each():
    res = run(NIST)
    assert res.text == ("Name: Glucose\nIonMode: Positive\nPrecursorMZ: 181.0707\nNum Peaks: 2\n"
                        '181.0707\t10\t"C6H13O6+/-0.5ppm"\n85.0284 4 "C4H5O2+ loss of 96"\n')
    assert (res.records[0]["total"], res.records[0]["impossible"], res.records[0]["error"]) \
        == (4, 2, None)


def test_msp_annotated_bare_text_and_crlf_and_all_kept_identity():
    text = ("Name: M\r\nIon_mode: P\r\nNum Peaks: 2\r\n181.0707 10 C6H13O6 1.2ppm\r\n"
            "85.0284 4 \"x\"\r\n")
    assert run(text).text == text
    res = run(text, fn=lambda p, m, x: [False] * len(p))
    assert res.text == "Name: M\r\nIon_mode: P\r\nNum Peaks: 0\r\n"


def test_annotated_lines_do_not_change_plain_line_behaviour():
    assert run("Name: a\nNum Peaks: 1\n181.0707 10 100.5\n").records[0]["error"]
    assert run("Name: a\nNum Peaks: 1\n181.0707 10 100.5:3\n").records[0]["error"]
    assert run("Name: a\nNum Peaks: 1\n181.0707 10 abc\n").records[0]["error"] is None


def test_annotation_with_bad_numbers_still_errors():
    res = run('Name: a\nNum Peaks: 1\n-5 10 "x"\n')
    assert res.records[0]["error"]
