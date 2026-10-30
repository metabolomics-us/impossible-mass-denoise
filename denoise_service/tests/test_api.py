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
def bigclient():
    with TestClient(create_app(small(max_body_bytes=1000000))) as c:
        yield c


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


def test_422_does_not_echo_input(bigclient, caplog):
    caplog.set_level(logging.DEBUG)
    secret = "SECRET123"
    for body in ({"mode": "neg", "spectrum": "5 %s" % secret},
                 {"mode": "neg", "spectrum": "%s 5" % secret},
                 {"mode": "neg", "spectrum": "%s:5" % "-" + secret},
                 {"mode": "neg", "spectrum": "-%s:3 1:2" % "5"},
                 {"mode": "neg", "spectrum": "5:%s" % secret},
                 {"mode": "neg", "spectrum": "1:2 3:4:%s" % secret},
                 {"mode": "neg", "spectrum": "1:2 3:4 %s" % secret},
                 {"mode": "neg", "spectrum": "1 -5:%s" % secret},
                 {"mode": secret, "peaks": [[86.06, 1]]},
                 {"mode": "neg", "peaks": [[86.06, 1]], "deuterium": secret},
                 {"mode": "neg", "peaks": [[secret, 1]]}):
        r = post(bigclient, body)
        assert r.status_code == 422
        assert secret not in r.text
    big = "9" * 100000
    for text in (big + "x 5", "-" + big + ":3", "1:" + big + "x"):
        r = post(bigclient, {"mode": "neg", "spectrum": text})
        assert r.status_code == 422
        assert len(r.text) < 300
        assert big[:50] not in r.text
    assert secret not in caplog.text and big[:50] not in caplog.text
    r = post(bigclient, {"mode": "neg", "spectrum": "-5:3"})
    assert r.json()["detail"] == "line 1: m/z must be greater than zero"


def test_unknown_field_name_is_truncated(bigclient):
    key = "k" * 5000
    r = post(bigclient, {"mode": "neg", "peaks": [[86.06, 1]], key: 1})
    assert r.status_code == 422
    assert len(r.text) < 300 and key not in r.text
    assert "k" * 32 + "..." in r.json()["detail"]


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


# ---- work budget, worker thread, pending cap -------------------------------------------------

import asyncio
import threading
import time

import httpx

import denoise_service.classify as classify_mod


def test_budget_exceeded_422_single_and_batch():
    cfg = small(max_peaks=5000, max_request_lookups=20, max_label_lookups=100)
    with TestClient(create_app(cfg)) as c:
        peaks = [[50.0 + i, 1] for i in range(5)]
        # 5 peaks x 4 combinations = 20: at the budget, allowed
        ok = {"mode": "neg", "peaks": peaks, "deuterium": 3}
        assert post(c, ok).status_code == 200
        r = post(c, dict(ok, deuterium=4))
        assert r.status_code == 422 and "budget" in r.json()["detail"]
        assert "50.0" not in r.text
        # each spectrum fits alone (15 and 15) but the batch total is 30
        one = {"mode": "neg", "peaks": peaks[:3], "deuterium": 4}
        assert post(c, {"spectra": [one]}, "/v1/classify/batch").status_code == 200
        r = post(c, {"spectra": [one, one]}, "/v1/classify/batch")
        assert r.status_code == 422 and "budget" in r.json()["detail"]
        # text input is counted after parsing
        text = {"mode": "neg", "spectrum": " ".join("%d:1" % (50 + i) for i in range(5)),
                "deuterium": 4}
        assert post(c, text).status_code == 422


@pytest.fixture
def slow(monkeypatch):
    info = {"threads": [], "started": threading.Event()}
    real = classify_mod.classify

    def slow_classify(peaks, options):
        info["threads"].append(threading.current_thread().name)
        info["started"].set()
        time.sleep(0.5)
        return real(peaks, options)
    monkeypatch.setattr(classify_mod, "classify", slow_classify)
    return info


async def _wait(event, limit=5.0):
    end = time.monotonic() + limit
    while not event.is_set():
        assert time.monotonic() < end
        await asyncio.sleep(0.01)


BODY = {"mode": "neg", "peaks": [[86.06, 45]]}


@pytest.mark.anyio
async def test_healthz_answers_while_classify_runs(slow):
    app = create_app(small())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as c:
            job = asyncio.ensure_future(c.post("/v1/classify", json=BODY))
            await _wait(slow["started"])
            t0 = time.monotonic()
            h = await c.get("/healthz")
            assert h.status_code == 200 and time.monotonic() - t0 < 0.25
            assert (await job).status_code == 200
    assert slow["threads"][0].startswith("denoise-worker")
    assert slow["threads"][0] != threading.current_thread().name


@pytest.mark.anyio
async def test_pending_cap_503_with_retry_after(slow):
    app = create_app(small(max_pending=1))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as c:
            first = asyncio.ensure_future(c.post("/v1/classify", json=BODY))
            await _wait(slow["started"])
            r = await c.post("/v1/classify", json=BODY)
            assert r.status_code == 503 and "detail" in r.json()
            assert int(r.headers["retry-after"]) >= 1
            assert (await first).status_code == 200
            assert (await c.post("/v1/classify", json=BODY)).status_code == 200


@pytest.mark.anyio
async def test_cache_clear_runs_on_worker_thread(monkeypatch):
    seen = []
    done = threading.Event()

    def fake_clear():
        seen.append(threading.current_thread().name)
        done.set()
    monkeypatch.setattr(denoise, "clear_cache", fake_clear)
    app = create_app(small(cache_clear_seconds=1))
    async with app.router.lifespan_context(app):
        await _wait(done, 5.0)
    assert seen and seen[0].startswith("denoise-worker")
    assert seen[0] != threading.current_thread().name


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ---- final-review wave -----------------------------------------------------------------------

def test_cache_is_capped_after_a_job(monkeypatch):
    cleared = []
    real = denoise.clear_cache
    monkeypatch.setattr(denoise, "clear_cache", lambda: (cleared.append(1), real())[1])
    with TestClient(create_app(small(max_cache_entries=50))) as c:
        peaks = [[50.123 + 7.31 * i, 1] for i in range(5)]
        r = post(c, {"mode": "neg", "peaks": peaks, "deuterium": 3, "carbon13": 3})
        assert r.status_code == 200
        assert c.get("/healthz").json()["cache_size"] <= 50
    assert cleared


def test_fingerprint_computed_once(monkeypatch):
    calls = []
    real = classify_mod.fingerprint
    monkeypatch.setattr(classify_mod, "fingerprint", lambda: (calls.append(1), real())[1])
    with TestClient(create_app(small())) as c:
        for _ in range(3):
            r = post(c, {"mode": "neg", "peaks": [[86.06, 1]]})
            assert r.json()["fingerprint"] == FINGERPRINT
        r = post(c, {"spectra": [BODY, BODY]}, "/v1/classify/batch")
        assert [x["fingerprint"] for x in r.json()["results"]] == [FINGERPRINT] * 2
        assert c.get("/healthz").json()["fingerprint"] == FINGERPRINT
    assert len(calls) <= 1


def test_retry_after_is_exposed_to_browsers():
    origin = {"origin": "https://example.org"}
    with TestClient(create_app(small(rate_per_minute=1, rate_burst=1))) as c:
        assert post(c, BODY, headers=origin).status_code == 200
        r = post(c, BODY, headers=origin)
        assert r.status_code == 429
        assert "retry-after" in r.headers["access-control-expose-headers"].lower()


@pytest.mark.anyio
async def test_503_exposes_retry_after(slow):
    app = create_app(small(max_pending=1, max_inflight_per_client=9))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as c:
            first = asyncio.ensure_future(c.post("/v1/classify", json=BODY))
            await _wait(slow["started"])
            r = await c.post("/v1/classify", json=BODY, headers={"origin": "https://e.org"})
            assert r.status_code == 503
            assert "retry-after" in r.headers["access-control-expose-headers"].lower()
            await first


@pytest.mark.anyio
async def test_503_before_body_is_parsed(slow):
    app = create_app(small(max_pending=1, max_inflight_per_client=9))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as c:
            first = asyncio.ensure_future(c.post("/v1/classify", json=BODY))
            await _wait(slow["started"])
            r = await c.post("/v1/classify", content=b"{not json",
                             headers={"content-type": "application/json"})
            assert r.status_code == 503
            await first


@pytest.mark.anyio
async def test_per_client_inflight_cap(slow):
    app = create_app(small(max_inflight_per_client=1))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as c:
            a = {"x-forwarded-for": "1.1.1.1"}
            first = asyncio.ensure_future(c.post("/v1/classify", json=BODY, headers=a))
            await _wait(slow["started"])
            r = await c.post("/v1/classify", json=BODY, headers=a)
            assert r.status_code == 429 and r.headers["retry-after"] == "1"
            assert "detail" in r.json()
            other = asyncio.ensure_future(
                c.post("/v1/classify", json=BODY, headers={"x-forwarded-for": "2.2.2.2"}))
            assert (await first).status_code == 200
            assert (await other).status_code == 200
            # released after completion, and after a 422 path
            assert (await c.post("/v1/classify", json=BODY, headers=a)).status_code == 200
            assert (await c.post("/v1/classify", json={"mode": "x"}, headers=a)).status_code == 422
            assert (await c.post("/v1/classify", json=BODY, headers=a)).status_code == 200


def test_inflight_released_after_job_exception(monkeypatch):
    real = classify_mod.classify
    boom = {"on": True}

    def flaky(peaks, options):
        if boom["on"]:
            boom["on"] = False
            raise RuntimeError("boom")
        return real(peaks, options)
    monkeypatch.setattr(classify_mod, "classify", flaky)
    with TestClient(create_app(small(max_inflight_per_client=1, max_pending=1)),
                    raise_server_exceptions=False) as c:
        assert post(c, BODY).status_code == 500
        assert post(c, BODY).status_code == 200


def test_text_parsing_stops_at_max_peaks(client):
    text = "\n".join("%d.5:1" % i for i in range(60, 160))
    r = post(client, {"mode": "neg", "spectrum": text})
    assert r.status_code == 422 and "more than 5 peaks" in r.json()["detail"]
