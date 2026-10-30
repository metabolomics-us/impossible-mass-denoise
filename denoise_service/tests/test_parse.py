"""Tests for the spectrum text parser: the accepted formats and every rejection rule."""
import pytest

from denoise_service.parse import SpectrumParseError, parse_spectrum


def test_parse_colon_pairs():
    assert parse_spectrum("86.06:45 104.11:100") == [(86.06, 45.0), (104.11, 100.0)]


def test_parse_two_column_lines():
    assert parse_spectrum("86.06 45\n104.11 100") == [(86.06, 45.0), (104.11, 100.0)]


def test_parse_msp_record_skips_headers():
    text = "Name: Glucose\nNum Peaks: 2\n86.06\t45\n104.11\t100\n"
    assert parse_spectrum(text) == [(86.06, 45.0), (104.11, 100.0)]


def test_parse_crlf_commas_semicolons_blank_lines():
    text = "86.06:45;104.11:100\r\n\r\n120.5, 7\r\n130.5,8,140.5,9\r\n"
    assert parse_spectrum(text) == [(86.06, 45.0), (104.11, 100.0), (120.5, 7.0),
                                    (130.5, 8.0), (140.5, 9.0)]


def test_parse_scientific_notation():
    assert parse_spectrum("1.5e2:2E3") == [(150.0, 2000.0)]
    assert parse_spectrum("1e2 5e-1") == [(100.0, 0.5)]


def test_parse_error_names_line_and_token():
    with pytest.raises(SpectrumParseError) as exc:
        parse_spectrum("86.06:45\n104.11:abc")
    assert "line 2" in str(exc.value)
    assert "abc" in str(exc.value)


def test_parse_rejects_odd_number_count_on_line():
    with pytest.raises(SpectrumParseError, match="line 2"):
        parse_spectrum("86.06 45\n104.11 100 5")


@pytest.mark.parametrize("text", ["nan:5", "5:nan", "inf:5", "5:inf", "0:5", "-3:5", "5:-1"])
def test_parse_rejects_nonfinite_zero_negative_mz_and_negative_intensity(text):
    with pytest.raises(SpectrumParseError, match="line 1"):
        parse_spectrum(text)


@pytest.mark.parametrize("text", ["", "   \n\r\n", "Name: X\nNum Peaks: 0\n"])
def test_parse_rejects_empty(text):
    with pytest.raises(SpectrumParseError):
        parse_spectrum(text)


def test_error_has_line_and_reason_without_token():
    from denoise_service.parse import SpectrumParseError, parse_spectrum
    for text, line, reason in [("1:2\n5 SECRET123", 2, "value is not a number"),
                               ("-5:3", 1, "m/z must be greater than zero"),
                               ("1:-3", 1, "intensity must not be negative"),
                               ("1 2 3", 1, "odd number of values")]:
        try:
            parse_spectrum(text)
        except SpectrumParseError as e:
            assert e.line == line and e.reason.startswith(reason)
            assert "SECRET123" not in e.reason
        else:
            raise AssertionError(text)


def test_very_long_token_is_rejected_quickly():
    import time
    t0 = time.monotonic()
    with pytest.raises(SpectrumParseError):
        parse_spectrum("9" * 100000 + "x 5")
    assert time.monotonic() - t0 < 1.0


def test_max_peaks_stops_early():
    import time
    assert len(parse_spectrum("1:1 2:1 3:1", max_peaks=3)) == 3
    with pytest.raises(SpectrumParseError) as e:
        parse_spectrum("1:1 2:1 3:1 4:1", max_peaks=3)
    assert e.value.reason == "more than 3 peaks" and e.value.line == 1
    huge = "\n".join("%d.5:1" % i for i in range(1, 100001))
    t0 = time.monotonic()
    with pytest.raises(SpectrumParseError):
        parse_spectrum(huge, max_peaks=5000)
    assert time.monotonic() - t0 < 1.0
