"""Run the MCP server: `python -m denoise_mcp --stdio`, or hosted behind a reverse proxy.

The fingerprint check happens before anything is served. If the tables in this checkout are not the
tables the deployment expects, the process exits non-zero instead of answering with the wrong
chemistry - a service that quietly disagrees with the paper is worse than one that is down.
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
from typing import List, Optional

from .limits import Limits

LOG = logging.getLogger("denoise_mcp")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="denoise_mcp",
        description="MCP server for the impossible-mass metabolomics filter: clean spectra and "
                    "explain every removal. Choose one transport: --stdio, or --host with --port.")
    p.add_argument("--stdio", action="store_true",
                   help="serve over stdin/stdout, for clients that spawn the process")
    p.add_argument("--host", help="interface to bind for streamable HTTP, e.g. 127.0.0.1")
    p.add_argument("--port", type=int, help="TCP port to bind for streamable HTTP")
    p.add_argument("--path", default="/mcp", help="HTTP path of the MCP endpoint (default /mcp)")
    p.add_argument("--stateless", action="store_true",
                   help="no session state between requests; the right setting behind a load balancer")
    p.add_argument("--session-timeout", type=float, default=60.0,
                   help="seconds a stateful session may sit idle before it is dropped")
    p.add_argument("--json-response", action="store_true",
                   help="reply with application/json instead of SSE frames (no streaming)")
    p.add_argument("--allowed-host", action="append", default=[], metavar="HOST[:PORT]",
                   help="Host header to accept; repeatable. Setting this turns on DNS-rebinding "
                        "protection, e.g. --allowed-host denoise.mcp.metabolomics.us")
    p.add_argument("--expect-fingerprint", metavar="HASH",
                   help="refuse to start unless the loaded chemistry tables hash to this value")
    p.add_argument("--cache-clear-seconds", type=float, default=900.0,
                   help="drop memoised verdicts this often (the memo grows ~26 MB per 100k "
                        "distinct masses); 0 turns it off")
    p.add_argument("--max-peaks", type=int, default=Limits().max_peaks)
    p.add_argument("--max-batch", type=int, default=Limits().max_batch)
    p.add_argument("--max-formula-ions", type=int, default=Limits().max_formula_ions,
                   help="cap on formula witnesses per request (each costs ~7.9 ms)")
    p.add_argument("--max-model-ions", type=int, default=Limits().max_model_ions,
                   help="cap on validity-model flags per request (each costs ~3.9 ms)")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def limits_from(args: argparse.Namespace) -> Limits:
    default = Limits()
    return Limits(max_peaks=args.max_peaks, max_batch=args.max_batch,
                  max_formula_ions=args.max_formula_ions, max_model_ions=args.max_model_ions,
                  max_label=default.max_label, max_label_lookups=default.max_label_lookups,
                  max_request_lookups=default.max_request_lookups,
                  max_explanation_work_us=default.max_explanation_work_us)


def _cache_clear_loop(seconds: float, stop: threading.Event) -> None:
    """Clear the memo every `seconds` until `stop` is set - it holds ~26 MB per 100k masses.

    Waiting first means the first clear lands one interval after startup, which is when a busy
    process starts to care; `stop` exists so the loop is testable without killing the process.
    """
    import denoise

    while not stop.wait(seconds):
        before = denoise.cache_size()
        denoise.clear_cache()
        LOG.info("cleared the verdict memo (%d entries released)", before)


def _clear_cache_periodically(seconds: float) -> threading.Event:
    """Start that loop as a daemon thread; return the event that stops it."""
    stop = threading.Event()
    threading.Thread(target=_cache_clear_loop, args=(seconds, stop), name="denoise-cache-clear",
                     daemon=True).start()
    return stop


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        stream=sys.stderr)
    if not args.stdio and not (args.host and args.port):
        print("denoise_mcp: choose a transport - --stdio, or --host with --port", file=sys.stderr)
        return 2

    # Load the tables and check the chemistry before a single request is answered.
    import denoise
    from . import explain
    from .server import build_server, serve

    denoise.is_possible(100.0, "pos")               # forces the tables to load
    got = denoise.fingerprint()
    LOG.info("tables loaded: %s | halogen table: %s | fingerprint %s | ceiling %g Da",
             denoise.table_status(), denoise.halogen_table_status(), got, explain.TABLE_MAX_MZ)
    if args.expect_fingerprint and got != args.expect_fingerprint:
        print("denoise_mcp: refusing to serve - chemistry fingerprint %s is not the expected %s"
              % (got, args.expect_fingerprint), file=sys.stderr)
        return 2

    if args.cache_clear_seconds > 0:
        _clear_cache_periodically(args.cache_clear_seconds)

    server = build_server(limits=limits_from(args))
    if args.stdio:
        LOG.info("serving over stdio")
        server.run("stdio")
        return 0
    if args.allowed_host:
        LOG.info("DNS-rebinding protection on; accepted Hosts: %s", ",".join(args.allowed_host))
    else:
        LOG.warning("no --allowed-host given: DNS-rebinding protection is OFF. Set it for any host "
                    "that is reachable from the internet, e.g. --allowed-host "
                    "denoise.mcp.metabolomics.us")
    LOG.info("serving streamable HTTP on http://%s:%g%s (stateless=%s, json_response=%s)",
             args.host, args.port, args.path, args.stateless, args.json_response)
    serve(server, host=args.host, port=args.port, path=args.path, stateless=args.stateless,
          json_response=args.json_response, session_idle_timeout=args.session_timeout,
          allowed_hosts=args.allowed_host)
    return 0


if __name__ == "__main__":
    sys.exit(main())
