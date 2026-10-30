"""API tests: run against the real filter through FastAPI's TestClient (lifespan entered)."""
import logging

import denoise
import pytest
from fastapi.testclient import TestClient

from denoise_service.app import create_app
from denoise_service.config import Config

FINGERPRINT = "5aae4ec26694da4d"


def small(**kw):
    base = dict(max_peaks=5, max_batch=3, max_body_bytes=2000, max_label=12,
                max_label_lookups=100, rate_per_minute=6000, rate_burst=1000)
    base.update(kw)
    return Config(**base)


@pytest.fixture
def client():
    with TestClient(create_app(small())) as c:
        yield c


def post(client, body, path="/v1/classify", **kw):
    return client.post(path, json=body, **kw)


def test_classify_json_peaks_shape_and_order(client):
    r = post(client, {"mode": "neg", "peaks": [[104.11, 100], [86.06, 45], [96.9691, 10]]})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["fingerprint"] == FINGERPRINT and body["mode"] == "neg"
    assert [i["mz"] for i in body["ions"]] == [104.11, 86.06, 96.9691]
    assert [i["intensity"] for i in body["ions"]] == [100.0, 45.0, 10.0]
    assert all(isinstance(i["possible"], bool) for i in body["ions"])
    s = body["summary"]
    assert set(s) == {"total", "impossible", "intensity_total", "intensity_impossible",
                      "intensity_impossible_fraction"}
    assert s["total"] == 3 and s["intensity_total"] == 155.0
    assert s["impossible"] == sum(not i["possible"] for i in body["ions"])


def test_classify_text_spectrum_matches_peaks_form(client):
    a = post(client, {"mode": "neg", "peaks": [[86.06, 45], [104.11, 100]]}).json()
    b = post(client, {"mode": "neg", "spectrum": "86.06:45 104.11:100"}).json()
    assert a == b


def test_classify_requires_exactly_one_of_peaks_and_spectrum(client):
    assert post(client, {"mode": "neg"}).status_code == 422
    both = {"mode": "neg", "peaks": [[86.06, 45]], "spectrum": "86.06:45"}
    r = post(client, both)
    assert r.status_code == 422 and "detail" in r.json()


@pytest.mark.parametrize("mode", [None, "negative", "NEG", "", 1])
def test_mode_required_and_validated(client, mode):
    body = {"peaks": [[86.06, 45]]}
    if mode is not None:
        body["mode"] = mode
    assert post(client, body).status_code == 422


def test_bad_text_returns_422_naming_line(client):
    r = post(client, {"mode": "neg", "spectrum": "86.06:45\n104.11:abc"})
    assert r.status_code == 422
    assert "line 2" in r.json()["detail"]


def test_over_limit_peaks_422(client):
    peaks = [[50.0 + i, 1] for i in range(6)]
    assert post(client, {"mode": "neg", "peaks": peaks}).status_code == 422
    text = " ".join("%d:1" % (50 + i) for i in range(6))
    assert post(client, {"mode": "neg", "spectrum": text}).status_code == 422
    assert post(client, {"mode": "neg", "peaks": peaks[:5]}).status_code == 200


def test_oversized_body_413(client):
    pad = "x" * 3000
    r = post(client, {"mode": "neg", "spectrum": "86.06:45", "pad": pad})
    assert r.status_code == 413 and "detail" in r.json()


def test_oversized_body_413_without_content_length(client):
    def gen():
        yield b'{"mode":"neg","spectrum":"86.06:45","pad":"'
        for _ in range(5):
            yield b"x" * 1000
        yield b'"}'
    r = client.post("/v1/classify", content=gen(),
                    headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_label_limits_422(client):
    base = {"mode": "neg", "peaks": [[86.06, 45]]}
    assert post(client, dict(base, deuterium=13)).status_code == 422
    assert post(client, dict(base, deuterium=-1)).status_code == 422
    assert post(client, dict(base, deuterium=12, carbon13=12)).status_code == 422
    assert post(client, dict(base, deuterium=3, carbon13=2, nitrogen15=1)).status_code == 200


@pytest.mark.parametrize("peak", [[0, 5], [-1, 5], [-1, 5], [86.06, -1],
                                  ["a", 5], [True, 5], [86.06], [1, 2, 3]])
def test_nonfinite_and_nonpositive_mz_422(client, peak):
    assert post(client, {"mode": "neg", "peaks": [peak]}).status_code == 422


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "1e999"])
def test_nonfinite_literal_in_json_422(client, literal):
    body = ('{"mode":"neg","peaks":[[%s,5]]}' % literal).encode()
    r = client.post("/v1/classify", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 422


def test_empty_spectrum_422(client):
    assert post(client, {"mode": "neg", "spectrum": ""}).status_code == 422
    assert post(client, {"mode": "neg", "spectrum": "   \n"}).status_code == 422
    assert post(client, {"mode": "neg", "peaks": []}).status_code == 422


def test_422_does_not_echo_input(client):
    secret = "86.0612345"
    for body in ({"mode": "neg", "spectrum": "%s:zzz" % secret},
                 {"mode": secret, "peaks": [[86.06, 1]]},
                 {"mode": "neg", "peaks": [[86.06, 1]], "deuterium": secret},
                 {"mode": "neg", "peaks": [[86.06, 1]], "bogus": secret}):
        r = post(client, body)
        assert r.status_code == 422
        assert secret not in r.text


def test_not_json_or_not_object_422(client):
    h = {"content-type": "application/json"}
    assert client.post("/v1/classify", content=b"{nope", headers=h).status_code == 422
    assert client.post("/v1/classify", content=b"[1,2]", headers=h).status_code == 422


def test_batch_order_ids_and_error_index(client):
    spectra = [{"id": "b", "mode": "neg", "spectrum": "104.11:100"},
               {"id": "a", "mode": "pos", "peaks": [[86.06, 45], [104.11, 100]]},
               {"mode": "neg", "peaks": [[86.06, 45]]}]
    r = post(client, {"spectra": spectra}, "/v1/classify/batch")
    assert r.status_code == 200
    res = r.json()["results"]
    assert [x.get("id") for x in res] == ["b", "a", None]
    assert [x["mode"] for x in res] == ["neg", "pos", "neg"]
    assert res[1]["ions"][0]["mz"] == 86.06
    assert res[0]["fingerprint"] == FINGERPRINT
    spectra[1]["mode"] = "bogus"
    r = post(client, {"spectra": spectra}, "/v1/classify/batch")
    assert r.status_code == 422 and "spectra[1]" in r.json()["detail"]


def test_batch_over_limit_422(client):
    one = {"mode": "neg", "peaks": [[86.06, 45]]}
    assert post(client, {"spectra": [one] * 4}, "/v1/classify/batch").status_code == 422
    assert post(client, {"spectra": []}, "/v1/classify/batch").status_code == 422
    assert post(client, {}, "/v1/classify/batch").status_code == 422


def test_healthz_reports_tables_and_fingerprint(client):
    client.post("/v1/classify", json={"mode": "neg", "peaks": [[86.06, 45]]})
    r = client.get("/healthz")
    assert r.status_code == 200
    b = r.json()
    assert b["status"] == "ok" and b["fingerprint"] == FINGERPRINT
    assert b["tables"]["chnops"].startswith("loaded")
    assert b["tables"]["halogen"].startswith("loaded")
    assert isinstance(b["cache_size"], int)


def test_startup_refuses_when_tables_missing(monkeypatch):
    monkeypatch.setattr(denoise, "table_status", lambda: "unusable: missing")
    with pytest.raises(RuntimeError):
        with TestClient(create_app(small())):
            pass


def test_startup_refuses_on_fingerprint_mismatch(monkeypatch):
    monkeypatch.setattr(denoise, "fingerprint", lambda: "0" * 16)
    with pytest.raises(RuntimeError):
        with TestClient(create_app(small())):
            pass


def test_rate_limit_429_with_retry_after():
    cfg = small(rate_per_minute=1, rate_burst=2)
    with TestClient(create_app(cfg)) as c:
        body = {"mode": "neg", "peaks": [[86.06, 45]]}
        assert post(c, body).status_code == 200
        assert post(c, body).status_code == 200
        r = post(c, body)
        assert r.status_code == 429 and "detail" in r.json()
        assert int(r.headers["retry-after"]) >= 1


def test_rate_limit_keyed_by_forwarded_for_only_when_trusted():
    body = {"mode": "neg", "peaks": [[86.06, 45]]}
    with TestClient(create_app(small(rate_per_minute=1, rate_burst=1, trusted_proxy=True))) as c:
        assert post(c, body, headers={"x-forwarded-for": "1.1.1.1, 9.9.9.9"}).status_code == 200
        assert post(c, body, headers={"x-forwarded-for": "2.2.2.2"}).status_code == 200
        assert post(c, body, headers={"x-forwarded-for": "1.1.1.1, 8.8.8.8"}).status_code == 429
    with TestClient(create_app(small(rate_per_minute=1, rate_burst=1, trusted_proxy=False))) as c:
        assert post(c, body, headers={"x-forwarded-for": "1.1.1.1"}).status_code == 200
        assert post(c, body, headers={"x-forwarded-for": "2.2.2.2"}).status_code == 429


def test_cors_header_for_any_origin(client):
    r = client.post("/v1/classify", json={"mode": "neg", "peaks": [[86.06, 45]]},
                    headers={"origin": "https://example.org"})
    assert r.headers["access-control-allow-origin"] == "*"
    pre = client.options("/v1/classify", headers={
        "origin": "https://example.org", "access-control-request-method": "POST",
        "access-control-request-headers": "content-type"})
    assert pre.status_code == 200
    assert pre.headers["access-control-allow-origin"] == "*"


def test_logs_do_not_contain_spectrum_values(client, caplog):
    caplog.set_level(logging.DEBUG)
    post(client, {"mode": "neg", "spectrum": "86.0612:45.5 104.1111:100"})
    post(client, {"mode": "neg", "spectrum": "86.0612:zzz"})
    post(client, {"mode": "neg", "peaks": [[86.0612, 45.5], [104.1111, 100]], "deuterium": 99})
    text = caplog.text
    for s in ("86.0612", "104.1111", "45.5"):
        assert s not in text
    assert "/v1/classify" in text
