"""An MCP client and server joined in one process.

The two transport tests (`test_stdio_tools.py`, `test_http_hosting.py`) spawn real processes, which
is what proves the wire works. They are the wrong tool for checking what the tools *do*: every
assertion about a verdict, a reason code or a payload shape can be made against the same handlers
through a real client session without a subprocess, which is faster and - because the server runs in
this process - measurable. Both matter: the gate enforces coverage, and code only reachable from a
child process is not covered no matter how many times the child is exercised.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional

import anyio
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from denoise_mcp.server import build_server


@asynccontextmanager
async def in_process(limits=None):
    """Yield an initialized `ClientSession` wired straight to a `denoise_mcp` server.

    `limits` lets a test tighten a budget (e.g. a tiny max_peaks) that would otherwise need a huge
    request to trip.
    """
    # The high-level MCPServer owns a low-level Server; that is the one that speaks over
    # arbitrary streams, which is what an in-process client needs.
    server = build_server(limits=limits)._lowlevel_server
    options = server.create_initialization_options()
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as tg:
            tg.start_soon(server.run, server_streams[0], server_streams[1], options)
            async with ClientSession(*client_streams) as session:
                await session.initialize()
                yield session
            tg.cancel_scope.cancel()


def as_json(result):
    """The structured result, or the parsed text if the server answered without one."""
    import json
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def error_text(result) -> str:
    assert result.is_error, "expected the call to fail: %r" % (result,)
    return result.content[0].text
