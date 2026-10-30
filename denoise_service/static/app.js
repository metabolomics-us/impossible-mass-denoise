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
  var NS = "http://www.w3.org/2000/svg";
  var $ = function (id) { return document.getElementById(id); };
  var lastResponse = null;

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

  function render(data) {
    lastResponse = data;
    $("summary").textContent = summaryText(data.summary);
    drawPlot(data);
    fillTable(data.ions);
    $("copystatus").textContent = "";
    $("result").hidden = false;
  }

  function submit(ev) {
    ev.preventDefault();
    showMessage("");
    var picked = document.querySelector('input[name="mode"]:checked');
    if (!picked) { showMessage("Choose a polarity (neg or pos) before classifying."); return; }
    var body = { mode: picked.value, spectrum: $("spectrum").value, halogens: $("halogens").checked };
    var pre = $("precursor").value.trim();
    if (pre !== "") {
      var n = Number(pre);
      if (!isFinite(n) || n <= 0) { showMessage("Precursor m/z must be a positive number."); return; }
      body.precursor_mz = n;
    }
    var go = $("go");
    go.disabled = true;
    fetch("/v1/classify", { method: "POST", headers: { "Content-Type": "application/json" },
                            body: JSON.stringify(body) })
      .then(function (r) {
        return r.json().then(function (j) { return { r: r, j: j }; }, function () { return { r: r, j: null }; });
      })
      .then(function (x) {
        if (x.r.status === 200 && x.j) { render(x.j); return; }
        $("result").hidden = true;
        var detail = x.j && typeof x.j.detail === "string" ? x.j.detail : "request failed (HTTP " + x.r.status + ")";
        var retry = x.r.headers.get("Retry-After");
        if ((x.r.status === 429 || x.r.status === 503) && retry) detail += " (retry after " + retry + " s)";
        showMessage(detail);
      })
      .catch(function () { showMessage("Could not reach the service. Check your connection and try again."); })
      .then(function () { go.disabled = false; });
  }

  function copyJson() {
    if (!lastResponse) return;
    var text = JSON.stringify(lastResponse, null, 2), status = $("copystatus");
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        function () { status.textContent = "Copied."; },
        function () { status.textContent = "Copy failed; select the table and copy manually."; });
    } else {
      status.textContent = "Clipboard unavailable in this browser; use the API docs link to fetch JSON directly.";
    }
  }

  $("form").addEventListener("submit", submit);
  $("copy").addEventListener("click", copyJson);
})();
