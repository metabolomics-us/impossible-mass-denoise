"""lib.js (pure page helpers) under node, plus structural checks on the page that load it."""
import json
import random
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from denoise_service import convert as cv

STATIC = Path(__file__).resolve().parent.parent / "static"
FIX = Path(__file__).resolve().parent / "fixtures"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def js(expr, payload=None):
    """Evaluate a JS expression with `L` (lib.js) and `P` (the JSON payload); return its JSON."""
    script = ("const L=require(%s);const P=JSON.parse(require('fs').readFileSync(0,'utf8'));"
              "process.stdout.write(JSON.stringify(%s));" % (json.dumps(str(STATIC / "lib.js")), expr))
    out = subprocess.run(["node", "-e", script], input=json.dumps(payload), capture_output=True,
                         text=True, check=True)
    return json.loads(out.stdout)


def many_msp(n, crlf=False):
    nl = "\r\n" if crlf else "\n"
    parts = []
    for i in range(n):
        parts.append(nl.join(["Name: rec%d" % i, "IonMode: Positive", "Num Peaks: 2",
                              "181.0707 100", "100.5 20", "", ""]))
    return "".join(parts).rstrip("\r\n")


@needs_node
@pytest.mark.parametrize("crlf", [False, True])
def test_chunks_never_split_a_record(crlf):
    text = many_msp(25, crlf)
    chunks = js("L.splitIntoChunks(P, 400, 4)", text)
    assert len(chunks) > 3
    for c in chunks:
        assert c.startswith("Name: rec")
        assert len(cv.split_records(c, "msp")) <= 4
    strings = "\n".join("181.0707:100 100.5:20 85.0284:%d" % i for i in range(30))
    chunks = js("L.splitIntoChunks(P, 200, 100)", strings)
    assert len(chunks) > 3 and all(len(c.strip().split("\n")) >= 1 for c in chunks)
    assert all(not c.startswith("\n") for c in chunks)


@needs_node
def test_chunks_concatenate_to_input():
    texts = [many_msp(40), many_msp(7, True), (FIX / "sample.mgf").read_text() * 9,
             "\n\n" + many_msp(5) + "\n\n\n", (FIX / "sample.strings.txt").read_text(),
             "181.0707 100\n100.5 20\n", "   \n", ""]
    for t in texts:
        for mb, mr in ((300, 3), (10 ** 6, 100), (1, 1)):
            assert "".join(js("L.splitIntoChunks(P[0], P[1], P[2])", [t, mb, mr])) == t


@needs_node
def test_js_records_match_python_split_records():
    rnd = random.Random(7)
    samples = [(FIX / "sample.msp").read_text(), (FIX / "sample.mgf").read_text(),
               (FIX / "sample.strings.txt").read_text(), many_msp(6, True), "100 1\n200 2\n",
               "Name: a\n1 2\n3 4\nComment: x\n\n\nName: b\nNum Peaks: 0\n\nName: c\n5 6",
               "\n\nBEGIN IONS\nTITLE=a\n1 2\nEND IONS\n# tail\nBEGIN IONS\n3 4\nEND IONS"]
    for _ in range(20):
        lines = []
        for _ in range(rnd.randint(1, 12)):
            lines.append(rnd.choice(["Name: n", "Num Peaks: 1", "181.0707 100", "", "x:1 y", "5:6 7:8"]))
        samples.append("\n".join(lines) + rnd.choice(["", "\n"]))
    for t in samples:
        kind = js("L.detectKind(P)", t)
        assert kind == cv.detect_format(t), t
        py = [r.raw for r in cv.split_records(t, kind)]
        assert js("L.splitRecords(P[0], P[1])", [t, kind]) == py, (kind, t)


@needs_node
def test_filename_for_cleaned_download():
    f = lambda n, fmt: js("L.cleanedFilename(P[0], P[1])", [n, fmt])  # noqa: E731
    assert f("sample.msp", "msp") == "sample.cleaned.msp"
    assert f("my.data.mgf", "mgf") == "my.data.cleaned.mgf"
    assert f("notes", "strings") == "notes.cleaned.txt"
    assert f("", "msp") == "cleaned.msp" and f(None, "columns") == "cleaned.txt"


@needs_node
def test_zoom_range_keeps_center_and_clamps():
    b = {"lo": 0, "hi": 1000}
    r = js("L.zoomRange(P[0], P[1], P[2], P[3])", [{"lo": 100, "hi": 200}, 0.5, 150, b])
    assert r == {"lo": 125, "hi": 175}
    r = js("L.zoomRange(P[0], P[1], P[2], P[3])", [{"lo": 100, "hi": 200}, 0.5, 100, b])
    assert r["lo"] == 100 and r["hi"] == 150  # the point under the cursor stays put
    r = js("L.zoomRange(P[0], P[1], P[2], P[3])", [{"lo": 100, "hi": 200}, 100, 150, b])
    assert r == b  # cannot zoom out past the data
    r = js("L.zoomRange(P[0], P[1], P[2], P[3])", [{"lo": 0, "hi": 100}, 2, 0, b])
    assert r == {"lo": 0, "hi": 200}
    r = js("L.panRange(P[0], P[1], P[2])", [{"lo": 900, "hi": 1000}, 50, b])
    assert r == {"lo": 900, "hi": 1000}
    r = js("L.panRange(P[0], P[1], P[2])", [{"lo": 100, "hi": 200}, -150, b])
    assert r == {"lo": 0, "hi": 100}
    ticks = js("L.niceTicks(P[0], P[1], 6)", [0, 100])
    assert ticks[0] == 0 and ticks[-1] == 100 and len(ticks) <= 12


@needs_node
def test_detect_kind_matches_server_formats():
    cases = ["Name: a\n1 2", "BEGIN IONS\n1 2\nEND IONS", "1:2 3:4", "1:2\n3:4", "1 2\n3 4",
             "1:2 3:4\n5:6", "Num Peaks: 3\n1 2", "abc:def ghi:jkl", ""]
    for t in cases:
        assert js("L.detectKind(P)", t) == cv.detect_format(t), t


@needs_node
def test_peak_text_keeps_only_peaks():
    mgf = "BEGIN IONS\nTITLE=a: b\nPEPMASS=1\n181.0707 100 2+\n85.0284 3\nEND IONS\n"
    assert js("L.peakText(P[0], P[1])", [mgf, "mgf"]) == "181.0707 100\n85.0284 3"
    msp = "Name: a:b\nNum Peaks: 1\n181.0707\t100\n"
    assert js("L.peakText(P[0], P[1])", [msp, "msp"]) == "181.0707\t100"


def test_index_html_loads_lib_before_app_and_has_no_inline_code():
    html = (STATIC / "index.html").read_text()
    assert html.index('src="/static/lib.js"') < html.index('src="/static/app.js"')
    for m in re.finditer(r"<script\b([^>]*)>", html):
        assert "src=" in m.group(1), "inline script"
    assert "<style" not in html and not re.search(r"\sstyle\s*=", html)
    assert not re.search(r"\son[a-z]+\s*=", html)


def test_lib_js_served_with_page_headers():
    from fastapi.testclient import TestClient
    from denoise_service.app import create_app
    from denoise_service.config import Config
    with TestClient(create_app(Config())) as c:
        r = c.get("/static/lib.js")
        assert r.status_code == 200 and "javascript" in r.headers["content-type"]
        assert r.headers["content-security-policy"].startswith("default-src 'self'")


@needs_node
def test_pass_through_result_keeps_the_record_text_and_marks_it_too_large():
    raw = "Name: big\nNum Peaks: 1\n100.5 3\n"
    res = js("L.passThroughResult(P[0], P[1])", [raw, "msp"])
    assert res["text"] == raw and res["format"] == "msp"
    (rec,) = res["records"]
    assert rec["error"] == "passed through: too large" and rec["total"] == 0
    assert res["summary"] == {"records": 1, "cleaned": 0, "passed_through": 1, "peaks": 0,
                              "impossible": 0}
    assert js("L.isRecordTooLarge(P[0], P[1])", [413, 1]) is True
    assert js("L.isRecordTooLarge(P[0], P[1])", [422, 1]) is True
    assert js("L.isRecordTooLarge(P[0], P[1])", [422, 2]) is False
    assert js("L.isRecordTooLarge(P[0], P[1])", [429, 1]) is False


@needs_node
def test_partial_download_name_and_summary():
    f = lambda n, fmt, partial: js("L.outputFilename(P[0], P[1], P[2])", [n, fmt, partial])  # noqa
    assert f("sample.msp", "msp", False) == "sample.cleaned.msp"
    assert f("sample.msp", "msp", True) == "sample.partial.msp"
    assert f("notes", "strings", True) == "notes.partial.txt"
    assert f("", "mgf", True) == "partial.mgf"
    note = js("L.partialNote(P[0], P[1])", [3, 10])
    assert "3 of 10" in note and "missing" in note
    assert js("L.partialNote(P[0], P[1])", [0, 10]) == ""


@needs_node
def test_peak_text_sends_two_numbers_of_an_annotated_line():
    msp = ('Name: a\nNum Peaks: 2\n181.0707\t10\t"C6H13O6+"\n85.0284 4 bare words here\n'
           '100.5 3 4\n')
    assert js("L.peakText(P[0], P[1])", [msp, "msp"]) == \
        '181.0707 10\n85.0284 4\n100.5 3 4'


ODD_WS = [
    "﻿Name: a\nIonMode: P\nNum Peaks: 1\n100.5 1\n\nName: b\nIonMode: P\nNum Peaks: 1\n181.0707 2\n",
    "Name: a\nNum Peaks: 1\n100.5 1\n\x85\nName: b\n3 4\n",
    "Name: a\n100.5 1\n\x1c\n\nName: b\n3 4\n",
    "Name: a\n100.5 1\n﻿\n\nName: b\n3 4\n",
    "Name: a\n100.5\x851\n\n\x1f\nName: b\n3 4\n",
    "1:2 3:4\n\x85\n5:6 7:8\n",
    "BEGIN IONS\n1 2\nEND IONS\n\x1c\nBEGIN IONS\n3 4\nEND IONS\n",
    "\x85Name: a\n1 2\n", "\x1cName: a\n1 2\n",
]


@needs_node
def test_js_and_python_agree_on_bom_nel_and_fs_boundaries():
    for t in ODD_WS:
        s = t[1:] if t.startswith("﻿") else t
        kind = js("L.detectKind(P)", t)
        assert kind == cv.detect_format(s), repr(t)
        py = [r.raw for r in cv.split_records(s, kind)]
        assert js("L.splitRecords(P[0], P[1])", [t, kind]) == py, repr(t)


@needs_node
def test_bom_msp_is_chunked_like_the_server_records():
    text = many_msp(9)
    bom = "﻿" + text
    chunks = js("L.splitIntoChunks(P, 300, 2)", bom)
    assert "".join(chunks) == text and len(chunks) > 3
    assert all(c.startswith("Name: rec") for c in chunks)
    assert js("L.splitIntoChunks(P, 300, 2)", text) == chunks
