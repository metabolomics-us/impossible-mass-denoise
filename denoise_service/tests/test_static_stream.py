"""The streaming pipeline in lib.js (splitter, packer, reader, scheduler, output), run under node
against real Blobs, real HTTP servers and the real service. No fakes."""
import json
import random
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from denoise_service import convert as cv

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def node(check, arg=None, timeout=120):
    out = subprocess.run(["node", str(HERE / "stream_checks.js"), check, json.dumps(arg)],
                         capture_output=True, text=True, timeout=timeout)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def many_msp(n, crlf=False, tail_newline=False):
    nl = "\r\n" if crlf else "\n"
    parts = []
    for i in range(n):
        parts.append(nl.join(["Name: rec%d" % i, "IonMode: Positive", "Num Peaks: 2",
                              "181.0707 100", "100.5 20", "", ""]))
    text = "".join(parts)
    return text if tail_newline else text.rstrip("\r\n")


def raws(text):
    kind = cv.detect_format(text.lstrip("﻿"))
    return kind, [r.raw for r in cv.split_records(text.lstrip("﻿"), kind)]


BIG_RECORD = "Name: big\nIonMode: Positive\nNum Peaks: 400\n" + "\n".join(
    "%d.%04d %d" % (100 + i, i, i + 1) for i in range(400)) + "\n"

SAMPLES = {
    "msp-lf": many_msp(30, tail_newline=True),
    "msp-crlf": many_msp(30, crlf=True, tail_newline=True),
    "msp-no-final-newline": many_msp(12),
    "msp-multibyte": "Name: café µg € \U0001f9ea\nIonMode: Positive\nNum Peaks: 1\n100.5 1\n\n"
                     "Name: 日本語\nIonMode: Positive\nNum Peaks: 1\n181.0707 2\n",
    "msp-bom": "﻿" + many_msp(8),
    "msp-big-record": many_msp(3) + "\n\n" + BIG_RECORD + "\n" + many_msp(3),
    "mgf": (FIX / "sample.mgf").read_text() * 6,
    "mgf-crlf": (FIX / "sample.mgf").read_text().replace("\n", "\r\n") * 4,
    "strings": (FIX / "sample.strings.txt").read_text() * 8,
    "strings-blank-runs": "1:2 3:4\n\n\n5:6\r\n\r\n\r\n7:8 9:1\n",
    "sample-msp": (FIX / "sample.msp").read_text(),
    "lone-cr": "Name: a\rNum Peaks: 1\r100.5 1\r\rName: b\rNum Peaks: 1\r85.0284 2\r",
    "blank-lead": "\n\n\nName: a\n1 2\n",
    "no-records": "\n\n  \n",
}


@needs_node
@pytest.mark.parametrize("label", sorted(SAMPLES))
def test_splitter_random_slices_equal_the_servers_records(label):
    text = SAMPLES[label]
    _, expected = raws(text)
    got = node("splitter_slices", {"text": text, "expected": expected, "rounds": 150, "seed": 11})
    assert got["bad"] == [], label
    assert got["count"] == len(expected)


@needs_node
@pytest.mark.parametrize("label", ["msp-multibyte", "msp-bom", "msp-big-record", "msp-crlf", "mgf"])
def test_blob_reader_slices_through_real_bytes_split_mid_character(label):
    text = SAMPLES[label]
    _, expected = raws(text)
    decoded = text.lstrip("﻿")   # TextDecoder drops a BOM at the start of the file
    results = node("blob_reader", {"text": text, "expected": expected, "decoded": decoded})
    assert len(results) == 12
    assert all(r["ok"] and r["same"] and r["read"] for r in results), results


@needs_node
def test_blob_reader_keeps_astral_characters_whole():
    # the test euro/flask characters above are 3 and 4 bytes; slices of 1-5 bytes cut every one
    assert "\U0001f9ea" in SAMPLES["msp-multibyte"] and "€" in SAMPLES["msp-multibyte"]


@needs_node
def test_packer_matches_pack_ranges_and_counts_bytes():
    for label in ("msp-lf", "msp-multibyte", "msp-big-record", "strings"):
        for mb, mr in ((300, 3), (400000, 100), (1, 1), (2000, 7)):
            r = node("packer", {"text": SAMPLES[label], "maxBytes": mb, "maxRecords": mr})
            assert r["same"] and r["bytesOk"] and r["n"] >= 1, (label, mb, mr)


@needs_node
def test_byte_len_matches_utf8():
    rnd = random.Random(3)
    alphabet = "ab \né€\U0001f9eaࠀ߿\u0080z"
    cases = ["", "plain", "퟿"] + ["".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 30))) for _ in range(200)]
    for js_len, py_len in node("byte_len", cases):
        assert js_len == py_len


@needs_node
def test_scheduler_orders_results_limits_concurrency_and_applies_backpressure():
    r = node("ordering", {"records": 400, "maxRecords": 5})
    assert r["res"]["completed"] and r["inOrder"] and r["n"] == 80
    assert r["res"]["recordsDone"] == 400
    assert r["maxActive"] <= 3, r
    # reading paused while requests waited: the queue never grew past the limit by more than the
    # requests one small piece can add, and the file was far from fully read at the first result
    assert r["maxBacklog"] <= r["maxQueued"] + 8, r   # 8: requests one 2 KB piece can add
    assert r["maxBacklog"] < 40, r
    assert r["firstResultRead"] < r["total"] / 2, r


@needs_node
def test_failed_request_stops_the_run_and_keeps_the_ordered_prefix():
    r = node("failure", {"records": 200, "failOn": 6})
    assert r["res"]["error"]["message"] == "boom"
    assert not r["res"]["completed"] and not r["res"]["readDone"]
    assert r["contiguous"] and 1 <= len(r["got"]) <= 6, r
    assert r["res"]["recordsDone"] == 5 * len(r["got"])


@needs_node
def test_busy_server_is_retried_after_retry_after():
    r = node("retry_after", {"records": 40})
    assert r["res"]["completed"] and r["got"] == 8
    assert r["ms"] >= 900 and any("retrying in 1 s" in n for n in r["notes"])


@needs_node
def test_413_halves_and_a_lone_oversized_record_passes_through():
    r = node("halving", {"records": 30, "maxText": 130})   # one record is ~70 chars, two are too big
    assert r["res"]["completed"] and r["passed"] == 0
    assert r["text"] == r["upper"] and r["posts"] > 30     # halved down to single records
    r2 = node("halving", {"records": 10, "maxText": 20})   # even one record is refused
    assert r2["res"]["completed"] and r2["passed"] == 10
    assert r2["text"] == r2["orig"]                         # passed through verbatim, not converted


@needs_node
def test_cancel_stops_reading_and_dispatch():
    r = node("cancel", {"records": 400})
    assert r["res"]["cancelled"] and r["res"]["error"] is None and not r["res"]["completed"]
    assert r["got"] == [0, 1, 2] or r["got"][:3] == [0, 1, 2]
    assert not r["readAll"]


@needs_node
def test_output_sink_failure_stops_the_run_with_its_message():
    r = node("sink_failure")
    assert r["error"]["message"] == "disk full" and not r["completed"]


@needs_node
def test_output_blob_folds_parts_and_writable_streams(tmp_path):
    r = node("output_blob")
    assert r["same"] and r["size"]
    w = node("output_writable", {"dir": str(tmp_path)})
    assert w["blob"] is None and w["same"] and w["size"] > 0


@needs_node
def test_accumulator_bounds_detail_but_counts_everything():
    r = node("accumulator")
    assert r["total"] == 34
    # the first 5 records, then error records until the extra-error cap of 5 is used up
    assert r["kept"] == [0, 1, 2, 3, 4, 5, 12, 14, 17, 19]
    assert r["errors"] == {"no ion mode": 6}   # counted for every record, kept or not
    assert r["summary"] == {"records": 34, "cleaned": 28, "passed_through": 6, "peaks": 68, "impossible": 34}
    assert r["format"] == "msp" and r["raws"] == ["r0", "r1"]


@needs_node
def test_meter_announcer_and_formatting():
    r = node("meter")
    assert 0.9e6 < r["steady"] < 1.1e6
    assert r["faster"] > r["steady"] and r["faster"] < 3.1e6   # smoothed: rises toward 3 MB/s
    assert r["none"] is None and r["eta"] == pytest.approx(10e6 / r["faster"])
    assert len(r["fired"]) <= 8 and all(b - a >= 8000 for a, b in zip(r["fired"], r["fired"][1:]))
    assert r["fmt"] == ["0 B", "1.50 KB", "5.00 MB", "1.00 GB"]
    assert r["dur"] == ["5 s", "2 min 05 s", "1 h 02 min", "estimating..."]


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def service(tmp_path_factory):
    port = _free_port()
    cfg = tmp_path_factory.mktemp("cfg") / "c.yaml"
    cfg.write_text("port: %d\nrate_per_minute: 100000\nrate_burst: 1000\n" % port)
    proc = subprocess.Popen([sys.executable, "-m", "denoise_service", "--config", str(cfg)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = "http://127.0.0.1:%d" % port
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(url + "/healthz", timeout=1).read()
                break
            except OSError:
                time.sleep(0.2)
        else:
            raise RuntimeError("service did not start")
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@needs_node
def test_pipeline_against_the_real_service_equals_sequential_chunk_output(service, tmp_path):
    f = tmp_path / "big.msp"
    f.write_text(many_msp(450, tail_newline=True) + "\n" + (FIX / "sample.msp").read_text(), encoding="utf-8")
    r = node("service", {"url": service, "file": str(f), "sliceBytes": 20000})
    assert r["res"]["completed"] and r["equal"], r
    assert r["records"] == r["expectedRecords"] == 452
    assert r["res"]["recordsDone"] == 452 and r["retained"] == 452
    assert r["summary"]["records"] == 452
