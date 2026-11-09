"""The agent page (/mcp on this host): served, self-contained, and telling the truth.

A documentation page rots quietly, so nothing here is allowed to compare the page with another
hand-written list. Every number, code and example is taken from the code that enforces or emits it:
`denoise_mcp.limits.Limits`, `denoise_mcp.explain`, `denoise_mcp/reason-codes.json`, and
`ops/deploy/smoke.json`. If the endpoint's address, a limit, or a reason code changes and the page
does not, a test here fails.

These tests run in the service's own venv, which has no MCP SDK: the page is served by the service,
and the parts of `denoise_mcp` it quotes are the pure layer, which imports no web library.
"""
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import denoise
from denoise_mcp import explain, limits
from denoise_service.app import PAGE_CSP, create_app
from denoise_service.config import Config

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "denoise_service" / "static"
ENDPOINT = "https://denoise.mcp.metabolomics.us/mcp"

# The four peaks of ops/deploy/smoke.json: two kept, two removed. The page shows the real answer.
SMOKE_FILE = json.loads((ROOT / "ops" / "deploy" / "smoke.json").read_text())
SMOKE = SMOKE_FILE["mcp"]


@pytest.fixture(scope="module")
def client():
    with TestClient(create_app(Config())) as c:
        yield c


@pytest.fixture(scope="module")
def page(client):
    r = client.get("/mcp")
    assert r.status_code == 200, r.text
    return r.text


def mono_cells(html):
    """The values in the page's tables, as written."""
    return [c.strip() for c in re.findall(r'<td class="mono">([^<]+)</td>', html)]


# ---------------------------------------------------------------- served as a page


def test_the_page_is_served_as_html_with_the_pages_headers(client):
    r = client.get("/mcp")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers["content-security-policy"] == PAGE_CSP
    assert '<html lang="en"' in r.text
    assert "/static/style.css" in r.text


def test_the_page_names_the_endpoint_the_mcp_host_serves(client, page):
    text = page
    assert ENDPOINT in text, "the address an agent must type is not on the page"
    assert "Streamable HTTP" in text
    assert "denoise://reason-codes" in text
    # The guide is on the site host; the endpoint host is a different name and serves only /mcp.
    assert "https://denoise.metabolomics.us/mcp" not in text.replace(ENDPOINT, "")


def test_the_endpoint_address_comes_before_the_explanation(client, page):
    """A person in a hurry, or an agent extracting instructions, needs the address first."""
    first_url = page.index("denoise.mcp.metabolomics.us/mcp")
    assert first_url < page.index("<h2>Tools</h2>"), "the address is buried below the detail"
    assert first_url < page.index("run it beside your own data")
    assert page.index('class="endpoint"') < page.index("<h2>Tools</h2>")


def test_the_page_fetches_nothing_outside_this_service(client, page):
    text = page
    css = client.get("/static/style.css").text
    for name, body in (("mcp.html", text), ("style.css", css)):
        refs = re.findall(r'(?:src|href)\s*=\s*["\']([^"\']+)', body)
        refs += re.findall(r'url\(\s*["\']?([^)"\']+)', body)
        for ref in refs:
            assert not re.match(r"(https?:)?//", ref), (name, ref)


def test_the_page_runs_no_script_of_its_own(client, page):
    """The page's CSP allows no inline or extra script, so the page must not need one."""
    text = page
    assert "<script" not in text.lower()
    assert "onclick" not in text.lower() and "onload" not in text.lower()


def test_the_tool_page_links_here_and_this_page_links_back(client, page):
    assert 'href="/mcp"' in client.get("/").text
    text = page
    for href in ('href="/"', 'href="/docs"', 'href="/healthz"'):
        assert href in text, href


# ---------------------------------------------------------------- what it promises


def test_a_json_rpc_post_is_not_answered_with_html(client):
    """A machine that POSTs JSON-RPC to the guide's address must get a clear refusal."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method":"initialize", "params": {}})
    assert r.status_code == 405, r.text
    assert "html" not in r.headers["content-type"]
    assert "allow" in r.headers          # FastAPI says which methods this path takes


def test_the_page_does_not_shadow_the_api(client):
    assert client.get("/healthz").json()["status"] == "ok"
    assert client.get("/docs").status_code == 200
    r = client.post("/v1/classify", json={"mode": "neg", "spectrum": "86.06:45 104.11:100"})
    assert r.status_code == 200 and r.json()["summary"]["total"] == 2


# ---------------------------------------------------------------- its facts come from the code


def test_the_page_lists_exactly_the_reason_codes_the_filter_emits(client, page):
    table = re.findall(r'<table class="spec codes">.*?</table>', page, re.S)[0]
    codes = re.findall(r'<tr><td class="mono">([A-Za-z0-9.-]+)</td>', table)
    assert len(codes) == len(set(codes)), "a code is listed twice"
    assert set(codes) == set(explain.REASON_CODES), (
        "the page and the code that emits reasons disagree: page-only=%s missing=%s"
        % (sorted(set(codes) - set(explain.REASON_CODES)),
           sorted(set(explain.REASON_CODES) - set(codes))))
    # Every row has to say something; a code listed with no explanation is a dead row.
    for code in codes:
        row = re.search(r'<tr><td class="mono">%s</td><td>(.*?)</td>' % re.escape(code), table)
        assert row, "%s is listed with no explanation" % code
        gloss = re.sub(r"<[^>]+>", "", row.group(1)).strip()
        assert len(gloss) >= 20 and gloss.endswith("."), "%s's gloss is filler: %r" % (code, gloss)


def test_the_published_codebook_matches_the_page(client, page):
    book = json.loads((ROOT / "denoise_mcp" / "reason-codes.json").read_text())
    assert set(book["codes"]) == set(explain.REASON_CODES)
    assert "denoise://reason-codes" in page, "the machine-readable list is not pointed at"


def test_every_limit_the_server_enforces_is_on_the_page(client, page):
    cells = mono_cells(page)
    numbers = {re.match(r"\d+", c).group(0) for c in cells if re.match(r"\d+", c)}
    for name, value in vars(limits.Limits()).items():
        assert str(value) in numbers, "%s=%s is not stated on the page (%s)" % (name, value, cells)
    assert any("µs" in c or "&micro;s" in c for c in cells), "the work cap is stated without units"
    assert str(int(explain.TABLE_MAX_MZ)) in numbers, "the tables' ceiling is not stated"


def test_the_limits_on_the_page_are_the_ones_the_rest_service_enforces_too(client, page):
    """One sentence on the page promises both doors are the same cleaner; the numbers must agree."""
    text = page
    numbers = {re.match(r"\d+", c).group(0) for c in mono_cells(text) if re.match(r"\d+", c)}
    cfg = Config()
    for name in ("max_peaks", "max_batch", "max_label"):
        assert str(getattr(cfg, name)) in numbers, name


def test_the_example_answer_is_a_real_answer(client, page):
    """The JSON on the page is the answer the smoke peaks produce, not an illustration."""
    text = page
    example = re.findall(r"<pre><code>(.*?)</code></pre>", text, re.S)
    shown = max(example, key=len).replace("&quot;", '"')
    assert denoise.fingerprint() == SMOKE_FILE["fingerprint"]
    assert denoise.fingerprint() in shown
    from denoise_mcp import clean
    res = clean.clean_spectrum([(float(m), float(i)) for m, i in SMOKE["request"]["peaks"]],
                               SMOKE["request"]["mode"])
    # The same call the tool makes: same entry cap, same byte budget.
    payload = clean.payload(res, include_kept=False,
                            max_removed=limits.Limits().max_result_peaks,
                            max_bytes=limits.Limits().max_answer_bytes)
    # Every key that says what the answer left out has to be on the page, or the page describes an
    # answer the tool no longer sends.
    for key in ("summary", "removed", "kept", "removed_truncated", "kept_omitted",
                "kept_truncated", "answer_bytes", "answer_oversized"):
        assert '"%s"' % key in shown, "%s is missing from the example" % key
    for notice in ("removed_truncated", "kept_omitted", "kept_truncated",
                   "answer_bytes", "answer_oversized"):
        assert "<code>%s</code>" % notice in text, "%s is shown but never explained" % notice
    # The example carries a measured size. Its exact value is what the tool reports (the wire
    # counts fields this layer does not model), and test_tools_in_process.py pins that number to a
    # live answer; here it only has to be a number the tool could have produced.
    shown_bytes = int(re.search(r'"answer_bytes": (\d+)', shown).group(1))
    assert payload["answer_bytes"] <= shown_bytes <= payload["answer_bytes"] + 200, shown_bytes
    assert '"total": 4' in shown and '"kept": 2' in shown and '"removed": 2' in shown
    assert json.dumps(payload["summary"]["intensity_removed_fraction"]) in shown
    for removed in payload["removed"]:
        assert json.dumps(removed["nearest_legal_mz"]) in shown, removed["mz"]
        # The narrative is shown wrapped for reading, so check its opening rather than the line.
        assert removed["narrative"][:40].replace("&", "&amp;") in shown, removed["mz"]
    assert payload["kept_omitted"] in shown


def test_the_header_rules_the_page_states_are_the_ones_the_server_applies(client, page):
    """The transport paragraph is a claim about the MCP server's behavior, and it was measured, not
    remembered. A hosted server answers when `Accept` covers both media types (`*/*` included),
    refuses with 406 when Accept names only one of them or is absent, and refuses a POST without
    `Content-Type: application/json` with 400 -- asserted in
    denoise_mcp/tests/test_http_hosting.py against a real process.

    This test asserts the page tells clients the same three rules, so the guide cannot drift from
    the endpoint. Change one side, change the other.
    """
    html_text = page
    # The paragraph under "By hand, without a client", and the list in its "Trouble" disclosure.
    paragraph = re.search(r"<h2>By hand, without a client</h2>\s*<p>(.*?)</p>", html_text, re.S)
    assert paragraph, "the by-hand section lost its paragraph"
    para = paragraph.group(1)

    assert "Content-Type" in para and "400" in para, para
    assert "406" in para and "cover both" in para and "*/*" in para, (
        "the page must state that Accept has to cover both types and that */* does", para)

    trouble = re.search(r"<summary>Trouble</summary>\s*<ul>(.*?)</ul>", html_text, re.S)
    assert trouble, "the trouble list is gone"
    codes = re.findall(r"<strong>(\d{3})</strong>", trouble.group(1))
    assert {"400", "406", "421"} <= set(codes), codes
    assert "Content-Type" in trouble.group(1), "400 must name the header that caused it"
    assert "no <code>Accept</code>" in trouble.group(1), (
        "the page must say that sending no Accept at all is also refused", trouble.group(1))
    assert "notification stream" in trouble.group(1), (
        "a GET whose Accept offers text/event-stream opens the notification stream and stays open "
        "(proved by test_a_get_on_the_endpoint_opens_the_stream_and_does_not_end, and seen on the "
        "live endpoint); the page has to say so, or the first person to GET /mcp reads a working "
        "stream as a hang", trouble.group(1))


def test_the_page_states_the_caveat_the_rest_of_the_site_states(client, page):
    caveat = ("Impossible means not explained by CHNOPS, singly charged, monoisotopic.")
    assert caveat in page
    assert caveat in client.get("/").text


def test_the_page_style_has_no_unfinished_rules():
    css = (STATIC / "style.css").read_text()
    assert css.count("{") == css.count("}"), "unbalanced braces in style.css"
    for selector in (".endpoint", ".prose", "table.spec", "pre", ".mono"):
        assert selector in css, "%s is used by mcp.html but not styled" % selector
