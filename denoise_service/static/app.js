"use strict";
/* DOM part. All text goes through textContent / setAttribute. */
(function () {
  if (typeof document === "undefined") return;
  var L = window.DenoiseLib;
  var NS = "http://www.w3.org/2000/svg";
  var MAX_BYTES = 400000, MAX_RECORDS = 100, MAX_RETRIES = 5, INLINE_CHARS = 200000;
  var $ = function (id) { return document.getElementById(id); };
  var source = null;      // {name, text} of a loaded file kept out of the textarea when large
  var job = null;         // the running clean: {cancelled, controller}
  var last = null;        // {kind, groups, format, text, records, summary, name}
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
  var BLUE = "#022851", RED = "#c62828";
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
    var ax = { stroke: "#333" };
    root.appendChild(svg("line", { x1: PAD.l, y1: PAD.t + IH, x2: PAD.l + IW, y2: PAD.t + IH, stroke: ax.stroke }));
    root.appendChild(svg("line", { x1: PAD.l, y1: PAD.t, x2: PAD.l, y2: PAD.t + IH, stroke: ax.stroke }));
    [0, 25, 50, 75, 100].forEach(function (p) {
      var y = PAD.t + IH - p / 100 * IH;
      root.appendChild(svg("line", { x1: PAD.l - 4, y1: y, x2: PAD.l, y2: y, stroke: ax.stroke }));
      root.appendChild(svg("text", { x: PAD.l - 7, y: y + 4, "text-anchor": "end", "font-size": 11, fill: "#333" }, String(p)));
    });
    L.niceTicks(r.lo, r.hi, 7).forEach(function (v) {
      var x = xOf(v);
      if (x < PAD.l - 0.5 || x > PAD.l + IW + 0.5) return;
      root.appendChild(svg("line", { x1: x, y1: PAD.t + IH, x2: x, y2: PAD.t + IH + 4, stroke: ax.stroke }));
      root.appendChild(svg("text", { x: x, y: PAD.t + IH + 17, "text-anchor": "middle", "font-size": 11, fill: "#333" }, fmt(v)));
    });
    root.appendChild(svg("text", { x: PAD.l + IW / 2, y: H - 4, "text-anchor": "middle", "font-size": 12, fill: "#333" }, "m/z"));
    root.appendChild(svg("text", { x: 11, y: PAD.t + IH / 2, "font-size": 12, fill: "#333", "text-anchor": "middle",
                                   transform: "rotate(-90 11 " + (PAD.t + IH / 2) + ")" }, "relative intensity (%)"));
    // possible first so impossible sticks draw on top
    [true, false].forEach(function (flag) {
      vis.forEach(function (i) {
        if (i.possible !== flag) return;
        root.appendChild(svg("line", { x1: xOf(i.mz), y1: PAD.t + IH, x2: xOf(i.mz), y2: yOf(i.intensity),
                                       stroke: flag ? BLUE : RED, "stroke-width": 2, "pointer-events": "none" }));
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
    var sel = svg("rect", { id: "selrect", x: 0, y: PAD.t, width: 0, height: IH, fill: "rgba(2,40,81,0.15)", stroke: BLUE, visibility: "hidden", "pointer-events": "none" });
    root.appendChild(sel);
    var tip = svg("g", { id: "tip", visibility: "hidden", "pointer-events": "none" });
    tip.appendChild(svg("rect", { id: "tiprect", rx: 3, fill: "#fff", stroke: "#333", "fill-opacity": 0.95 }));
    tip.appendChild(svg("text", { id: "tiptext", "font-size": 11, fill: "#111" }));
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

  function sleep(ms, job) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  function postJson(url, body, signal) {
    return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                        body: JSON.stringify(body), signal: signal })
      .then(function (r) {
        return r.json().then(function (j) { return { r: r, j: j }; }, function () { return { r: r, j: null }; });
      });
  }

  function detailOf(x) {
    return x.j && typeof x.j.detail === "string" ? x.j.detail : "request failed (HTTP " + x.r.status + ")";
  }

  function options() {
    var picked = document.querySelector('input[name="mode"]:checked');
    var o = { halogens: $("halogens").checked };
    if (picked) o.mode = picked.value;
    var pre = $("precursor").value.trim();
    if (pre !== "") {
      var n = Number(pre);
      if (!isFinite(n) || n <= 0) return null;
      o.precursor_mz = n;
    }
    return o;
  }

  /* Convert groups[from, to) with retries; resolves to a list of results in order, or throws. */
  function convertRange(ctx, from, to, onDone) {
    var body = { text: ctx.groups.slice(from, to).join(""), format: ctx.kind };
    for (var k in ctx.opts) body[k] = ctx.opts[k];
    var tries = 0;
    function attempt() {
      if (ctx.job.cancelled) return Promise.reject({ cancelled: true });
      return postJson("/v1/convert", body, ctx.job.controller.signal).then(function (x) {
        if (x.r.status === 200 && x.j) { onDone(from, to, x.j); return; }
        var st = x.r.status;
        if ((st === 413 || st === 422) && to - from > 1) {
          var mid = from + Math.ceil((to - from) / 2);
          return convertRange(ctx, from, mid, onDone).then(function () { return convertRange(ctx, mid, to, onDone); });
        }
        if ((st === 429 || st === 503) && tries < MAX_RETRIES) {
          tries++;
          var wait = Math.min(30, Math.max(1, parseInt(x.r.headers.get("Retry-After"), 10) || 1));
          ctx.status("Server busy; retrying in " + wait + " s (" + tries + " of " + MAX_RETRIES + ")");
          return sleep(wait * 1000).then(attempt);
        }
        return Promise.reject({ message: detailOf(x) });
      });
    }
    return attempt();
  }

  function setBusy(busy) {
    $("go").disabled = busy;
    $("cancel").hidden = !busy;
  }

  function clean(ev) {
    ev.preventDefault();
    showMessage("");
    var text = source ? source.text : $("spectrum").value;
    if (!text.trim()) { showMessage("Paste a spectrum or load a file first."); return; }
    var opts = options();
    if (!opts) { showMessage("Precursor m/z must be a positive number."); return; }
    var kind = L.detectKind(text), groups = L.splitRecords(text, kind);
    var ranges = L.packRanges(groups, MAX_BYTES, MAX_RECORDS);
    var j = { cancelled: false, controller: new AbortController() };
    job = j;
    var out = [], records = [], done = 0, format = kind;
    var summary = { records: 0, cleaned: 0, passed_through: 0, peaks: 0, impossible: 0 };
    var ctx = { kind: kind, groups: groups, opts: opts, job: j,
                status: function (t) { $("progress").textContent = t; } };
    var results = {};
    function onDone(from, to, res) {
      results[from] = res;
      done += to - from;
      ctx.status("Cleaned " + done + " of " + groups.length + " records");
    }
    function finish(partial) {
      Object.keys(results).map(Number).sort(function (a, b) { return a - b; }).forEach(function (from) {
        var res = results[from];
        format = res.format;
        out.push(res.text);
        res.records.forEach(function (r) { r.index = records.length; records.push(r); });
        for (var key in summary) summary[key] += res.summary[key];
      });
      if (records.length) {
        last = { kind: kind, groups: groups, format: format, text: out.join(""), records: records,
                 summary: summary, name: source ? source.name : "", partial: partial, opts: opts };
        renderResult();
      }
    }
    setBusy(true);
    $("result").hidden = true;
    ctx.status("Cleaning " + groups.length + " record" + (groups.length === 1 ? "" : "s") + "...");
    ranges.reduce(function (p, r) { return p.then(function () { return convertRange(ctx, r[0], r[1], onDone); }); },
                  Promise.resolve())
      .then(function () { finish(false); ctx.status(""); })
      .catch(function (err) {
        finish(true);
        if (j.cancelled || (err && err.cancelled)) ctx.status("Cancelled. Kept the " + done + " records cleaned so far.");
        else {
          ctx.status("");
          showMessage((err && err.message) ? err.message + " Partial output kept (" + done + " of " + groups.length + " records)."
                                           : "Could not reach the service. Check your connection and try again.");
        }
      })
      .then(function () { setBusy(false); job = null; });
  }

  function problems(res) {
    var n = {};
    res.records.forEach(function (r) { if (r.error) n[r.error] = (n[r.error] || 0) + 1; });
    return Object.keys(n).map(function (k) { return n[k] + " x " + k; });
  }

  function renderResult() {
    var s = last.summary;
    var line = s.records + " record" + (s.records === 1 ? "" : "s") + ": " + s.cleaned + " cleaned, " +
               s.passed_through + " passed through unchanged. " + s.impossible + " of " + s.peaks + " ions removed.";
    var p = problems(last);
    if (p.length) line += " Not cleaned: " + p.join(", ") + (p.join().indexOf("no ion mode") >= 0 ? " (choose a polarity above and run again)." : ".");
    if (last.partial) line += " Output is partial.";
    $("summary").textContent = line;
    fillRecords();
    $("copystatus").textContent = "";
    $("result").hidden = false;
    var first = 0;
    selectRecord(first);
  }

  function fillRecords() {
    var body = $("records").tBodies[0];
    body.textContent = "";
    last.records.forEach(function (r) {
      var tr = document.createElement("tr");
      if (r.error) tr.className = "impossible";
      [String(r.index + 1), r.name || "", String(r.total), String(r.impossible), r.error ? r.error : "cleaned"].forEach(function (t) {
        var td = document.createElement("td");
        td.textContent = t;
        tr.appendChild(td);
      });
      var td = document.createElement("td");
      if (!r.error && r.total) {
        var b = document.createElement("button");
        b.type = "button";
        b.className = "secondary small";
        b.textContent = "Plot";
        b.setAttribute("aria-label", "Plot record " + (r.index + 1));
        b.addEventListener("click", function () { selectRecord(r.index); });
        td.appendChild(b);
      }
      tr.appendChild(td);
      body.appendChild(tr);
    });
  }

  /* Plot one record's ORIGINAL ions coloured by verdict, using /v1/classify for that record. */
  function selectRecord(i) {
    selected = i;
    var rec = last.records[i], info = $("plotinfo");
    $("plot").textContent = "";
    view = null;
    $("range").textContent = "";
    $("table").tBodies[0].textContent = "";
    if (!rec || rec.error || !rec.total || !rec.mode) {
      info.textContent = rec && rec.error ? "Record " + (i + 1) + " was not cleaned (" + rec.error + "); nothing to plot." : "";
      return;
    }
    info.textContent = "Plot of record " + (i + 1) + (rec.name ? " (" + rec.name + ")" : "") + ", " + rec.mode + " mode.";
    var body = { mode: rec.mode, spectrum: L.peakText(last.groups[i], last.kind), halogens: last.opts.halogens };
    if (rec.precursor_mz) body.precursor_mz = rec.precursor_mz;
    var token = last;
    postJson("/v1/classify", body).then(function (x) {
      if (token !== last || selected !== i) return;
      if (x.r.status === 200 && x.j) { setPlot(x.j); fillTable(x.j.ions); }
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
    if (!last) return;
    var blob = new Blob([last.text], { type: "text/plain;charset=utf-8" });
    var url = URL.createObjectURL(blob), a = document.createElement("a");
    a.href = url;
    a.download = L.cleanedFilename(last.name, last.format);
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 10000);
  }

  function loadFile(file) {
    if (!file) return;
    if (file.size > 200 * 1024 * 1024) { showMessage("That file is larger than 200 MB; split it first."); return; }
    var reader = new FileReader();
    reader.onload = function () {
      var text = String(reader.result);
      source = { name: file.name, text: text };
      $("filename").textContent = file.name + " (" + Math.round(file.size / 1024) + " KB)";
      var ta = $("spectrum");
      ta.value = text.length <= INLINE_CHARS ? text : "";
      ta.placeholder = text.length <= INLINE_CHARS ? ta.placeholder : "Loaded " + file.name + ". Type here to use pasted text instead.";
      showMessage("");
    };
    reader.onerror = function () { showMessage("Could not read that file."); };
    reader.readAsText(file);
  }

  setupPlotEvents();
  $("form").addEventListener("submit", clean);
  $("cancel").addEventListener("click", function () {
    if (job) { job.cancelled = true; job.controller.abort(); }
  });
  $("spectrum").addEventListener("input", function () { source = null; $("filename").textContent = ""; });
  $("file").addEventListener("change", function (e) { loadFile(e.target.files[0]); });
  ["dragover", "drop"].forEach(function (n) {
    $("spectrum").addEventListener(n, function (e) {
      e.preventDefault();
      if (n === "drop" && e.dataTransfer && e.dataTransfer.files.length) loadFile(e.dataTransfer.files[0]);
    });
  });
  $("download").addEventListener("click", download);
  $("copycleaned").addEventListener("click", function () { if (last) copy(last.text, "Copied cleaned text."); });
  $("copy").addEventListener("click", function () {
    if (last) copy(JSON.stringify({ format: last.format, text: last.text, records: last.records, summary: last.summary }, null, 2), "Copied JSON.");
  });
})();
