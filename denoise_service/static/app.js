"use strict";
/* Pure helpers first (no DOM), so they can be tested under node. */
var BLUE = "#022851", RED = "#c62828";

function summaryText(s) {
  var pct = s.intensity_impossible_fraction * 100;
  var shown = pct === 0 || pct >= 1 || pct <= -1 ? String(Math.round(pct)) : pct.toFixed(1);
  return s.impossible + " of " + s.total + " ions impossible, " + shown + "% of total intensity";
}

function niceStep(span, target) {
  var raw = span / target, mag = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / mag;
  return (f < 1.5 ? 1 : f < 3.5 ? 2 : f < 7.5 ? 5 : 10) * mag;
}

function axisTicks(lo, hi, target) {
  if (!(hi > lo)) return [lo];
  var step = niceStep(hi - lo, target), out = [];
  for (var v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) {
    out.push(Math.round(v / step) * step);
  }
  return out;
}

function fmtTick(v) { return String(parseFloat(v.toPrecision(6))); }

/* Geometry for the stick plot: pixel positions per ion plus the tick marks. Never divides by zero. */
function plotModel(ions, w, h, pad) {
  var xs = ions.map(function (i) { return i.mz; });
  var lo = Math.min.apply(null, xs), hi = Math.max.apply(null, xs);
  if (!(hi > lo)) { lo -= 1; hi += 1; }
  var margin = (hi - lo) * 0.03; lo -= margin; hi += margin;
  var top = Math.max.apply(null, ions.map(function (i) { return i.intensity; }));
  if (!(top > 0)) top = 1;
  var iw = w - pad.l - pad.r, ih = h - pad.t - pad.b;
  var sticks = ions.map(function (i) {
    return { x: pad.l + (i.mz - lo) / (hi - lo) * iw,
             y: pad.t + ih - Math.max(0, i.intensity) / top * ih,
             y0: pad.t + ih, possible: i.possible };
  });
  var xticks = axisTicks(lo, hi, 6).map(function (v) {
    return { v: v, x: pad.l + (v - lo) / (hi - lo) * iw };
  });
  var yticks = [0, 25, 50, 75, 100].map(function (p) {
    return { v: p, y: pad.t + ih - p / 100 * ih };
  });
  return { sticks: sticks, xticks: xticks, yticks: yticks, iw: iw, ih: ih };
}

if (typeof module !== "undefined") {
  module.exports = { summaryText: summaryText, axisTicks: axisTicks, plotModel: plotModel };
}

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

  function drawPlot(data) {
    var W = 720, H = 300, pad = { l: 46, r: 14, t: 14, b: 62 };
    var m = plotModel(data.ions, W, H, pad);
    var root = svg("svg", { viewBox: "0 0 " + W + " " + H, role: "img",
                            "aria-label": "Stick plot of ion intensity against m/z" });
    root.appendChild(svg("line", { x1: pad.l, y1: pad.t + m.ih, x2: pad.l + m.iw, y2: pad.t + m.ih, stroke: "#333" }));
    root.appendChild(svg("line", { x1: pad.l, y1: pad.t, x2: pad.l, y2: pad.t + m.ih, stroke: "#333" }));
    m.yticks.forEach(function (t) {
      root.appendChild(svg("line", { x1: pad.l - 4, y1: t.y, x2: pad.l, y2: t.y, stroke: "#333" }));
      root.appendChild(svg("text", { x: pad.l - 7, y: t.y + 4, "text-anchor": "end", "font-size": 11, fill: "#333" }, String(t.v)));
    });
    m.xticks.forEach(function (t) {
      root.appendChild(svg("line", { x1: t.x, y1: pad.t + m.ih, x2: t.x, y2: pad.t + m.ih + 4, stroke: "#333" }));
      root.appendChild(svg("text", { x: t.x, y: pad.t + m.ih + 17, "text-anchor": "middle", "font-size": 11, fill: "#333" }, fmtTick(t.v)));
    });
    root.appendChild(svg("text", { x: pad.l + m.iw / 2, y: H - 24, "text-anchor": "middle", "font-size": 12, fill: "#333" }, "m/z"));
    root.appendChild(svg("text", { x: 12, y: pad.t + m.ih / 2, "font-size": 12, fill: "#333", "text-anchor": "middle",
                                   transform: "rotate(-90 12 " + (pad.t + m.ih / 2) + ")" }, "relative intensity (%)"));
    // possible first so impossible sticks draw on top
    [true, false].forEach(function (flag) {
      m.sticks.forEach(function (s) {
        if (s.possible !== flag) return;
        root.appendChild(svg("line", { x1: s.x, y1: s.y0, x2: s.x, y2: s.y,
                                       stroke: flag ? BLUE : RED, "stroke-width": 2 }));
      });
    });
    var np = data.summary.total - data.summary.impossible, ni = data.summary.impossible;
    var ly = H - 8;
    root.appendChild(svg("rect", { x: pad.l, y: ly - 10, width: 12, height: 12, fill: BLUE }));
    root.appendChild(svg("text", { x: pad.l + 18, y: ly, "font-size": 12, fill: "#222" }, "possible (" + np + ")"));
    root.appendChild(svg("rect", { x: pad.l + 140, y: ly - 10, width: 12, height: 12, fill: RED }));
    root.appendChild(svg("text", { x: pad.l + 158, y: ly, "font-size": 12, fill: "#222" }, "impossible (" + ni + ")"));
    var box = $("plot");
    box.textContent = "";
    box.appendChild(root);
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
      if (x.r.status === 200 && x.j) { drawPlot(x.j); fillTable(x.j.ions); }
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
