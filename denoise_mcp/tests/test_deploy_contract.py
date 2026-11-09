"""Integration layer: the deploy contract, checked against a real server on a real socket.

`ops/deploy/smoke.json` is the handoff artefact. Everything else in this repository is advice;
that file is what whoever deploys pastes into curl, and what `ops/deploy/README.md` tells them to
trust. So the `mcp` block has to describe the bytes the endpoint actually puts on the wire, not the
dict the library returns internally -- they are not the same object.

The library's summary is `{kept: 2, removed: 2, peaks: [...]}`. What `tools/call clean_spectrum`
returns as `structuredContent` is

    {summary: {total, kept, removed, intensity_*, fingerprint, table_max_mz, ...},
     removed: [{mz, intensity, reasons, narrative, nearest_legal_mz, nearest_legal_mDa}, ...],
     kept: null,                       <- only with include_kept=true, and the answer says so
     kept_omitted: "include_kept=true would add 2 peaks (~74 bytes as JSON)",
     kept_truncated: null, removed_truncated: null,
     answer_bytes: 1438, answer_oversized: null}

Written down here from the live public endpoint on 2026-11-08:

    $ curl -sS -X POST https://denoise.mcp.metabolomics.us/mcp \\
        -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \\
        --data '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":
                 {"name":"clean_spectrum","arguments":{"mode":"pos",
                   "peaks":[[181.0707,100],[100.5,20],[123.9,5],[85.0284,40]]}}}'
    ... "summary":{...,"kept":2,"removed":2,...},"kept":null,
        "kept_omitted":"include_kept=true would add 2 peaks (~74 bytes as JSON)",
        "answer_bytes":1438

`kept` is null on the default path -- keeping the kept peaks is opt-in, because the kept list is the
part of an answer a caller rarely needs and a large one is what overflows a harness. A deploy check
that asked `jq .kept` would read `null` and report the service as broken.

Nothing here is mocked: the server runs as `python -m denoise_mcp --stateless`, exactly as
`ops/deploy/denoise-mcp.service` starts it, and the request goes over a socket as JSON-RPC.
"""
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PUBLIC_HOST = "denoise.mcp.metabolomics.us"
SMOKE = json.loads((ROOT / "ops" / "deploy" / "smoke.json").read_text())


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    """Started the way the unit starts it: `python -m denoise_mcp --stateless --allowed-host ...`."""

    def __init__(self):
        self.port = _free_port()
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "denoise_mcp", "--host", "127.0.0.1", "--port", str(self.port),
             "--path", "/mcp", "--allowed-host", PUBLIC_HOST, "--allowed-host", "127.0.0.1:*",
             "--stateless"],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def wait_ready(self, timeout=90.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError("server exited early:\n%s" % self.proc.stdout.read())
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1.0):
                    return
            except OSError:
                time.sleep(0.2)
        self.proc.kill()
        raise AssertionError("server never listened on %d" % self.port)

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=15)

    def rpc(self, payload, read_timeout=60.0):
        """One JSON-RPC POST, answered as an SSE frame; returns the decoded JSON-RPC message."""
        body = json.dumps(payload).encode()
        request = (b"POST /mcp HTTP/1.1\r\nHost: %s\r\nContent-Type: application/json\r\n"
                   b"Accept: application/json, text/event-stream\r\nContent-Length: %d\r\n"
                   b"Connection: close\r\n\r\n" % (PUBLIC_HOST.encode(), len(body))) + body
        with socket.create_connection(("127.0.0.1", self.port), timeout=read_timeout) as sock:
            sock.sendall(request)
            text = ""
            while True:
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    break
                if not chunk:
                    break
                text += chunk.decode("utf-8", "replace")
                # An SSE event ends at a blank line; stop once a whole data frame has arrived, so a
                # trailing keep-alive comment cannot hold this call open for the full timeout.
                if "data: " in text and "\n\n" in text[text.index("data: "):]:
                    break
        for line in text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
        raise AssertionError("no SSE data frame in response:\n%s" % text[:800])


@pytest.fixture(scope="module")
def deployed_server():
    server = _Server()
    server.wait_ready()
    yield server
    server.stop()


def _structured(server, tool, arguments):
    reply = server.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": tool, "arguments": arguments}})
    assert "error" not in reply, reply
    return reply["result"]["structuredContent"]


# ------------------------------------------------------------------ the tool list the deploy page promises
def test_the_endpoint_offers_the_four_tools_the_deploy_docs_count(deployed_server):
    """`ops/deploy/README.md` says a `tools/list` answer "names four tools". The count is only worth
    anything if the names are the documented ones -- `clean_spectrum`, `clean_batch`, `explain_ion`,
    `service_info`, which is what `denoise_service/static/mcp.html` tabulates and what the live
    endpoint answered on 2026-11-08."""
    names = sorted(t["name"] for t in
                   deployed_server.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                                        "params": {}})["result"]["tools"])
    assert names == ["clean_batch", "clean_spectrum", "explain_ion", "service_info"], names
    assert len(names) == 4


# ------------------------------------------------------------------ the smoke contract, on the wire
def test_smoke_json_describes_the_answer_the_tool_actually_returns(deployed_server):
    """The `mcp.expect` block has to be walkable against `structuredContent`, key for key.

    It used to be written against the library's own summary dict (`kept`, `kept_mz`, `removed`,
    `removed_mz`, `peaks`). The tool wraps that summary one level down under `summary`, renames the
    per-peak list to `removed`, and leaves `kept` null unless `include_kept` is set -- so the file an
    operator is told to check produced `jq .kept -> null` on a healthy service.
    """
    expect = SMOKE["mcp"]["expect"]
    got = _structured(deployed_server, "clean_spectrum", SMOKE["mcp"]["request"])

    for key, want in expect["summary"].items():
        assert got["summary"][key] == want, (key, got["summary"][key], want)
    assert got["summary"]["fingerprint"] == SMOKE["fingerprint"], (
        "the answer must carry the fingerprint smoke.json says this build has")
    assert expect["both_verdicts_required"] is True
    assert got["summary"]["kept"] > 0 and got["summary"]["removed"] > 0, (
        "a smoke test that cannot fail proves nothing: both verdicts have to occur")

    assert got["kept"] == expect["kept_without_include_kept"], (
        "the default answer omits the kept peaks; if that changed, the deploy README and the guide "
        "page both need rewording")
    assert "include_kept" in got["kept_omitted"], got["kept_omitted"]

    assert [p["mz"] for p in got["removed"]] == [p["mz"] for p in expect["removed"]], got["removed"]
    for want, actual in zip(expect["removed"], got["removed"]):
        assert set(want["reasons_subset"]) <= set(actual["reasons"]), (want, actual["reasons"])
        assert actual["nearest_legal_mz"] == pytest.approx(want["nearest_legal_mz"], abs=1e-9)
    assert got["answer_bytes"] > 0 and got["answer_oversized"] is None, (
        "the answer carries its own size; on a smoke request it must fit")


def test_include_kept_adds_the_kept_peaks_the_smoke_file_names(deployed_server):
    """`include_kept` is the switch that turns the null `kept` into the list, so the smoke file names
    the kept masses under that switch rather than pretending they arrive by default."""
    arguments = dict(SMOKE["mcp"]["request"])
    arguments["include_kept"] = True
    got = _structured(deployed_server, "clean_spectrum", arguments)
    assert [round(p["mz"], 4) for p in got["kept"]] == SMOKE["mcp"]["expect"]["kept_with_include_kept"]
    assert got["kept_omitted"] is None


def test_service_info_answers_the_fingerprint_the_deploy_handoff_records(deployed_server):
    """Readiness for this service is a bounded POST, and `service_info` is the one that proves the
    right chemistry tables are loaded -- `ops/deploy/README.md` sends it for exactly that."""
    got = _structured(deployed_server, "service_info", {})
    assert got["fingerprint"] == SMOKE["fingerprint"], got["fingerprint"]
    assert got["protocol"] == SMOKE["mcp"]["protocol"], got["protocol"]
    assert sorted(got["tools"]) == ["clean_batch", "clean_spectrum", "explain_ion", "service_info"]
    assert got["limits"], "limits are what tell a caller its request will be rejected"
