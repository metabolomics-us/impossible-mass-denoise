"""Unit layer: the things that must be true for this package to be deployable.

These tests read the artefacts (vhost, unit, requirements, CI step, deploy smoke data, source
imports) rather than executing them, because the deploy is owned outside this repository and can
only ever be handed a file. If a file and its expected content drift apart, this fails here.
"""
import json
import re
from pathlib import Path

import pytest

from denoise_mcp import clean, explain, limits

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "ops" / "deploy"
CI = ROOT / "ops" / "ci"

PURE = ["denoise_mcp/explain.py", "denoise_mcp/clean.py", "denoise_mcp/limits.py"]
BANNED = ("mcp", "pydantic", "fastapi", "yaml", "uvicorn", "httpx", "starlette")


def read(rel):
    return (ROOT / rel).read_text()


# ---------------------------------------------------------------- the pure layer stays pure


@pytest.mark.parametrize("rel", PURE)
def test_pure_layer_imports_no_web_or_mcp_libraries(rel):
    src = read(rel)
    imports = re.findall(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", src, flags=re.M)
    offenders = [i for i in imports if i.split(".")[0] in BANNED]
    assert offenders == [], "%s must run on the nodes' Python 3.9 without the MCP SDK: %s" % (
        rel, offenders)


@pytest.mark.parametrize("rel", PURE)
def test_pure_layer_has_no_runtime_3_10_only_syntax(rel):
    """`X | Y` outside an annotation and `match` are 3.10+; production nodes are 3.9."""
    src = read(rel)
    assert not re.search(r"^\s*match\s+.*:\s*$", src, flags=re.M), "match statement in %s" % rel
    assert "from __future__ import annotations" in src, \
        "%s needs the annotations future import to keep unions in signatures legal on 3.9" % rel


def test_the_pure_layer_is_importable_from_the_service_side():
    """denoise_service/tests runs in a venv without the MCP SDK."""
    import denoise_service.classify
    assert callable(clean.clean_spectrum)
    assert denoise_service.classify.Options(mode="pos").mode == "pos"


# ---------------------------------------------------------------- limits agree with the service


def _defaults_of(text, class_name):
    block = re.search(r"class %s\b.*?(?=\nclass |\Z)" % class_name, text, flags=re.S).group(0)
    return {m.group(1): m.group(2) for m in
            re.finditer(r"^\s{4}(\w+):\s*\w+\s*=\s*([^,\n]+)$", block, flags=re.M)}


def test_shared_limits_are_the_same_numbers_the_rest_service_enforces():
    mine = _defaults_of(read("denoise_mcp/limits.py"), "Limits")
    theirs = _defaults_of(read("denoise_service/config.py"), "Config")
    shared = {"max_peaks", "max_batch", "max_label", "max_label_lookups", "max_request_lookups"}
    assert shared <= set(mine), "denoise_mcp declares none of %s" % sorted(shared)
    for key in sorted(shared & set(theirs)):
        assert mine[key] == theirs[key], "%s: MCP %s vs service %s" % (key, mine[key], theirs[key])


def test_every_limit_violation_is_a_named_error():
    assert issubclass(limits.LimitError, ValueError)


# ---------------------------------------------------------------- what the deploy consumes


def test_nginx_vhost_publishes_the_mcp_host_name():
    text = read("ops/deploy/nginx-denoise-mcp.conf")
    assert "server_name denoise.mcp.metabolomics.us;" in text
    # The proxy target is an `upstream` block rather than a literal address in the location, so
    # follow the name: the vhost has to reach the port the systemd unit listens on.
    assert "proxy_pass http://denoise_mcp_backend;" in text
    upstream = re.search(r"upstream denoise_mcp_backend \{(.*?)\}", text, re.S).group(1)
    assert "server 127.0.0.1:8301" in upstream, "the unit binds 8301; the vhost proxies elsewhere"
    assert "keepalive" in upstream
    # Streaming: a proxy_read_timeout shorter than the longest clean kills the connection mid-job.
    timeout = int(re.search(r"proxy_read_timeout\s+(\d+)s", text).group(1))
    assert timeout >= 120, "proxy_read_timeout is %ds" % timeout
    body = int(re.search(r"client_max_body_size\s+(\d+)([mk])", text).group(1))
    assert body >= 2
    assert re.search(r"X-Forwarded-For\s+\$remote_addr", text), \
        "the limiter key must be the real client"
    assert not re.search(r"proxy_buffering\s+on", text)


def test_only_the_mcp_path_reaches_the_mcp_server_on_that_host():
    """The endpoint host serves one path; anything else must be answered here, not proxied."""
    text = read("ops/deploy/nginx-denoise-mcp.conf")
    https = text.split("listen 443")[-1]          # past both the IPv4 and the IPv6 listen lines
    locations = re.findall(r"location\s+(.+?)\s*\{", https)
    assert locations[0] == "= /mcp", "the exact-match /mcp location must be the one that proxies"
    assert locations[-1] == "/", "the catch-all must come last"
    mcp_block = re.search(r"location = /mcp \{(.*?)\n    \}", https, re.S).group(1)
    assert "proxy_pass" in mcp_block and "limit_req" in mcp_block
    catch_all = https.split("location / {")[1]
    assert "proxy_pass" not in catch_all, "every path must not reach a server that knows only /mcp"
    assert "return 404" in catch_all and "https://denoise.metabolomics.us/mcp" in catch_all, \
        "the refusal has to point at the guide"


def test_the_vhost_declares_nothing_the_estate_already_sets_at_http_level():
    """conf.d files are included into ONE http context, and nginx rejects the same directive twice
    in the same context. This was not hypothetical: the first version of this file carried
    `limit_req_status 429;` at http level, which flasheic-mcp.conf already sets there, and
    `nginx -t` failed with "limit_req_status directive is duplicate" -- taking every vhost on that
    reverse proxy down at the next reload, not just this one.

    So: directives that belong to the http context may be declared once, and the estate's other
    conf.d files already own `limit_req_status`. Rate limiting still applies here because the zone
    is namespaced and `limit_req` is used inside the location, which inherits the status.
    """
    text = read("ops/deploy/nginx-denoise-mcp.conf")
    code = "\n".join(line.split("#")[0] for line in text.splitlines())
    assert "limit_req_status" not in code, (
        "another conf.d file sets this at http level; declaring it twice fails nginx -t for the "
        "whole reverse proxy")
    zones = set(re.findall(r"limit_req_zone[^;]*zone=([A-Za-z0-9_]+)", code))
    assert zones, "the vhost must declare its own rate-limit zone"
    assert all(z.startswith("denoise") for z in zones), (
        "zone names are global in the http context: %s" % sorted(zones))
    assert "include conf.d/tls-common.inc;" in code, (
        "TLS settings come from the shared include, not from a copy of them here")
    # The copy in this repository IS the deployed file: the nginx role mirrors it byte for byte into
    # ansible/roles/nginx/files/conf.d/denoise-mcp.conf. Say where it lands and what the mirror is,
    # or the next editor treats this as a sketch and the two drift.
    assert "# /etc/nginx/conf.d/denoise-mcp.conf   on kvm-node-12" in text, (
        "the file must name the path it is deployed to, the way every other conf.d file does")
    assert "fiehnlab-slurm-cluster" in text or "impossible-mass-denoise" in text, (
        "the file must name the mirror, in this repo or in the nginx role")


def test_the_deploy_docs_point_monitors_at_a_request_that_exists():
    """A health path is the easiest thing in a deploy document to invent: it reads plausibly, and
    the person who writes it rarely has a process in front of them. This document asserted that the
    MCP host answered `/health` with the table fingerprint. It does not -- `GET /health` is a uvicorn
    404 on the live process, because the host header guards run before routing and only the MCP path
    is registered -- and it called the REST endpoint `/health` when `denoise_service/app.py` defines
    `@app.get("/healthz")`. Both are asserted against the source now, so the sentence cannot come
    back without the route coming back with it.
    """
    readme = read("ops/deploy/README.md")
    app = read("denoise_service/app.py")
    assert '@app.get("/healthz")' in app, \
        "the REST health endpoint moved; the deploy README still names it"
    assert "/healthz" in readme, "the README must name the REST endpoint that exists"
    assert "There is no health route on this process" in readme, (
        "the MCP section has to say plainly that there is no health route: a monitor author assumes "
        "/health, and a 404 from a healthy process reads as an outage")
    assert "`/health` answers 200" not in readme, (
        "this sentence was written from assumption rather than measurement; the live process 404s")
    vhost = read("ops/deploy/nginx-denoise-mcp.conf")
    assert "bounded POST" in vhost, (
        "the vhost header must point at the request that actually proves readiness")


def test_systemd_unit_runs_the_mcp_server_as_the_service_user():
    text = read("ops/deploy/denoise-mcp.service")
    start = re.search(r"^ExecStart=(.+)$", text, flags=re.M).group(1)
    for needle in ["-m denoise_mcp", "--host 127.0.0.1", "--port 8301",
                   "--allowed-host denoise.mcp.metabolomics.us", "--stateless"]:
        assert needle in start, start
    assert "User=denoise" in text and "Group=denoise" in text
    assert "ProtectSystem=strict" in text and "MemoryMax=" in text
    assert "Restart=always" in text
    assert "/opt/denoise-mcp/venv/bin/python" in start, "the MCP venv is its own, Python >= 3.10"


def test_requirements_for_the_mcp_surface_are_separate_and_pinned():
    loose = read("ops/../requirements-mcp.in")
    assert re.search(r"^mcp\s*[=<>]", loose, flags=re.M)
    assert re.search(r"^numpy", loose, flags=re.M)
    lock = read("requirements-mcp.lock")
    pins = {p.lower() for p in re.findall(r"^([A-Za-z0-9_.-]+)==\S+ \\$", lock, flags=re.M)}
    assert {"mcp", "numpy"} <= pins, sorted(pins)
    blocks = re.split(r"^(?=[A-Za-z0-9_.-]+==)", lock, flags=re.M)[1:]
    assert blocks and all("--hash=sha256:" in b for b in blocks)


def test_ci_runs_the_mcp_gate():
    pipeline = read(".woodpecker.yaml")
    assert "name: mcp-gate" in pipeline
    assert "bash ops/ci/test_mcp.sh" in pipeline
    script = read("ops/ci/test_mcp.sh")
    assert "python -m pytest denoise_mcp" in script or "-m pytest denoise_mcp" in script
    assert "--cov-fail-under=80" in script


def test_readme_documents_the_hosted_surface():
    text = read("README.md")
    assert "denoise.mcp.metabolomics.us" in text
    assert "denoise_mcp" in text
    assert "report_progress" in text or "progress" in text.lower()


# ---------------------------------------------------------------- deploy smoke data


def test_the_mcp_smoke_expectation_matches_the_code_in_this_commit():
    """The chemistry layer of the smoke contract: the numbers in `smoke.json` have to be the numbers
    this commit's pure functions produce, so a deploy built from a different commit is caught before
    anyone points a client at it.

    The keys are the wire keys (`expect.summary`, `expect.removed`), because that is what
    `smoke.json` now carries -- see `denoise_mcp/tests/test_deploy_contract.py`, which walks the same
    expectation against a real `--stateless` server over a socket. The library's summary dict agrees
    with the wire's `summary` object field for field, and that agreement is part of what this
    assertion protects: if the two ever disagree, this fails while the integration test passes.
    """
    smoke = json.loads(read("ops/deploy/smoke.json"))
    assert "mcp" in smoke, "ops/deploy/smoke.json needs an mcp block for the MCP deploy"
    mcp = smoke["mcp"]
    expect = mcp["expect"]
    peaks = [(float(m), float(i)) for m, i in mcp["request"]["peaks"]]
    res = clean.clean_spectrum(peaks, mcp["request"]["mode"])
    summary = clean.summary(res)
    for key, want in expect["summary"].items():
        assert summary[key] == want, (key, summary[key], want)
    assert summary["fingerprint"] == smoke["fingerprint"], (
        "the smoke request's own answer must carry the fingerprint the file records")
    assert [r.mz for r in res.removed] == [p["mz"] for p in expect["removed"]]
    for expected, actual in zip(expect["removed"], res.removed):
        assert set(expected["reasons_subset"]) <= set(actual.reasons), (expected, actual.reasons)
        assert expected["nearest_legal_mz"] == pytest.approx(actual.nearest_legal_mz, abs=1e-9)
    # A smoke test that cannot fail proves nothing: both verdicts must occur.
    assert expect["both_verdicts_required"] is True
    assert summary["kept"] > 0 and summary["removed"] > 0
    assert [round(p.mz, 4) for p in res.kept] == expect["kept_with_include_kept"], (
        "the file names the kept masses as they appear under include_kept=true")


def test_the_reason_codebook_is_the_one_the_resource_serves():
    book = json.loads(read("denoise_mcp/reason-codes.json"))
    assert sorted(book["codes"]) == sorted(explain.REASON_CODES)
    assert book["meaning"].keys() == set(explain.REASON_CODES)
    assert book["table_max_mz"] == explain.TABLE_MAX_MZ
