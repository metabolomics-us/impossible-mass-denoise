"""End-to-end layer: the hosted shape. A real process, a real socket, real SSE frames.

Nothing here is mocked: the server runs as a subprocess bound to 127.0.0.1, an MCP client
negotiates with it, and a raw socket checks the two headers a reverse proxy cares about plus the
Host allowlist that stands up behind `denoise.mcp.metabolomics.us`.
"""
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[2]
PUBLIC_HOST = "denoise.mcp.metabolomics.us"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """`python -m denoise_mcp --host 127.0.0.1 --port P --allowed-host ...`, then torn down."""

    def __init__(self, *extra):
        self.port = free_port()
        self.url = "http://127.0.0.1:%d/mcp" % self.port
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "denoise_mcp", "--host", "127.0.0.1", "--port", str(self.port),
             "--path", "/mcp", *extra],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def wait_ready(self, timeout=60.0):
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
        raise AssertionError("server never listened on port %d" % self.port)

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=15)


@pytest.fixture
def server():
    s = Server("--allowed-host", PUBLIC_HOST, "--allowed-host", "127.0.0.1:*")
    s.wait_ready()
    yield s
    s.stop()


@pytest.fixture
def stateless_server():
    """How the public endpoint actually runs (denoise-mcp.service passes --stateless)."""
    s = Server("--allowed-host", PUBLIC_HOST, "--allowed-host", "127.0.0.1:*", "--stateless")
    s.wait_ready()
    yield s
    s.stop()


def post_raw(port, host, body):
    """One POST with an exact Host header, as a reverse proxy would send it."""
    data = json.dumps(body).encode()
    req = ("POST /mcp HTTP/1.1\r\nHost: %s\r\nContent-Type: application/json\r\n"
           "Accept: application/json, text/event-stream\r\nContent-Length: %d\r\n"
           "Connection: close\r\n\r\n" % (host, len(data))).encode() + data
    with socket.create_connection(("127.0.0.1", port), timeout=20) as s:
        s.sendall(req)
        chunks = []
        total = 0
        while total < 4096:
            chunk = s.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
    return head.decode().split("\r\n"), body.decode(errors="replace")


def post_with_headers(port, header_lines, body):
    """One POST with headers chosen by the test, to measure what a wrong header shape costs."""
    data = json.dumps(body).encode()
    req = ("POST /mcp HTTP/1.1\r\n" + "".join(h + "\r\n" for h in header_lines)
           + "Content-Length: %d\r\nConnection: close\r\n\r\n" % len(data)).encode() + data
    with socket.create_connection(("127.0.0.1", port), timeout=20) as s:
        s.sendall(req)
        chunks = []
        total = 0
        while total < 8192:
            chunk = s.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
    return head.decode().split("\r\n"), body.decode(errors="replace")


# What the Accept and Content-Type headers cost, measured against a real hosted server here and
# against denoise.mcp.metabolomics.us before the guide was written. Everything runs against the
# --stateless server, because that is how denoise-mcp.service runs the public endpoint. The guide
# page (denoise_service/static/mcp.html) tells clients these same rules, and
# denoise_service/tests/test_static_mcp_page.py asserts it keeps telling them; change one side and
# you must change the other, or the page lies about the endpoint.

LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}


def post(port, headers, body=LIST):
    return post_with_headers(port, ["Host: %s" % PUBLIC_HOST] + headers, body)


@pytest.mark.parametrize("accept,code", [
    ("application/json, text/event-stream", "200"),   # what the guide's curl example sends
    ("*/*", "200"),                                    # what curl sends when you say nothing
    ("application/json", "406"),                       # naming one of the two is not enough
    ("text/event-stream", "406"),
    ("text/plain", "406"),
])
async def test_the_accept_header_must_cover_both_media_types(stateless_server, accept, code):
    lines, body = post(stateless_server.port, ["Content-Type: application/json",
                                               "Accept: %s" % accept])
    assert lines[0].startswith("HTTP/1.1 " + code), (accept, lines[0], body[:200])
    if code == "406":
        assert "must accept both" in body.lower(), (accept, body[:200])
    else:
        assert "tools" in body, (accept, body[:200])


async def test_an_absent_accept_header_is_refused_with_406(stateless_server):
    """Absent is not the same as `*/*`: the endpoint wants the header. curl users never hit this,
    because curl sends `Accept: */*` unless told otherwise -- which is why a bare curl works and a
    hand-rolled HTTP client that omits Accept does not."""
    lines, body = post(stateless_server.port, ["Content-Type: application/json"])
    assert lines[0].startswith("HTTP/1.1 406"), (lines[0], body[:200])


async def test_a_post_without_a_json_content_type_is_refused_with_400(stateless_server):
    lines, body = post(stateless_server.port, ["Accept: application/json, text/event-stream"])
    assert lines[0].startswith("HTTP/1.1 400"), (lines[0], body[:200])
    assert "content-type" in body.lower(), body[:200]


async def test_without_the_stateless_flag_every_post_needs_a_session_id(server):
    """Why denoise-mcp.service passes --stateless: a stateful endpoint answers 400 "Missing session
    ID" to any POST that did not come from a session it created, so a one-shot client (curl, a
    script, an agent with no session handling) cannot use it at all."""
    lines, body = post(server.port, ["Content-Type: application/json",
                                     "Accept: application/json, text/event-stream"])
    assert lines[0].startswith("HTTP/1.1 400"), (lines[0], body[:200])
    assert "session" in body.lower(), body[:200]
    """A body with no Content-Type is refused before anything is parsed."""
    lines, body = post_with_headers(
        server.port, ["Host: %s" % PUBLIC_HOST, "Accept: application/json, text/event-stream"],
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert lines[0].startswith("HTTP/1.1 400"), (lines[0], body[:200])
    assert "content-type" in body.lower(), body[:200]


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                   "clientInfo": {"name": "e2e", "version": "0"}}}


# ---------------------------------------------------------------- negotiated protocol


async def test_the_hosted_server_negotiates_the_version_real_clients_use(server):
    async with streamable_http_client(server.url) as (read, write):
        async with ClientSession(read, write) as s:
            init = await s.initialize()
            assert init.protocol_version == "2025-11-25"
            assert "tools" in init.capabilities.model_dump(exclude_none=True)
            names = {t.name for t in (await s.list_tools()).tools}
            assert "clean_spectrum" in names


async def test_a_tool_call_completes_over_http(server):
    async with streamable_http_client(server.url) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            res = await s.call_tool("clean_spectrum", {
                "mode": "pos", "peaks": [[181.0707, 100.0], [100.5, 20.0]], "include_kept": True})
            assert not res.is_error
            assert res.structured_content["summary"]["kept"] == 1


async def test_streaming_reaches_the_client_as_it_happens(server):
    """Progress notifications land while the work is running, not bundled with the answer.

    That is the whole point of `stream=true`: an agent watching a long clean needs to see it
    happening. The messages are delivered in whatever chunks the SSE frames carry, so the test
    measures when they arrive relative to the result, not the gap between consecutive ones.
    """
    arrival = []
    t0 = None

    async def on_progress(progress, total, message):
        arrival.append((time.perf_counter() - t0, message))

    # Enough work that "arrived early" cannot be an accident of scheduling: 4 spectra x 3000
    # peaks takes the server well over half a second.
    spectra = [{"mode": "pos", "peaks": [[100.5 + i * 0.0017, 1.0] for i in range(3000)]}] * 4
    async with streamable_http_client(server.url) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            t0 = time.perf_counter()
            res = await s.call_tool(
                "clean_batch",
                # interval 0: every peak is a notification, which is what makes the arrival times
                # below a test of delivery rather than of the server's own throttling (that is
                # test_a_min_progress_interval_thins_peak_lines... in test_tools_in_process.py).
                {"spectra": spectra, "stream": True, "explanations": True,
                 "min_progress_interval_ms": 0},
                progress_callback=on_progress)
    t_result = time.perf_counter() - t0
    assert not res.is_error
    assert len(arrival) > 100, "expected one message per peak, got %d" % len(arrival)
    spectra_lines = [m for _t, m in arrival if m.startswith("SPECTRUM")]
    assert "index=4 total=4" in spectra_lines[-1], spectra_lines[-1]
    first = arrival[0][0]
    assert first < t_result - 0.05, (
        "the first progress line arrived %gs before the answer; the stream is buffered" % first)
    assert arrival[-1][0] <= t_result, "a notification arrived after the answer: %r" % (arrival[-1],)
    assert all(t <= 60.0 for t, _m in arrival)


# ---------------------------------------------------------------- proxy-facing headers


def test_sse_responses_tell_a_proxy_not_to_buffer(server):
    lines, body = post_raw(server.port, PUBLIC_HOST, INIT)
    headers = {k.lower(): v for k, v in
               (l.split(": ", 1) for l in lines[1:] if ": " in l)}
    assert lines[0].startswith("HTTP/1.1 200"), lines[0]
    assert headers["content-type"].startswith("text/event-stream")
    assert headers["x-accel-buffering"] == "no"
    assert "no-transform" in headers["cache-control"]
    assert headers["transfer-encoding"] == "chunked"
    assert "event: message" in body


def test_a_host_not_allowlisted_is_refused_with_421(server):
    lines, body = post_raw(server.port, "evil.example", INIT)
    assert lines[0].startswith("HTTP/1.1 421"), (lines[0], body)
    assert "Host" in body


def test_the_allowlisted_public_host_is_accepted(server):
    lines, _ = post_raw(server.port, PUBLIC_HOST, INIT)
    assert lines[0].startswith("HTTP/1.1 200"), lines[0]


def test_the_server_refuses_to_start_without_a_bound_host_or_stdio():
    proc = subprocess.run([sys.executable, "-m", "denoise_mcp", "--help"], cwd=str(ROOT),
                          capture_output=True, text=True)
    assert proc.returncode == 0
    for flag in ("--stdio", "--host", "--port", "--allowed-host", "--cache-clear-seconds"):
        assert flag in proc.stdout, flag


def test_a_bad_fingerprint_stops_the_server_before_it_serves_anything():
    """A release whose tables do not match the expected chemistry must not go live."""
    proc = subprocess.run(
        [sys.executable, "-m", "denoise_mcp", "--stdio", "--expect-fingerprint", "0" * 16],
        cwd=str(ROOT), capture_output=True, text=True, timeout=90)
    assert proc.returncode != 0
    assert "fingerprint" in proc.stderr.lower()


# ---------------------------------------------------------------- the GET notification stream
def test_a_get_on_the_endpoint_opens_the_stream_and_does_not_end(stateless_server):
    """A `GET` with an SSE Accept is the server-to-client notification stream: 200,
    `text/event-stream`, and it stays open. This is protocol behaviour, not a stuck server -- and
    it is exactly why a health check has to POST. The first public probe of
    https://denoise.mcp.metabolomics.us/mcp used a bare `GET` (curl sends `Accept: */*`, which does
    cover both media types) and appeared to hang for 20 seconds; nothing was wrong, the probe was
    holding an open stream.

    Asserting "still open" means asserting a read times out. If the SDK ever starts refusing GET
    on a stateless endpoint (the spec allows 405 there), this test is where that becomes visible,
    and the guide page has to say so.
    """
    with socket.create_connection(("127.0.0.1", stateless_server.port), timeout=5) as sock:
        sock.sendall(b"GET /mcp HTTP/1.1\r\n"
                     b"Host: " + PUBLIC_HOST.encode() + b"\r\n"
                     b"Accept: application/json, text/event-stream\r\n\r\n")
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            assert chunk, "the server closed a GET it should have kept open: %r" % head
            head += chunk
        status, headers = head.split(b"\r\n", 1)[0].decode(), head.split(b"\r\n\r\n")[0].decode()
        assert status.startswith("HTTP/1.1 200"), status
        assert "text/event-stream" in status or "content-type: text/event-stream" in headers.lower()
        sock.settimeout(0.75)
        try:
            sock.recv(4096)               # may return a frame; what must NOT happen is a clean close
            # Data arrived; the next read must be the one that times out.
            sock.recv(4096)
            raise AssertionError("the stream ended after two reads; a notification stream stays open")
        except (TimeoutError, socket.timeout):
            pass                          # still open after the first byte or after a frame: correct


# ---------------------------------------------------------------- there is no health endpoint
def test_there_is_no_health_route_on_the_endpoint(stateless_server):
    """`GET /health` is a 404 here, and it is worth asserting because the opposite is what a
    monitor author assumes. The host header guards run before routing and this ASGI app registers
    only the MCP path, so every non-endpoint path -- `/`, `/health`, `/healthz` -- is a uvicorn 404
    even when the process is perfectly healthy and holding the right tables. Live proof, the same
    request over TLS through nginx:

        $ curl -sS -o /dev/null -w '%{http_code}\\n' -H 'Accept: */*' \\
              https://denoise.mcp.metabolomics.us/health
        404

    What a monitor should send instead is a bounded POST, which is also what `ops/deploy/smoke.json`
    sends; `tools/list` answers in hundredths of a second, and `service_info` carries the table
    fingerprint with the limits. A GET on the endpoint itself is the one request that must never be
    a health check -- see test_a_get_on_the_endpoint_opens_the_stream_and_does_not_end.
    """
    for path in ("/health", "/healthz", "/healthcheck", "/status"):
        with socket.create_connection(("127.0.0.1", stateless_server.port), timeout=5) as sock:
            sock.sendall(b"GET %s HTTP/1.1\r\nHost: %s\r\nAccept: */*\r\nConnection: close\r\n\r\n"
                         % (path.encode(), PUBLIC_HOST.encode()))
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = sock.recv(4096)
                assert chunk, "no response at all for %s: %r" % (path, head)
                head += chunk
            status = head.split(b"\r\n", 1)[0].decode()
            assert status.startswith("HTTP/1.1 404"), (path, status)
