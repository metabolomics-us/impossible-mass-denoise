"""POST /v1/convert against the real filter (shared limiter, caps and error shapes)."""
import asyncio
import logging
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import denoise_service.classify as classify_mod
from denoise_service.app import create_app
from denoise_service.config import Config

MSP = ("Name: A\nIonMode: Positive\nNum Peaks: 4\n181.0707 100\n100.5 20\n123.9 5\n85.0284 40\n"
       "\nName: B\nIonMode: Negative\nNum Peaks: 1\n181.0707 100\n")


def cfg(**kw):
    base = dict(max_peaks=50, max_batch=3, max_body_bytes=4000, rate_per_minute=6000,
                rate_burst=1000)
    base.update(kw)
    return Config(**base)


@pytest.fixture
def client():
    with TestClient(create_app(cfg())) as c:
        yield c


def conv(client, body):
    return client.post("/v1/convert", json=body)


def test_convert_msp_end_to_end(client):
    r = conv(client, {"text": MSP})
    assert r.status_code == 200
    body = r.json()
    assert body["fingerprint"] == "5aae4ec26694da4d" and body["format"] == "msp"
    a, b = body["records"]
    assert (a["name"], a["total"], a["impossible"], a["error"]) == ("A", 4, 2, None)
    assert (b["name"], b["total"], b["error"]) == ("B", 1, None)
    assert "Num Peaks: 2\n181.0707 100\n85.0284 40\n" in body["text"]
    assert "100.5" not in body["text"].split("Name: B")[0]
    assert body["summary"]["records"] == 2 and body["summary"]["impossible"] >= 2


def test_convert_strings_end_to_end(client):
    text = "181.0707:100 100.5:20 85.0284:40\n123.9:5 100.5:1\n"
    r = conv(client, {"text": text, "mode": "pos"})
    body = r.json()
    assert r.status_code == 200 and body["format"] == "strings"
    assert body["text"] == "181.0707:100 85.0284:40\n\n"
    assert [x["impossible"] for x in body["records"]] == [1, 2]
    # no mode anywhere: passed through, with the reason
    body = conv(client, {"text": text}).json()
    assert body["text"] == text
    assert {x["error"] for x in body["records"]} == {"no ion mode"}


def test_convert_verdicts_match_classify_for_same_peaks(client):
    peaks = [[181.0707, 100], [100.5, 20], [123.9, 5], [85.0284, 40], [96.9691, 3], [86.06, 9]]
    for mode in ("pos", "neg"):
        verdict = client.post("/v1/classify", json={"mode": mode, "peaks": peaks}).json()
        want = " ".join("%s:%s" % (p[0], p[1]) for p, i in zip(peaks, verdict["ions"])
                        if i["possible"])
        text = " ".join("%s:%s" % (p[0], p[1]) for p in peaks)
        got = conv(client, {"text": text, "mode": mode}).json()["text"]
        assert got == want


def test_convert_options_pass_through_halogens_and_precursor(monkeypatch, client):
    seen = []
    real = classify_mod.classify

    def spy(peaks, options):
        seen.append(options)
        return real(peaks, options)
    monkeypatch.setattr(classify_mod, "classify", spy)
    conv(client, {"text": "Name: A\nPrecursorMZ: 150.5\n100.5 1\n\nName: B\n100.5 1\n",
                  "mode": "neg", "halogens": True, "deuterium": 1, "carbon13": 2,
                  "nitrogen15": 1, "precursor_mz": 99.0})
    assert [(o.mode, o.halogens, o.deuterium, o.carbon13, o.nitrogen15, o.precursor_mz)
            for o in seen] == [("neg", True, 1, 2, 1, 150.5), ("neg", True, 1, 2, 1, 99.0)]


def test_convert_rejects_oversized_body_413(client):
    r = conv(client, {"text": "1:2 " * 2000})
    assert r.status_code == 413 and "detail" in r.json()


def test_convert_rejects_too_many_records_422(client):
    text = "".join("Name: n%d\n100.5 1\n\n" % i for i in range(4))
    r = conv(client, {"text": text, "mode": "pos"})
    assert r.status_code == 422 and "too many records" in r.json()["detail"]


def test_convert_validation_errors_do_not_echo_input(client):
    secret = "SECRET181.0707"
    for body in ({"text": secret, "mode": "x"}, {"text": 5}, {"text": secret, "bogus": 1},
                 {"text": secret, "format": "nope"}, {"text": secret, "deuterium": 99},
                 {"text": "  \n"}, {"text": "1:2", "mode": "pos", "precursor_mz": -1}):
        r = conv(client, body)
        assert r.status_code == 422 and secret not in r.text, body


def test_convert_budget_exceeded_422():
    with TestClient(create_app(cfg(max_request_lookups=10, max_label_lookups=100))) as c:
        text = " ".join("%d.5:1" % (100 + i) for i in range(5))
        assert conv(c, {"text": text, "mode": "pos", "deuterium": 1}).status_code == 200
        r = conv(c, {"text": text, "mode": "pos", "deuterium": 2})
        assert r.status_code == 422 and "budget" in r.json()["detail"]


def test_convert_rate_limit_and_busy_use_existing_error_shapes(monkeypatch):
    with TestClient(create_app(cfg(rate_per_minute=1, rate_burst=1))) as c:
        body = {"text": "100.5:1", "mode": "pos"}
        assert conv(c, body).status_code == 200
        r = conv(c, body)
        assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
        assert "detail" in r.json()

    started = threading.Event()
    real = classify_mod.classify

    def slow(peaks, options):
        started.set()
        time.sleep(0.5)
        return real(peaks, options)
    monkeypatch.setattr(classify_mod, "classify", slow)

    async def go():
        app = create_app(cfg(max_pending=1))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://t") as c:
                body = {"text": "100.5:1", "mode": "pos"}
                first = asyncio.ensure_future(c.post("/v1/convert", json=body))
                end = time.monotonic() + 5
                while not started.is_set():
                    assert time.monotonic() < end
                    await asyncio.sleep(0.01)
                r = await c.post("/v1/convert", json=body)
                assert r.status_code == 503 and int(r.headers["retry-after"]) >= 1
                assert (await first).status_code == 200
    asyncio.run(go())


def test_convert_never_logs_spectrum_text(client, caplog):
    caplog.set_level(logging.DEBUG)
    conv(client, {"text": "Name: SECRETNAME\n181.0707 100.25\n100.5 20\n", "mode": "pos"})
    conv(client, {"text": "181.0707:zzz 100.5:20", "mode": "pos"})
    for s in ("181.0707", "100.25", "100.5", "SECRETNAME", "zzz"):
        assert s not in caplog.text
    assert "/v1/convert" in caplog.text


def test_convert_openapi_documents_route(client):
    assert "/v1/convert" in client.get("/openapi.json").json()["paths"]
