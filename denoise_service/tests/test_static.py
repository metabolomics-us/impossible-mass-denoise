"""The paste-and-plot page: served by the same app, self-contained, never shadows the API."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from denoise_service.app import create_app
from denoise_service.config import Config

STATIC = Path(__file__).resolve().parent.parent / "static"
CAVEAT = ("Impossible means not explained by CHNOPS, singly charged, monoisotopic. "
          "Works for Orbitrap data; off by default for TTOF; not for QTOF. "
          "Ions above 1700 m/z are always reported possible.")


@pytest.fixture(scope="module")
def client():
    with TestClient(create_app(Config())) as c:
        yield c


def test_root_serves_page_with_brand_colours_and_no_external_assets(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    css = client.get("/static/style.css").text
    assert "#022851" in css and "#FFBF00" in css.upper()
    assert "Impossible-mass denoise" in r.text
    assert "West Coast Metabolomics Center" in r.text
    assert CAVEAT in r.text
    assert '<html lang="en"' in r.text
    assert "/static/app.js" in r.text and "/static/style.css" in r.text
    for name, text in (("index.html", r.text), ("style.css", css),
                       ("app.js", client.get("/static/app.js").text)):
        refs = re.findall(r'(?:src|href)\s*=\s*["\']([^"\']+)', text)
        refs += re.findall(r'url\(\s*["\']?([^)"\']+)', text)
        for ref in refs:
            assert not re.match(r"(https?:)?//", ref), (name, ref)


def test_static_assets_served(client):
    js = client.get("/static/app.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    css = client.get("/static/style.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")


def test_app_js_never_uses_innerhtml():
    assert "innerHTML" not in (STATIC / "app.js").read_text()


def test_api_routes_not_shadowed_and_docs_work(client):
    assert client.get("/healthz").json()["status"] == "ok"
    assert client.get("/docs").status_code == 200
    r = client.post("/v1/classify", json={"mode": "neg", "spectrum": "86.06:45 104.11:100"})
    assert r.status_code == 200 and r.json()["summary"]["total"] == 2


CSP = ("default-src 'self'; img-src 'self' data:; base-uri 'none'; "
       "form-action 'none'; frame-ancestors 'none'")


def test_page_links_docs_and_openapi_and_schema_lists_paths(client):
    html = client.get("/").text
    assert 'href="/docs"' in html and 'href="/openapi.json"' in html
    r = client.get("/openapi.json")
    assert r.status_code == 200 and "json" in r.headers["content-type"]
    assert {"/v1/classify", "/v1/classify/batch", "/healthz"} <= set(r.json()["paths"])


@pytest.mark.parametrize("path", ["/", "/static/app.js", "/static/style.css"])
def test_page_responses_have_cache_and_csp_headers(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers["content-security-policy"] == CSP


def test_not_modified_response_keeps_headers(client):
    first = client.get("/static/app.js")
    r = client.get("/static/app.js", headers={"If-None-Match": first.headers["etag"]})
    assert r.status_code == 304
    assert r.headers["content-security-policy"] == CSP


def test_csp_scoped_to_page_only(client):
    for r in (client.get("/docs"), client.get("/healthz"),
              client.post("/v1/classify", json={"mode": "neg", "spectrum": "86.06:45"})):
        assert "content-security-policy" not in r.headers
