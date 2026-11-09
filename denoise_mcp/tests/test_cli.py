"""The command line, without starting a server.

`__main__` mostly exists to be exec'd by `denoise-mcp.service`, and the transport tests do prove it
runs. What they cannot prove cheaply is the part that decides *whether* it runs: the argument rules
(one transport, an allowlist for a public host) and the fingerprint check that refuses to serve the
wrong chemistry. Those are unit tests here; `test_main_refuses_without_a_transport` is the one
behaviour a systemd unit would otherwise discover on its first restart.
"""
from __future__ import annotations

import threading

import denoise
from denoise_mcp.__main__ import _cache_clear_loop, limits_from, main, parse_args
from denoise_mcp.gen_codebook import book, main as write_codebook
from denoise_mcp.limits import Limits


def test_stdio_needs_nothing_else_and_http_needs_both_host_and_port():
    assert parse_args(["--stdio"]).stdio is True
    assert parse_args(["--host", "127.0.0.1", "--port", "8301"]).port == 8301
    assert parse_args(["--stdio"]).host is None


def test_the_http_defaults_are_the_ones_the_unit_file_assumes():
    args = parse_args(["--host", "127.0.0.1", "--port", "8301"])
    assert args.path == "/mcp" and args.stateless is False and args.json_response is False
    assert args.session_timeout == 60.0
    assert args.allowed_host == []            # protection stays off until a host is named
    assert parse_args(["--allowed-host", "a.example", "--allowed-host", "b.example:8301"]).allowed_host \
        == ["a.example", "b.example:8301"]


def test_the_limits_a_deployment_sets_are_the_limits_the_server_gets():
    args = parse_args(["--stdio", "--max-peaks", "1200", "--max-batch", "50",
                       "--max-formula-ions", "10", "--max-model-ions", "20"])
    lim = limits_from(args)
    assert (lim.max_peaks, lim.max_batch) == (1200, 50)
    assert (lim.max_formula_ions, lim.max_model_ions) == (10, 20)
    # Everything not exposed on the command line keeps the shipped value, so a deployment cannot
    # quietly change the chemistry budget while tuning a size limit.
    default = Limits()
    assert lim.max_label == default.max_label
    assert lim.max_request_lookups == default.max_request_lookups
    assert lim.max_explanation_work_us == default.max_explanation_work_us


def test_main_refuses_without_a_transport_rather_than_listening_on_nothing():
    assert main(["--log-level", "ERROR"]) == 2


def test_main_refuses_to_serve_the_wrong_chemistry(capfd):
    got = denoise.fingerprint()
    assert main(["--stdio", "--log-level", "ERROR", "--expect-fingerprint", "0" * 16]) == 2
    text = capfd.readouterr().err
    assert "refusing to serve" in text and got in text and "0" * 16 in text


def test_the_cache_clearer_releases_the_memo_when_told_to_stop():
    import time

    denoise.is_possible(100.0, "pos")          # one verdict, so the memo has something in it
    assert denoise.cache_size() >= 1
    stop = threading.Event()
    thread = threading.Thread(target=_cache_clear_loop, args=(0.01, stop), daemon=True)
    thread.start()
    for _ in range(1000):                      # it clears on the first interval; this is not a race
        if denoise.cache_size() == 0:
            break
        time.sleep(0.005)
    stop.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert denoise.cache_size() == 0, "the loop ran and never cleared"


def test_the_published_codebook_is_the_one_the_generator_writes():
    import json
    from pathlib import Path

    assert write_codebook() == 0
    on_disk = json.loads(Path(__file__).resolve().parents[1].joinpath("reason-codes.json")
                         .read_text())
    assert on_disk == json.loads(json.dumps(book()))
