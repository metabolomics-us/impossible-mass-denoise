"""Two surfaces, one answer. Runs in the service venv (no MCP SDK installed there).

`denoise_mcp` must classify with the same code `denoise_service` classifies with, and refuse the
same malformed input. If someone re-implements cleaning inside the MCP layer, this is what fails.
"""
import json
from pathlib import Path

import pytest

from denoise_mcp import clean, limits
from denoise_service import classify
from denoise_service.app import ApiError, _validate
from denoise_service.config import Config

ROOT = Path(__file__).resolve().parents[2]
SMOKE = json.loads((ROOT / "ops" / "deploy" / "smoke.json").read_text())

CASES = [
    [(181.0707, 100.0), (100.5, 20.0), (123.9, 5.0), (85.0284, 40.0)],
    [(474.9938, 6.3), (496.9761, 2.8), (393.0010, 100.0), (519.0, 1.0)],
    [(100.5, 1.0)],
]


@pytest.mark.parametrize("peaks", CASES)
@pytest.mark.parametrize("mode", ["pos", "neg"])
@pytest.mark.parametrize("options", [{}, {"halogens": True}, {"deuterium": 3, "carbon13": 1}])
def test_the_two_surfaces_keep_exactly_the_same_peaks(peaks, mode, options):
    service = classify.classify(peaks, classify.Options(mode=mode, **options))
    mcp = clean.clean_spectrum(peaks, mode, **options)
    service_kept = {mz for (mz, _intensity), ok in zip(peaks, service.possible) if ok}
    assert {p.mz for p in mcp.kept} == service_kept
    # And the removed set is the complement, not a third answer.
    assert {r.mz for r in mcp.removed} == {mz for (mz, _i), ok in zip(peaks, service.possible)
                                          if not ok}


def test_the_smoke_peaks_agree_on_both_surfaces():
    peaks = [(float(m), float(i)) for m, i in SMOKE["request"]["peaks"]]
    service = classify.classify(peaks, classify.Options(mode=SMOKE["request"]["mode"]))
    mcp = clean.clean_spectrum(peaks, SMOKE["request"]["mode"])
    assert list(service.possible) == SMOKE["expect"]
    kept = {p.mz for p in mcp.kept}
    assert [mz in kept for mz, _ri in peaks] == list(service.possible)


@pytest.mark.parametrize("bad", [
    '{"mode":"negative","peaks":[[100.5,1.0]]}',
    '{"mode":"pos","peaks":[]}',
    '{"mode":"pos","peaks":[["x",1.0]]}',
    '{"mode":"pos","peaks":[[-1.0,1.0]]}',
    '{"mode":"pos","peaks":[[100.5,1.0]],"deuterium":20}',
    '{"mode":"pos","peaks":[[100.5,1.0]],"halogens":"yes"}',
])
def test_the_two_surfaces_reject_the_same_requests(bad):
    payload = json.loads(bad)
    with pytest.raises(ApiError) as rest:
        _validate(payload, Config(), "request")
    assert rest.value.status == 422
    kwargs = {k: payload[k] for k in ("halogens", "deuterium", "carbon13", "nitrogen15")
              if k in payload}
    with pytest.raises(limits.LimitError):
        clean.clean_spectrum(payload["peaks"], payload["mode"], **kwargs)


def test_the_rejection_message_names_the_same_field_on_both_surfaces():
    bad = '{"mode":"pos","peaks":[[100.5,1.0]],"deuterium":20}'
    payload = json.loads(bad)
    with pytest.raises(ApiError) as rest:
        _validate(payload, Config(), "request")
    with pytest.raises(limits.LimitError) as mcp:
        clean.clean_spectrum(payload["peaks"], "pos", deuterium=20)
    assert "deuterium" in rest.value.detail and "deuterium" in str(mcp.value)
    assert str(Config().max_label) in rest.value.detail
    assert str(limits.Limits().max_label) in str(mcp.value)


def test_service_and_mcp_share_the_same_max_peaks_number():
    from denoise_service.config import Config as ServiceConfig
    assert limits.Limits().max_peaks == ServiceConfig().max_peaks
