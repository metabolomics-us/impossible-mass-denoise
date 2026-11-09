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
    smoke = json.loads(read("ops/deploy/smoke.json"))
    assert "mcp" in smoke, "ops/deploy/smoke.json needs an mcp block for the MCP deploy"
    mcp = smoke["mcp"]
    peaks = [(float(m), float(i)) for m, i in mcp["request"]["peaks"]]
    res = clean.clean_spectrum(peaks, mcp["request"]["mode"])
    assert clean.summary(res)["kept"] == mcp["expect"]["kept"]
    assert clean.summary(res)["removed"] == mcp["expect"]["removed"]
    assert [r.mz for r in res.removed] == mcp["expect"]["removed_mz"]
    assert [p["mz"] for p in mcp["expect"]["peaks"]] == mcp["expect"]["removed_mz"]
    for expected, actual in zip(mcp["expect"]["peaks"], res.removed):
        assert set(expected["reasons"]) <= set(actual.reasons), (expected, actual.reasons)
        assert expected["nearest_legal_mz"] == pytest.approx(actual.nearest_legal_mz, abs=1e-9)
    # A smoke test that cannot fail proves nothing: both verdicts must occur.
    assert mcp["expect"]["kept"] > 0 and mcp["expect"]["removed"] > 0


def test_the_reason_codebook_is_the_one_the_resource_serves():
    book = json.loads(read("denoise_mcp/reason-codes.json"))
    assert sorted(book["codes"]) == sorted(explain.REASON_CODES)
    assert book["meaning"].keys() == set(explain.REASON_CODES)
    assert book["table_max_mz"] == explain.TABLE_MAX_MZ
