"use strict";
/* DOM part. All text goes through textContent / setAttribute. */
(function () {
  if (typeof document === "undefined") return;
  var L = window.DenoiseLib;
  var NS = "http://www.w3.org/2000/svg";
  var MAX_BYTES = 400000, MAX_RECORDS = 100, SLICE_BYTES = 4 * 1024 * 1024, KEEP = 1000;
  var MAX_FILE = 1024 * 1024 * 1024, COPY_LIMIT = 5 * 1000 * 1000;
  var $ = function (id) { return document.getElementById(id); };
  var source = null;      // {file, name, size}: a loaded file, read in slices, never held as a string
  var job = null;         // the running clean: {cancelled, controller, reader}
  var last = null;        // the finished run: {kind, format, blob, acc, summary, name, partial, ...}
  var selected = -1;

  function svg(name, attrs, text) {
    var el = document.createElementNS(NS, name);
    for (var k in attrs) el.setAttribute(k, String(attrs[k]));
    if (text !== undefined) el.textContent = text;
    return el;
  }

  function showMessage(text) {
    var m = $("message");
    m.textContent = text;
    m.hidden = !text;
  }

  /* ---- zoomable stick plot -------------------------------------------------------------- */
  var W = 720, H = 300, PAD = { l: 46, r: 12, t: 12, b: 36 };
  var IW = W - PAD.l - PAD.r, IH = H - PAD.t - PAD.b;
  var view = null;   // {ions, bounds:{lo,hi}, range:{lo,hi}, focus:ion|null}

  function fmt(v) { return String(parseFloat(v.toPrecision(6))); }

  function setPlot(data) {
    var ions = data.ions.slice().sort(function (a, b) { return a.mz - b.mz; });
    var lo = ions[0].mz, hi = ions[ions.length - 1].mz;
    var pad = hi > lo ? (hi - lo) * 0.03 : 1;
    var bounds = { lo: lo - pad, hi: hi + pad };
    view = { ions: ions, bounds: bounds, range: { lo: bounds.lo, hi: bounds.hi }, tip: null };
    $("npossible").textContent = "(" + ions.filter(function (i) { return i.possible; }).length + ")";
    $("nimpossible").textContent = "(" + ions.filter(function (i) { return !i.possible; }).length + ")";
    drawPlot();
  }

  function xOf(mz) { return PAD.l + (mz - view.range.lo) / (view.range.hi - view.range.lo) * IW; }
  function mzOf(x) { return view.range.lo + (x - PAD.l) / IW * (view.range.hi - view.range.lo); }

  function drawPlot() {
    if (!view) return;
    var r = view.range;
    var vis = view.ions.filter(function (i) { return i.mz >= r.lo && i.mz <= r.hi; });
    var top = vis.reduce(function (m, i) { return Math.max(m, i.intensity); }, 0) || 1;
    var yOf = function (v) { return PAD.t + IH - Math.max(0, v) / top * IH; };
    var root = svg("svg", { viewBox: "0 0 " + W + " " + H, role: "img",
                            "aria-label": "Stick plot, m/z " + fmt(r.lo) + " to " + fmt(r.hi) + ", " + vis.length + " ions shown" });
    root.appendChild(svg("line", { x1: PAD.l, y1: PAD.t + IH, x2: PAD.l + IW, y2: PAD.t + IH, class: "ax" }));
    root.appendChild(svg("line", { x1: PAD.l, y1: PAD.t, x2: PAD.l, y2: PAD.t + IH, class: "ax" }));
    [0, 25, 50, 75, 100].forEach(function (p) {
      var y = PAD.t + IH - p / 100 * IH;
      root.appendChild(svg("line", { x1: PAD.l - 4, y1: y, x2: PAD.l, y2: y, class: "ax" }));
      root.appendChild(svg("text", { x: PAD.l - 7, y: y + 4, "text-anchor": "end", "font-size": 11, class: "axt" }, String(p)));
    });
    L.niceTicks(r.lo, r.hi, 7).forEach(function (v) {
      var x = xOf(v);
      if (x < PAD.l - 0.5 || x > PAD.l + IW + 0.5) return;
      root.appendChild(svg("line", { x1: x, y1: PAD.t + IH, x2: x, y2: PAD.t + IH + 4, class: "ax" }));
      root.appendChild(svg("text", { x: x, y: PAD.t + IH + 17, "text-anchor": "middle", "font-size": 11, class: "axt" }, fmt(v)));
    });
    root.appendChild(svg("text", { x: PAD.l + IW / 2, y: H - 4, "text-anchor": "middle", "font-size": 12, class: "axt" }, "m/z"));
    root.appendChild(svg("text", { x: 11, y: PAD.t + IH / 2, "font-size": 12, class: "axt", "text-anchor": "middle",
                                   transform: "rotate(-90 11 " + (PAD.t + IH / 2) + ")" }, "relative intensity (%)"));
    // possible first so impossible sticks draw on top
    [true, false].forEach(function (flag) {
      vis.forEach(function (i) {
        if (i.possible !== flag) return;
        root.appendChild(svg("line", { x1: xOf(i.mz), y1: PAD.t + IH, x2: xOf(i.mz), y2: yOf(i.intensity),
                                       class: flag ? "stk-p" : "stk-i", "stroke-width": 2, "pointer-events": "none" }));
      });
    });
    if (vis.length <= 60) {
      // keyboard-focusable hit targets, only when few enough ions are visible to tab through
      vis.forEach(function (i) {
        var hit = svg("line", { x1: xOf(i.mz), y1: PAD.t + IH, x2: xOf(i.mz), y2: yOf(i.intensity) - 3,
                                stroke: "transparent", "stroke-width": 9, tabindex: 0, class: "hit",
                                "aria-label": "m/z " + i.mz + ", intensity " + i.intensity + ", " + (i.possible ? "possible" : "not explained by CHNOPS") });
        hit.addEventListener("focus", function () { showTip(i); });
        hit.addEventListener("blur", hideTip);
        root.appendChild(hit);
      });
    }
    var sel = svg("rect", { id: "selrect", x: 0, y: PAD.t, width: 0, height: IH, class: "selbox", visibility: "hidden", "pointer-events": "none" });
    root.appendChild(sel);
    var tip = svg("g", { id: "tip", visibility: "hidden", "pointer-events": "none" });
    tip.appendChild(svg("rect", { id: "tiprect", rx: 3, class: "tipbg" }));
    tip.appendChild(svg("text", { id: "tiptext", "font-size": 11, class: "tiptext" }));
    root.appendChild(tip);
    var box = $("plot");
    var hadFocus = document.activeElement && box.contains(document.activeElement) && document.activeElement !== box;
    box.textContent = "";
    box.appendChild(root);
    view.svg = root;
    view.yOf = yOf;
    view.vis = vis;
    view.tip = null;
    $("range").textContent = "m/z " + fmt(r.lo) + " to " + fmt(r.hi) + ", " + vis.length + " of " + view.ions.length + " ions";
    if (hadFocus) box.focus();
  }

  function showTip(i) {
    var t = $("tiptext"), g = $("tip"), rc = $("tiprect");
    if (!t) return;
    t.textContent = "";
    var lines = ["m/z " + i.mz, "intensity " + i.intensity, i.possible ? "possible" : "not explained by CHNOPS"];
    lines.forEach(function (l, n) {
      var ts = svg("tspan", { x: 0, dy: n ? 13 : 0 }, l);
      t.appendChild(ts);
    });
    var w = 150, h = 48;
    var x = Math.min(Math.max(xOf(i.mz) + 8, PAD.l + 2), W - w - 4);
    var y = Math.max(view.yOf(i.intensity), PAD.t + 4);
    y = Math.min(y, PAD.t + IH - h);
    rc.setAttribute("x", x - 5); rc.setAttribute("y", y - 4); rc.setAttribute("width", w); rc.setAttribute("height", h);
    t.setAttribute("transform", "translate(" + x + " " + (y + 8) + ")");
    t.querySelectorAll("tspan").forEach(function (ts) { ts.setAttribute("x", 0); });
    g.setAttribute("visibility", "visible");
  }

  function hideTip() { var g = $("tip"); if (g) g.setAttribute("visibility", "hidden"); }

  function applyRange(r) {
    if (!view) return;
    view.range = r;
    drawPlot();
  }

  function zoomBy(factor, centerMz) {
    if (!view) return;
    var c = centerMz === undefined ? (view.range.lo + view.range.hi) / 2 : centerMz;
    applyRange(L.zoomRange(view.range, factor, c, view.bounds));
  }

  function panBy(frac) {
    if (!view) return;
    applyRange(L.panRange(view.range, (view.range.hi - view.range.lo) * frac, view.bounds));
  }

  function resetZoom() { if (view) applyRange({ lo: view.bounds.lo, hi: view.bounds.hi }); }

  /* Pointer handling: wheel zooms about the cursor; mouse drag selects a range, shift-drag pans;
     one finger pans; two fingers pinch. Double-click resets. */
  function svgX(ev) {
    var rc = view.svg.getBoundingClientRect();
    return (ev.clientX - rc.left) / rc.width * W;
  }

  function nearest(ev) {
    var x = svgX(ev), best = null, d = 8;
    view.vis.forEach(function (i) { var dd = Math.abs(xOf(i.mz) - x); if (dd < d) { d = dd; best = i; } });
    return best;
  }

  function setupPlotEvents() {
    var box = $("plot"), pts = {}, drag = null, pinch = null;
    function count() { return Object.keys(pts).length; }
    box.addEventListener("wheel", function (ev) {
      if (!view) return;
      ev.preventDefault();
      zoomBy(ev.deltaY < 0 ? 0.8 : 1.25, mzOf(svgX(ev)));
    }, { passive: false });
    box.addEventListener("dblclick", resetZoom);
    box.addEventListener("pointerdown", function (ev) {
      if (!view || !view.svg || ev.button > 0) return;
      pts[ev.pointerId] = { x: ev.clientX };
      try { box.setPointerCapture(ev.pointerId); } catch (e) { /* synthetic pointers */ }
      if (count() === 2) {
        var ks = Object.keys(pts);
        pinch = { d: Math.abs(pts[ks[0]].x - pts[ks[1]].x) || 1, range: { lo: view.range.lo, hi: view.range.hi } };
        drag = null;
      } else {
        var pan = ev.shiftKey || ev.pointerType === "touch";
        drag = { x0: svgX(ev), pan: pan, range: { lo: view.range.lo, hi: view.range.hi }, moved: false };
      }
    });
    box.addEventListener("pointermove", function (ev) {
      if (!view || !view.svg) return;
      if (pts[ev.pointerId]) pts[ev.pointerId].x = ev.clientX;
      if (pinch && count() === 2) {
        var ks = Object.keys(pts), d = Math.abs(pts[ks[0]].x - pts[ks[1]].x) || 1;
        var mid = (pts[ks[0]].x + pts[ks[1]].x) / 2, rc = view.svg.getBoundingClientRect();
        var center = pinch.range.lo + ((mid - rc.left) / rc.width * W - PAD.l) / IW * (pinch.range.hi - pinch.range.lo);
        view.range = L.zoomRange(pinch.range, pinch.d / d, center, view.bounds);
        drawPlot();
        return;
      }
      if (drag) {
        var x = svgX(ev);
        if (Math.abs(x - drag.x0) > 3) drag.moved = true;
        if (drag.pan) {
          var dmz = (drag.x0 - x) / IW * (drag.range.hi - drag.range.lo);
          view.range = L.panRange(drag.range, dmz, view.bounds);
          drawPlot();
          drag.x0 = x; drag.range = { lo: view.range.lo, hi: view.range.hi };
        } else {
          var sel = $("selrect");
          var x1 = Math.max(PAD.l, Math.min(drag.x0, x)), x2 = Math.min(PAD.l + IW, Math.max(drag.x0, x));
          sel.setAttribute("x", x1); sel.setAttribute("width", Math.max(0, x2 - x1));
          sel.setAttribute("visibility", "visible");
        }
        return;
      }
      var hit = nearest(ev);
      if (hit) showTip(hit); else hideTip();
    });
    function end(ev) {
      if (!view) return;
      if (drag && !drag.pan && drag.moved && count() === 1) {
        var a = mzOf(Math.max(PAD.l, Math.min(drag.x0, svgX(ev)))), b = mzOf(Math.min(PAD.l + IW, Math.max(drag.x0, svgX(ev))));
        if (b - a > (view.bounds.hi - view.bounds.lo) * 1e-4) {
          view.range = { lo: Math.max(view.bounds.lo, a), hi: Math.min(view.bounds.hi, b) };
          drawPlot();
        }
      }
      delete pts[ev.pointerId];
      if (count() < 2) pinch = null;
      if (!count()) drag = null;
      var sel = $("selrect");
      if (sel) sel.setAttribute("visibility", "hidden");
    }
    box.addEventListener("pointerup", end);
    box.addEventListener("pointercancel", end);
    box.addEventListener("pointerleave", function () { if (!drag) hideTip(); });
    box.addEventListener("keydown", function (ev) {
      if (ev.target !== box) return;
      var k = ev.key;
      if (k === "+" || k === "=") zoomBy(0.7);
      else if (k === "-" || k === "_") zoomBy(1 / 0.7);
      else if (k === "ArrowLeft") panBy(-0.2);
      else if (k === "ArrowRight") panBy(0.2);
      else if (k === "0" || k === "Home") resetZoom();
      else return;
      ev.preventDefault();
    });
    $("zoomin").addEventListener("click", function () { zoomBy(0.7); });
    $("zoomout").addEventListener("click", function () { zoomBy(1 / 0.7); });
    $("panleft").addEventListener("click", function () { panBy(-0.25); });
    $("panright").addEventListener("click", function () { panBy(0.25); });
    $("zoomreset").addEventListener("click", resetZoom);
  }

  function fillTable(ions) {
    var body = $("table").tBodies[0];
    body.textContent = "";
    ions.forEach(function (ion) {
      var tr = document.createElement("tr");
      if (!ion.possible) tr.className = "impossible";
      [String(ion.mz), String(ion.intensity), ion.possible ? "possible" : "impossible"].forEach(function (t) {
        var td = document.createElement("td");
        td.textContent = t;
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
  }

  function num(n) { return Number(n).toLocaleString("en-US"); }
  var KIND_LABEL = { msp: "MSP", mgf: "MGF", strings: "mz:int strings", columns: "Two columns" };

  function postJson(url, body, signal) {
    return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                        body: JSON.stringify(body), signal: signal })
      .then(function (r) {
        var ra = parseInt(r.headers.get("Retry-After"), 10) || 0;
        return r.json().then(function (j) { return { status: r.status, json: j, retryAfter: ra }; },
                             function () { return { status: r.status, json: null, retryAfter: ra }; });
      });
  }

  function detailOf(x) {
    return x.json && typeof x.json.detail === "string" ? x.json.detail : "request failed (HTTP " + x.status + ")";
  }

  function options() {
    var picked = document.querySelector('input[name="mode"]:checked');
    var o = { halogens: $("halogens").checked };
    if (picked && picked.value) o.mode = picked.value;
    var pre = $("precursor").value.trim();
    if (pre !== "") {
      var n = Number(pre);
      if (!isFinite(n) || n <= 0) return null;
      o.precursor_mz = n;
    }
    return o;
  }

  function setBusy(busy) {
    $("go").disabled = busy;
    $("cancel").hidden = !busy;
    $("file").disabled = busy;
    $("clearfile").disabled = busy;
  }

  function status(t) { $("progress").textContent = t; }

  /* ---- the run: stream the input, clean in parallel requests, assemble the output ----------- */
  function clean(ev) {
    ev.preventDefault();
    if (job) return;
    showMessage("");
    var text = source ? null : $("spectrum").value;
    if (!source && !text.trim()) { showMessage("Paste a spectrum or load a file first."); return; }
    var opts = options();
    if (!opts) { showMessage("Precursor m/z must be a positive number."); return; }
    var name = source ? source.name : "";
    if ($("streamsave").checked && !$("savetoggle").hidden && window.showSaveFilePicker) {
      // must be requested now, inside the click, before any waiting
      window.showSaveFilePicker({ suggestedName: L.outputFilename(name, "", false) })
        .then(function (handle) { return handle.createWritable().then(function (w) { return { handle: handle, w: w }; }); })
        .then(function (target) { run(text, opts, target); },
              function (err) {
                if (err && err.name === "AbortError") status("No file chosen; nothing was cleaned.");
                else showMessage("Could not open a file for writing. Untick “Write straight to a file” to download instead.");
              });
      return;
    }
    run(text, opts, null);
  }

  function run(text, opts, target) {
    var total = source ? source.size : L.byteLen(text);
    var name = source ? source.name : "";
    var reader = source ? L.createBlobReader(source.file, { sliceBytes: SLICE_BYTES }) : L.createTextReader(text, 1 << 20);
    var out = L.createOutput({ writable: target ? target.w : null }), acc = L.createAccumulator(KEEP);
    var meter = L.createRateMeter(750), announce = L.createAnnouncer(8000);
    var j = { cancelled: false, controller: new AbortController(), reader: reader };
    var kind = null, st = null, lastPaint = 0, started = Date.now();
    job = j;
    setBusy(true);
    $("result").hidden = true;
    $("empty").hidden = false;
    $("runpanel").hidden = false;
    paint({ recordsDone: 0, bytesDone: 0 }, true);
    status("Cleaning " + L.formatBytes(total) + (target ? ", writing straight to your file" : "") + "...");

    function paint(stats, force) {
      st = stats;
      var now = Date.now();
      if (!force && now - lastPaint < 200) return;
      lastPaint = now;
      var frac = total ? Math.min(1, stats.bytesDone / total) : 0;
      $("barfill").style.width = (frac * 100).toFixed(1) + "%";
      $("bar").setAttribute("aria-valuenow", String(Math.min(99, Math.floor(frac * 100))));
      $("readline").textContent = "Read " + L.formatBytes(Math.min(reader.bytesRead(), total)) + " of " + L.formatBytes(total);
      $("st-records").textContent = num(stats.recordsDone);
      var rate = meter.rate();
      $("st-speed").textContent = rate ? L.formatBytes(rate) + "/s" : "–";
      $("st-eta").textContent = stats.final ? "done" : frac >= 1 ? "finishing" : L.formatDuration(meter.eta(total - stats.bytesDone));
      $("st-kind").textContent = kind ? (KIND_LABEL[kind] || kind) : "–";
      if (announce(now, frac)) {
        status("Cleaning: " + Math.floor(frac * 100) + "% done, " + num(stats.recordsDone) + " records" +
               (rate ? ", about " + L.formatDuration(meter.eta(total - stats.bytesDone)) + " left" : "") + ".");
      }
    }
    var ticker = setInterval(function () { if (st) paint(st, true); }, 500);

    L.runPipeline({
      read: function () { return reader.read(); },
      isCancelled: function () { return j.cancelled; },
      maxBytes: MAX_BYTES, maxRecords: MAX_RECORDS, concurrency: 3, maxQueued: 8,
      onKind: function (k) { kind = k; },
      send: function (req) {
        return L.convertRecords({ post: function (body) { return postJson("/v1/convert", body, j.controller.signal); },
                                  kind: req.kind, opts: opts, isCancelled: function () { return j.cancelled; },
                                  onStatus: status }, req.records);
      },
      onResult: function (req, res) {
        out.add(res.text);
        acc.add(req, res);
        meter.add(req.bytes, Date.now());
        if (out.error()) throw new Error("the output file could not be written");
      },
      onProgress: function (stats) { paint(stats, false); }
    }).then(function (result) {
      clearInterval(ticker);
      reader.cancel();
      return out.finish().then(function (blob) {
        var partial = !result.completed;
        var missing = Math.max(0, result.recordsRead - result.recordsDone);
        paint({ recordsDone: result.recordsDone, bytesDone: partial ? result.bytesDone : total, final: true }, true);
        if (result.completed) $("bar").setAttribute("aria-valuenow", "100");
        var elapsed = (Date.now() - started) / 1000;
        last = { kind: kind, format: acc.format || kind, blob: blob, streamed: !!target, size: out.size(), acc: acc,
                 name: name, partial: partial, opts: opts, missing: missing, total: result.recordsRead,
                 unread: !result.readDone, savedName: target ? target.handle.name : "", seconds: elapsed, input: total };
        if (result.cancelled) status("Cancelled. Kept the " + num(result.recordsDone) + " records cleaned so far.");
        else if (result.error) {
          status("");
          showMessage((result.error.message || "The run failed.") + " Partial output kept (" + num(result.recordsDone) + " of " +
                      num(result.recordsRead) + (result.readDone ? "" : "+") + " records).");
        } else if (out.error()) {
          showMessage("The output file could not be written: " + (out.error().message || "unknown error") + ".");
        } else status("Done: " + num(result.recordsDone) + " records in " + L.formatDuration(elapsed) + ".");
        if (acc.total) renderResult(); else last = null;
      });
    }).then(function () { setBusy(false); job = null; },
            function (err) {
              clearInterval(ticker);
              showMessage("Unexpected error: " + ((err && err.message) || "see the console") + ".");
              setBusy(false); job = null;
            });
  }

  function problems() {
    var e = last.acc.errors;
    return Object.keys(e).map(function (k) { return num(e[k]) + " × " + k; });
  }

  function renderResult() {
    var s = last.acc.summary;
    $("s-records").textContent = num(s.records);
    $("s-cleaned").textContent = num(s.cleaned);
    $("s-passed").textContent = num(s.passed_through);
    $("s-removed").textContent = (s.peaks ? (100 * s.impossible / s.peaks).toFixed(s.impossible && s.impossible * 1000 < s.peaks ? 2 : 1) : "0") + "%";
    $("s-removed-n").textContent = num(s.impossible) + " of " + num(s.peaks) + " ions";
    var line = s.records + " record" + (s.records === 1 ? "" : "s") + ": " + s.cleaned + " cleaned, " +
               s.passed_through + " passed through unchanged. " + s.impossible + " of " + s.peaks + " ions removed.";
    var p = problems();
    if (p.length) line += " Not cleaned: " + p.join(", ") + (p.join().indexOf("no ion mode") >= 0 ? " (choose a polarity above and run again)." : ".");
    if (last.partial) line += " Output is partial: " + (L.partialNote(last.missing, last.total, last.unread) || "the run did not finish.");
    $("summary").textContent = line;
    fillRecords();
    $("copystatus").textContent = last.streamed ? "Written to " + (last.savedName || "your file") + " as it was cleaned."
                                                : "Cleaned output: " + L.formatBytes(last.size) + ".";
    $("download").hidden = !last.blob;
    $("savefile").hidden = !(last.blob && window.showSaveFilePicker);
    var big = !last.blob || last.size > COPY_LIMIT;
    $("copycleaned").disabled = big;
    $("copy").disabled = big;
    if (last.blob && big) $("copystatus").textContent = "Output is " + L.formatBytes(last.size) + ", too large to copy here. Use Download.";
    $("result").hidden = false;
    $("empty").hidden = true;
    selectRecord(last.acc.retained.length ? last.acc.retained[0] : null);
  }

  function fillRecords() {
    var body = $("records").tBodies[0], entries = last.acc.retained.slice(0, KEEP);
    body.textContent = "";
    entries.forEach(function (e) {
      var r = e.rec, tr = document.createElement("tr");
      if (r.error) tr.className = "impossible";
      [String(r.index + 1), r.name || "", String(r.total), String(r.impossible), r.error ? r.error : "cleaned"].forEach(function (t) {
        var td = document.createElement("td");
        td.textContent = t;
        tr.appendChild(td);
      });
      var td = document.createElement("td");
      if (!r.error && r.total && e.raw !== null) {
        var b = document.createElement("button");
        b.type = "button";
        b.className = "secondary small";
        b.textContent = "Plot";
        b.setAttribute("aria-label", "Plot record " + (r.index + 1));
        b.addEventListener("click", function () { selectRecord(e); });
        td.appendChild(b);
      }
      tr.appendChild(td);
      body.appendChild(tr);
    });
    $("recordsnote").textContent = entries.length >= last.acc.total
      ? "Showing all " + num(last.acc.total) + " record" + (last.acc.total === 1 ? "" : "s") + "."
      : "Showing first " + num(entries.length) + " of " + num(last.acc.total) + " records. The totals above cover all of them.";
  }

  /* Plot one record's ORIGINAL ions coloured by verdict, using /v1/classify for that record.
     Only records the run kept (the first 1000, and records with errors) can be plotted. */
  function selectRecord(entry) {
    var info = $("plotinfo");
    selected = entry ? entry.rec.index : -1;
    $("plot").textContent = "";
    view = null;
    $("range").textContent = "";
    $("npossible").textContent = "";
    $("nimpossible").textContent = "";
    $("table").tBodies[0].textContent = "";
    var rec = entry && entry.rec;
    if (!rec || rec.error || !rec.total || !rec.mode || entry.raw === null) {
      info.textContent = rec && rec.error ? "Record " + (rec.index + 1) + " was not cleaned (" + rec.error + "); nothing to plot." : "";
      return;
    }
    var i = rec.index;
    info.textContent = "Plot of record " + (i + 1) + (rec.name ? " (" + rec.name + ")" : "") + ", " + rec.mode + " mode.";
    var body = { mode: rec.mode, spectrum: L.peakText(entry.raw, last.kind), halogens: last.opts.halogens };
    if (rec.precursor_mz) body.precursor_mz = rec.precursor_mz;
    var token = last;
    postJson("/v1/classify", body).then(function (x) {
      if (token !== last || selected !== i) return;
      if (x.status === 200 && x.json) { setPlot(x.json); fillTable(x.json.ions); }
      else info.textContent = "Could not plot record " + (i + 1) + ": " + detailOf(x);
    }).catch(function () { info.textContent = "Could not plot record " + (i + 1) + ": service not reachable."; });
  }

  function copy(text, ok) {
    var status = $("copystatus");
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        function () { status.textContent = ok; },
        function () { status.textContent = "Copy failed; use Download instead."; });
    } else {
      status.textContent = "Clipboard unavailable in this browser; use Download instead.";
    }
  }

  function download() {
    if (!last || !last.blob) return;
    var url = URL.createObjectURL(last.blob), a = document.createElement("a");
    a.href = url;
    a.download = L.outputFilename(last.name, last.format, last.partial);
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 60000);
  }

  /* Streams the finished (disk-backed) Blob to a file the user picks; no giant string is built. */
  function saveFile() {
    if (!last || !last.blob || !window.showSaveFilePicker) return;
    var blob = last.blob, st = $("copystatus");
    window.showSaveFilePicker({ suggestedName: L.outputFilename(last.name, last.format, last.partial) })
      .then(function (h) {
        return h.createWritable().then(function (w) {
          return (blob.stream ? blob.stream().pipeTo(w) : w.write(blob).then(function () { return w.close(); }))
            .then(function () { st.textContent = "Saved " + h.name + "."; });
        });
      })
      .catch(function (err) {
        st.textContent = err && err.name === "AbortError" ? "Save cancelled." : "Could not save the file; use Download instead.";
      });
  }

  /* ---- input: drop zone, file chip ---------------------------------------------------------- */
  function loadFile(file) {
    if (!file) return;
    if (file.size > MAX_FILE) {
      showMessage("That file is " + L.formatBytes(file.size) + ", over the 1 GB limit (1,073,741,824 bytes). Split it first, for example by record count.");
      return;
    }
    if (file.size === 0) { showMessage("That file is empty."); return; }
    source = { file: file, name: file.name, size: file.size };
    $("filename").textContent = file.name + " (" + L.formatBytes(file.size) + ")";
    $("filechip").hidden = false;
    var ta = $("spectrum");
    ta.value = "";
    ta.placeholder = "Loaded " + file.name + ". Type here to use pasted text instead.";
    showMessage("");
  }

  function clearFile() {
    source = null;
    $("filechip").hidden = true;
    $("filename").textContent = "";
    $("file").value = "";
    $("spectrum").placeholder = "86.06:45 104.11:100 ...";
  }

  function setupDrop() {
    var zone = $("dropzone");
    ["dragenter", "dragover"].forEach(function (n) {
      zone.addEventListener(n, function (e) { e.preventDefault(); zone.classList.add("over"); });
    });
    zone.addEventListener("dragleave", function (e) {
      if (!e.relatedTarget || !zone.contains(e.relatedTarget)) zone.classList.remove("over");
    });
    zone.addEventListener("drop", function (e) {
      e.preventDefault();
      zone.classList.remove("over");
      if (!job && e.dataTransfer && e.dataTransfer.files.length) loadFile(e.dataTransfer.files[0]);
    });
    // a file dropped beside the zone must not navigate the browser away from the page
    ["dragover", "drop"].forEach(function (n) {
      document.addEventListener(n, function (e) { if (!zone.contains(e.target)) e.preventDefault(); });
    });
    $("spectrum").addEventListener("drop", function (e) {
      e.preventDefault();
      if (!job && e.dataTransfer && e.dataTransfer.files.length) loadFile(e.dataTransfer.files[0]);
    });
  }

  setupPlotEvents();
  setupDrop();
  if (window.showSaveFilePicker) $("savetoggle").hidden = false;
  $("form").addEventListener("submit", clean);
  $("cancel").addEventListener("click", function () {
    if (job) { job.cancelled = true; job.controller.abort(); job.reader.cancel(); }
  });
  $("spectrum").addEventListener("input", function () { if (source) clearFile(); });
  $("file").addEventListener("change", function (e) { loadFile(e.target.files[0]); });
  $("clearfile").addEventListener("click", clearFile);
  $("download").addEventListener("click", download);
  $("savefile").addEventListener("click", saveFile);
  $("copycleaned").addEventListener("click", function () {
    if (last && last.blob) last.blob.text().then(function (t) { copy(t, "Copied cleaned text."); });
  });
  $("copy").addEventListener("click", function () {
    if (!last || !last.blob) return;
    last.blob.text().then(function (t) {
      var recs = last.acc.retained.slice(0, KEEP).map(function (e) { return e.rec; });
      copy(JSON.stringify({ format: last.format, text: t, records: recs, records_total: last.acc.total,
                            summary: last.acc.summary }, null, 2), "Copied JSON.");
    });
  });
})();
