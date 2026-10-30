"use strict";
/* Pure helpers shared by the page and the tests (loaded by node as well as by the browser).
   The record splitting mirrors denoise_service/convert.py so client chunks end exactly where the
   server's records end; a test compares the two. */
(function (root) {
  var LINE = /[^\r\n]*(?:\r\n|\n|\r)|[^\r\n]+/g;
  var TOKEN = /[^\s;,]+/g;
  var NUM_PEAKS = /^\s*num[ _-]?peaks\s*:/i;
  var NONFINITE_PEAK = /^[+-]?(?:nan|inf|infinity):/i;

  function splitLines(text) { return text.match(LINE) || []; }
  function tokens(s) { return s.match(TOKEN) || []; }
  function isHeader(s) { return /^\p{L}/u.test(s) && s.indexOf(":") >= 0 && !NONFINITE_PEAK.test(s); }
  function isPairLine(s) {
    var t = tokens(s);
    return t.length > 0 && t.every(function (x) { return /^[^:\s]+:[^:\s]+$/.test(x); });
  }

  function byteLen(s) {
    if (typeof TextEncoder !== "undefined") return new TextEncoder().encode(s).length;
    return unescape(encodeURIComponent(s)).length;
  }

  /* "msp" | "mgf" | "strings" | "columns", the same rule as the server. */
  function detectKind(text) {
    var lines = splitLines(text).map(function (l) { return l.trim(); }).filter(Boolean);
    if (lines.some(function (l) { return l.toUpperCase() === "BEGIN IONS"; })) return "mgf";
    if (lines.some(function (l) { return /^name\s*:/i.test(l) || NUM_PEAKS.test(l); })) return "msp";
    if (lines.length && lines.every(isPairLine)) {
      if (lines.length === 1 || lines.some(function (l) { return tokens(l).length > 1; })) return "strings";
    }
    return "columns";
  }

  function lineKind(s, kind, inside) {
    if (!s) return "b";
    if (kind === "strings") return "p";
    if (kind === "mgf") {
      if (s.toUpperCase() === "BEGIN IONS" || s.toUpperCase() === "END IONS" || !inside) return "h";
      return /^\p{L}/u.test(s) && s.indexOf("=") >= 0 ? "h" : "p";
    }
    return isHeader(s) ? "h" : "p";
  }

  /* Records of `text` as strings; their concatenation is the input. */
  function splitRecords(text, kind) {
    var lines = splitLines(text), groups = [], lead = [];
    function start() { var g = lead.splice(0, lead.length); groups.push(g); return g; }
    function put(line) { if (groups.length) groups[groups.length - 1].push(line); else lead.push(line); }
    var i, s, k;
    if (kind === "mgf") {
      var inside = false;
      for (i = 0; i < lines.length; i++) {
        s = lines[i].trim();
        if (s.toUpperCase() === "BEGIN IONS") { start(); inside = true; }
        else if (s.toUpperCase() === "END IONS") inside = false;
        groups.length ? groups[groups.length - 1].push(lines[i]) : lead.push(lines[i]);
      }
    } else if (kind === "strings") {
      for (i = 0; i < lines.length; i++) {
        s = lines[i].trim();
        if (s) start().push(lines[i]); else put(lines[i]);
      }
    } else {
      var cur = null, hasPeaks = false, hasName = false, blankAfterPeak = false;
      for (i = 0; i < lines.length; i++) {
        s = lines[i].trim();
        k = lineKind(s, kind, false);
        if (kind === "msp" && k === "h") {
          var isName = /^name\s*:/i.test(s);
          if (cur && ((hasPeaks && (blankAfterPeak || isName)) || (isName && hasName))) cur = null;
          if (!cur) { cur = start(); hasPeaks = hasName = blankAfterPeak = false; }
          hasName = hasName || isName;
        } else if (!cur) {
          if (k === "b") { put(lines[i]); continue; }
          cur = start();
        }
        if (k === "p") { hasPeaks = true; blankAfterPeak = false; }
        else if (k === "b" && hasPeaks) blankAfterPeak = true;
        cur.push(lines[i]);
      }
    }
    if (lead.length && !groups.length) start();
    return groups.map(function (g) { return g.join(""); });
  }

  /* Index ranges [start, end) over `groups` holding at most maxRecords records and maxBytes bytes
     (a single record may exceed the byte limit; it can only go alone). */
  function packRanges(groups, maxBytes, maxRecords) {
    var out = [], start = 0, bytes = 0;
    for (var i = 0; i < groups.length; i++) {
      var b = byteLen(groups[i]);
      if (i > start && (i - start >= maxRecords || bytes + b > maxBytes)) {
        out.push([start, i]); start = i; bytes = 0;
      }
      bytes += b;
    }
    if (groups.length) out.push([start, groups.length]);
    return out;
  }

  /* Chunks of `text` cut at record boundaries; joined they equal `text`. */
  function splitIntoChunks(text, maxBytes, maxRecords) {
    var groups = splitRecords(text, detectKind(text));
    if (!groups.length) return [text];
    return packRanges(groups, maxBytes, maxRecords).map(function (r) {
      return groups.slice(r[0], r[1]).join("");
    });
  }

  /* Only the peak lines of one record, in a form /v1/classify accepts. */
  function peakText(raw, kind) {
    var inside = false, out = [];
    splitLines(raw).forEach(function (line) {
      var s = line.trim(), u = s.toUpperCase();
      if (kind === "mgf") {
        if (u === "BEGIN IONS") { inside = true; return; }
        if (u === "END IONS") { inside = false; return; }
      }
      if (lineKind(s, kind, inside) !== "p") return;
      var t = tokens(s);
      out.push(kind === "mgf" && t[0].indexOf(":") < 0 ? t.slice(0, 2).join(" ") : s);
    });
    return out.join("\n");
  }

  function cleanedFilename(name, format) {
    var ext = { msp: "msp", mgf: "mgf" }[format] || "txt";
    if (!name) return "cleaned." + ext;
    var dot = name.lastIndexOf(".");
    if (dot > 0) return name.slice(0, dot) + ".cleaned" + name.slice(dot);
    return name + ".cleaned." + ext;
  }

  /* Download name: `<name>.cleaned.<ext>`, or `<name>.partial.<ext>` when records are missing. */
  function outputFilename(name, format, partial) {
    var tag = partial ? ".partial" : ".cleaned";
    var ext = { msp: "msp", mgf: "mgf" }[format] || "txt";
    if (!name) return (partial ? "partial." : "cleaned.") + ext;
    var dot = name.lastIndexOf(".");
    if (dot > 0) return name.slice(0, dot) + tag + name.slice(dot);
    return name + tag + "." + ext;
  }

  function partialNote(missing, total) {
    if (!(missing > 0)) return "";
    return missing + " of " + total + " records are missing from this output (not processed).";
  }

  /* A single record the server refuses as too large cannot be split further. */
  function isRecordTooLarge(status, count) {
    return (status === 413 || status === 422) && count <= 1;
  }

  /* The /v1/convert result for one record left exactly as it came in. */
  function passThroughResult(raw, format) {
    return { format: format, text: raw,
             records: [{ index: 0, name: null, total: 0, impossible: 0,
                         error: "passed through: too large", mode: null, precursor_mz: null }],
             summary: { records: 1, cleaned: 0, passed_through: 1, peaks: 0, impossible: 0 } };
  }

  /* A range {lo, hi} scaled by `factor` (< 1 zooms in) about `center`, kept inside `bounds`. */
  function zoomRange(range, factor, center, bounds) {
    var span = (range.hi - range.lo) * factor;
    var minSpan = bounds ? (bounds.hi - bounds.lo) * 1e-4 : 1e-9;
    if (bounds) span = Math.min(span, bounds.hi - bounds.lo);
    span = Math.max(span, minSpan);
    var t = (center - range.lo) / (range.hi - range.lo);
    var lo = center - t * span;
    return clampRange({ lo: lo, hi: lo + span }, bounds);
  }

  function panRange(range, dx, bounds) {
    return clampRange({ lo: range.lo + dx, hi: range.hi + dx }, bounds);
  }

  function clampRange(r, bounds) {
    if (!bounds) return r;
    var span = r.hi - r.lo;
    if (r.lo < bounds.lo) return { lo: bounds.lo, hi: bounds.lo + span };
    if (r.hi > bounds.hi) return { lo: bounds.hi - span, hi: bounds.hi };
    return r;
  }

  function niceTicks(lo, hi, target) {
    if (!(hi > lo)) return [lo];
    var raw = (hi - lo) / (target || 6), mag = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / mag;
    var step = (f < 1.5 ? 1 : f < 3.5 ? 2 : f < 7.5 ? 5 : 10) * mag, out = [];
    for (var v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) {
      out.push(parseFloat((Math.round(v / step) * step).toPrecision(12)));
    }
    return out;
  }

  var api = { byteLen: byteLen, detectKind: detectKind, splitRecords: splitRecords,
              packRanges: packRanges, splitIntoChunks: splitIntoChunks, peakText: peakText,
              cleanedFilename: cleanedFilename,
              outputFilename: outputFilename, partialNote: partialNote,
              isRecordTooLarge: isRecordTooLarge, passThroughResult: passThroughResult, zoomRange: zoomRange, panRange: panRange,
              niceTicks: niceTicks };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.DenoiseLib = api;
})(typeof self !== "undefined" ? self : this);
