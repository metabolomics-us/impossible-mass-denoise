"use strict";
/* Pure helpers shared by the page and the tests (loaded by node as well as by the browser).
   The record splitting mirrors denoise_service/convert.py so client chunks end exactly where the
   server's records end; a test compares the two. */
(function (root) {
  var LINE = /[^\r\n]*(?:\r\n|\n|\r)|[^\r\n]+/g;
  /* Python's str.strip()/\s set (JS's \s and trim() differ: they include U+FEFF, and lack U+001C-001F
     and U+0085). Used everywhere so both sides agree on blank lines and record boundaries. */
  var WS = "\\t\\n\\v\\f\\r \\x1c-\\x1f\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
  var TOKEN = new RegExp("[^" + WS + ";,]+", "g");
  var NUM_PEAKS = new RegExp("^[" + WS + "]*num[ _-]?peaks[" + WS + "]*:", "i");
  var NAME_LINE = new RegExp("^name[" + WS + "]*:", "i");
  var WS_CHAR = new RegExp("^[" + WS + "]$");

  function strip(s) {
    var a = 0, b = s.length;
    while (a < b && WS_CHAR.test(s.charAt(a))) a++;
    while (b > a && WS_CHAR.test(s.charAt(b - 1))) b--;
    return s.slice(a, b);
  }
  function stripBom(text) { return text.charAt(0) === "\ufeff" ? text.slice(1) : text; }
  var NONFINITE_PEAK = /^[+-]?(?:nan|inf|infinity):/i;

  var PAIRISH = new RegExp("^[^:" + WS + "]+:[^:" + WS + "]+$");

  function splitLines(text) { return text.match(LINE) || []; }
  function tokens(s) { return s.match(TOKEN) || []; }
  function isHeader(s) { return /^\p{L}/u.test(s) && s.indexOf(":") >= 0 && !NONFINITE_PEAK.test(s); }
  function isPairLine(s) {
    var t = tokens(s);
    return t.length > 0 && t.every(function (x) { return PAIRISH.test(x); });
  }

  /* UTF-8 length of a string without allocating (a 1 GiB file is measured one record at a time). */
  function byteLen(s) {
    var n = 0, c;
    for (var i = 0, len = s.length; i < len; i++) {
      c = s.charCodeAt(i);
      if (c < 0x80) n += 1;
      else if (c < 0x800) n += 2;
      else if (c >= 0xd800 && c <= 0xdbff && i + 1 < len && (s.charCodeAt(i + 1) & 0xfc00) === 0xdc00) { n += 4; i++; }
      else n += 3;
    }
    return n;
  }

  /* "msp" | "mgf" | "strings" | "columns", the same rule as the server. */
  function detectKind(text) {
    text = stripBom(text);
    var lines = splitLines(text).map(function (l) { return strip(l); }).filter(Boolean);
    if (lines.some(function (l) { return l.toUpperCase() === "BEGIN IONS"; })) return "mgf";
    if (lines.some(function (l) { return NAME_LINE.test(l) || NUM_PEAKS.test(l); })) return "msp";
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

  /* Incremental record splitter. push(text) takes the next slice of the file and returns the records
     that are COMPLETE; end() flushes the rest. Lines are cut only at line terminators (a trailing
     "\r" waits for a possible "\n" in the next slice), so the result equals splitRecords() of the
     whole text however it is sliced. With no `kind`, the kind is detected from the first
     `detectMin` characters (or at end()). A BOM is dropped at the very start only. */
  function createSplitter(opts) {
    opts = opts || {};
    var kind = opts.kind || null, detectMin = opts.detectMin || 65536;
    var carry = "", head = [], headLen = 0, bomChecked = false;
    var lead = [], last = null, cur = null, out = [], inside = false;
    var hasPeaks = false, hasName = false, blankAfterPeak = false;

    function emitLast() { if (last) out.push(last.join("")); }
    function start() { emitLast(); last = lead.splice(0, lead.length); return last; }
    function put(line) { (last || lead).push(line); }

    function processLine(raw) {
      var s = strip(raw), k, u;
      if (kind === "mgf") {
        u = s.toUpperCase();
        if (u === "BEGIN IONS") { start(); inside = true; }
        else if (u === "END IONS") inside = false;
        put(raw);
      } else if (kind === "strings") {
        if (s) start().push(raw); else put(raw);
      } else {
        k = lineKind(s, kind, false);
        if (kind === "msp" && k === "h") {
          var isName = NAME_LINE.test(s);
          if (cur && ((hasPeaks && (blankAfterPeak || isName)) || (isName && hasName))) cur = null;
          if (!cur) { cur = start(); hasPeaks = hasName = blankAfterPeak = false; }
          hasName = hasName || isName;
        } else if (!cur) {
          if (k === "b") { put(raw); return; }
          cur = start();
        }
        if (k === "p") { hasPeaks = true; blankAfterPeak = false; }
        else if (k === "b" && hasPeaks) blankAfterPeak = true;
        cur.push(raw);
      }
    }

    function processText(text) {
      var lines = splitLines(text);
      for (var i = 0; i < lines.length; i++) processLine(lines[i]);
    }
    function decide() {
      var text = head.join("");
      head = []; headLen = 0;
      kind = detectKind(text);
      processText(text);
    }
    function accept(text) {
      if (!text) return;
      if (kind) { processText(text); return; }
      head.push(text); headLen += text.length;
      if (headLen >= detectMin) decide();
    }
    function take() { return out.splice(0, out.length); }

    return {
      kind: function () { return kind; },
      push: function (text) {
        carry += text;
        if (!bomChecked && carry.length) { bomChecked = true; carry = stripBom(carry); }
        var end = carry.length;
        if (end && carry.charAt(end - 1) === "\r") end--;
        var cut = Math.max(carry.lastIndexOf("\n", end - 1), carry.lastIndexOf("\r", end - 1));
        if (cut < 0) return take();
        var done = carry.slice(0, cut + 1);
        carry = carry.slice(cut + 1);
        accept(done);
        return take();
      },
      end: function () {
        var rest = carry;
        carry = "";
        accept(rest);
        if (!kind) decide();
        if (lead.length && !last) start();
        emitLast();
        last = null;
        return take();
      }
    };
  }

  /* Records of `text` as strings; their concatenation is the input (minus a leading BOM). */
  function splitRecords(text, kind) {
    var sp = createSplitter({ kind: kind || detectKind(text) });
    return sp.push(text).concat(sp.end());
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

  /* Incremental packer: push(record) returns the requests {records, bytes} that just became full;
     end() returns the last one. Same limits and cuts as packRanges. */
  function createPacker(maxBytes, maxRecords) {
    var recs = [], bytes = 0;
    return {
      push: function (rec) {
        var out = [], b = byteLen(rec);
        if (recs.length && (recs.length >= maxRecords || bytes + b > maxBytes)) {
          out.push({ records: recs, bytes: bytes });
          recs = []; bytes = 0;
        }
        recs.push(rec); bytes += b;
        return out;
      },
      end: function () {
        var out = recs.length ? [{ records: recs, bytes: bytes }] : [];
        recs = []; bytes = 0;
        return out;
      }
    };
  }

  /* ---- streaming pipeline pieces ----------------------------------------------------------- */

  /* Reads a Blob/File as text in slices of about `sliceBytes`, decoding with a streaming TextDecoder
     so a multi-byte character cut by a slice edge stays whole. read() resolves to a string (possibly
     empty) or null at the end. Uses blob.stream() when present, else Blob.slice + arrayBuffer or
     FileReader. Never holds more than one slice. */
  function createBlobReader(blob, opts) {
    opts = opts || {};
    var sliceBytes = opts.sliceBytes || 4 * 1024 * 1024;
    var decoder = new TextDecoder("utf-8"), bytes = 0, finished = false;
    if (typeof blob.stream === "function" && !opts.forceSlice) {
      var reader = blob.stream().getReader();
      return {
        bytesRead: function () { return bytes; },
        cancel: function () { finished = true; return reader.cancel().catch(function () {}); },
        read: function () {
          if (finished) return Promise.resolve(null);
          var parts = [], got = 0;
          function step() {
            return reader.read().then(function (r) {
              if (r.done) {
                finished = true;
                var tail = decoder.decode();
                if (tail) parts.push(tail);
                return parts.length ? parts.join("") : null;
              }
              bytes += r.value.length; got += r.value.length;
              parts.push(decoder.decode(r.value, { stream: true }));
              return got >= sliceBytes ? parts.join("") : step();
            });
          }
          return step();
        }
      };
    }
    var offset = 0, flushed = false;
    function readBuffer(part) {
      if (typeof part.arrayBuffer === "function") return part.arrayBuffer();
      return new Promise(function (resolve, reject) {
        var fr = new FileReader();
        fr.onload = function () { resolve(fr.result); };
        fr.onerror = function () { reject(fr.error || new Error("read failed")); };
        fr.readAsArrayBuffer(part);
      });
    }
    return {
      bytesRead: function () { return bytes; },
      cancel: function () { finished = true; return Promise.resolve(); },
      read: function () {
        if (finished) return Promise.resolve(null);
        if (offset >= blob.size) {
          finished = true;
          var tail = decoder.decode();
          return Promise.resolve(tail || null);
        }
        var part = blob.slice(offset, Math.min(blob.size, offset + sliceBytes));
        offset += part.size;
        return readBuffer(part).then(function (buf) {
          bytes += buf.byteLength;
          return decoder.decode(buf, { stream: true });
        });
      }
    };
  }

  /* Reader over a string already in memory (pasted text), in slices of `sliceChars`. */
  function createTextReader(text, sliceChars) {
    var pos = 0, step = sliceChars || 1 << 20;
    return {
      bytesRead: function () { return Math.min(pos, text.length); },
      cancel: function () { pos = text.length; return Promise.resolve(); },
      read: function () {
        if (pos >= text.length) return Promise.resolve(null);
        var out = text.slice(pos, pos + step);
        pos += step;
        return Promise.resolve(out);
      }
    };
  }

  function addSummary(into, from) {
    for (var key in into) into[key] += from.summary[key] || 0;
  }

  /* One /v1/convert result for several results in file order. */
  function mergeResults(list) {
    var out = { format: list[0].format, text: "", records: [],
                summary: { records: 0, cleaned: 0, passed_through: 0, peaks: 0, impossible: 0 } };
    var parts = [];
    list.forEach(function (r) {
      parts.push(r.text);
      Array.prototype.push.apply(out.records, r.records);
      addSummary(out.summary, r);
    });
    out.text = parts.join("");
    return out;
  }

  function sleepUnless(ms, isCancelled) {
    return new Promise(function (resolve) {
      var left = ms;
      (function tick() {
        if (left <= 0 || isCancelled()) { resolve(); return; }
        var d = Math.min(200, left);
        left -= d;
        setTimeout(tick, d);
      })();
    });
  }

  function detailOf(x) {
    return x.json && typeof x.json.detail === "string" ? x.json.detail : "request failed (HTTP " + x.status + ")";
  }

  /* Convert `records` (strings) through o.post(body) -> Promise<{status, json, retryAfter}>.
     Retries 429/503 after Retry-After, halves on 413/422, passes a lone oversized record through.
     Resolves to one merged result; rejects {cancelled} or {message}. */
  function convertRecords(o, records) {
    var tries = 0, maxRetries = o.maxRetries === undefined ? 5 : o.maxRetries;
    function attempt() {
      if (o.isCancelled()) return Promise.reject({ cancelled: true });
      var body = { text: records.join(""), format: o.kind };
      for (var k in o.opts) body[k] = o.opts[k];
      return o.post(body).then(function (x) {
        if (x.status === 200 && x.json) return x.json;
        var st = x.status;
        if (isRecordTooLarge(st, records.length)) return passThroughResult(records[0], o.kind);
        if ((st === 413 || st === 422) && records.length > 1) {
          var mid = Math.ceil(records.length / 2);
          return convertRecords(o, records.slice(0, mid)).then(function (a) {
            return convertRecords(o, records.slice(mid)).then(function (b) { return mergeResults([a, b]); });
          });
        }
        if ((st === 429 || st === 503) && tries < maxRetries) {
          tries++;
          var wait = Math.min(o.maxWaitSeconds || 30, Math.max(o.minWaitSeconds || 1, x.retryAfter || 1));
          if (o.onStatus) o.onStatus("Server busy; retrying in " + wait + " s (" + tries + " of " + maxRetries + ")");
          return sleepUnless(wait * 1000, o.isCancelled).then(attempt);
        }
        return Promise.reject({ message: detailOf(x) });
      }, function (err) {
        if (o.isCancelled()) return Promise.reject({ cancelled: true });
        return Promise.reject(err && err.message ? { message: "Could not reach the service. Check your connection and try again." } : err);
      });
    }
    return attempt();
  }

  function yieldToEventLoop() {
    return new Promise(function (resolve) { setTimeout(resolve, 0); });
  }

  /* The scheduler. Reads slices with o.read(), splits and packs them on the fly, and runs the packed
     requests through o.send(req) (req = {seq, records, text, bytes}) with at most `concurrency` in
     flight. Reading pauses while more than `maxQueued` requests wait, so memory stays flat however
     large the file is. Results reach o.onResult(req, res) strictly in request order. A failed
     request stops reading and dispatch; results contiguous with the delivered ones are still
     delivered, then the run ends with {error}. Resolves (never rejects) to
     {completed, error, cancelled, readDone, recordsRead, recordsDone, bytesDone, requests}. */
  function runPipeline(o) {
    var conc = o.concurrency || 3, maxQueued = o.maxQueued === undefined ? 8 : o.maxQueued;
    var window = o.window || conc + maxQueued + 4, piece = o.pieceChars || 262144;
    var yielder = o.yieldFn || yieldToEventLoop;
    var splitter = createSplitter({ kind: o.kind, detectMin: o.detectMin });
    var packer = createPacker(o.maxBytes || 400000, o.maxRecords || 100);
    var queue = [], inflight = 0, nextSeq = 0, nextOut = 0, results = {}, pending = "";
    var readDone = false, failed = null, wake = null, settled = false, kindSeen = false;
    var st = { recordsRead: 0, recordsDone: 0, bytesDone: 0, requests: 0 };
    var finish;

    function progress() { if (o.onProgress) o.onProgress(st, { queued: queue.length, inflight: inflight }); }
    function poke() { if (wake) { var w = wake; wake = null; w(); } }
    function fail(err) { if (!failed) failed = err; poke(); check(); }
    function enqueue(reqs) {
      noteKind();
      reqs.forEach(function (r) {
        r.seq = nextSeq++;
        r.kind = splitter.kind();
        r.text = r.records.join("");
        st.recordsRead += r.records.length;
        queue.push(r);
      });
      pump();
    }
    function noteKind() {
      if (!kindSeen && splitter.kind()) { kindSeen = true; if (o.onKind) o.onKind(splitter.kind()); }
    }
    function drain() {
      while (!failed && results[nextOut]) {
        var item = results[nextOut];
        delete results[nextOut];
        nextOut++;
        try { o.onResult(item.req, item.res); } catch (e) { fail({ message: (e && e.message) || "could not write the output" }); return; }
        st.recordsDone += item.req.records.length;
        st.bytesDone += item.req.bytes;
        st.requests++;
      }
      progress();
    }
    function dispatch(req) {
      inflight++;
      var p;
      try { p = o.send(req); } catch (e) { p = Promise.reject(e); }
      p.then(function (res) {
        inflight--;
        results[req.seq] = { req: req, res: res };
        drain(); pump();
      }, function (err) {
        inflight--;
        fail(err);
      });
    }
    function pump() {
      while (!failed && inflight < conc && queue.length && queue[0].seq - nextOut < window) dispatch(queue.shift());
      poke();
      check();
    }
    /* A failed run keeps delivering what is contiguous with the prefix while requests settle. */
    function check() {
      if (settled) return;
      if (failed) {
        if (inflight > 0) return;
        drain2();
      } else if (!(readDone && !queue.length && inflight === 0)) return;
      settled = true;
      finish({ completed: !failed && readDone, error: failed && !failed.cancelled ? failed : null,
               cancelled: !!(failed && failed.cancelled), readDone: readDone,
               recordsRead: st.recordsRead, recordsDone: st.recordsDone, bytesDone: st.bytesDone,
               requests: st.requests });
    }
    function drain2() {
      var f = failed; failed = null;
      drain();
      failed = f;
    }
    function waitForRoom() { return new Promise(function (resolve) { wake = resolve; }); }

    function produce() {
      if (failed) return Promise.resolve();
      if (o.isCancelled && o.isCancelled()) { fail({ cancelled: true }); return Promise.resolve(); }
      if (queue.length > maxQueued || nextSeq - nextOut >= window) return waitForRoom().then(produce);
      if (pending.length) {
        var piecetext = pending.slice(0, piece);
        pending = pending.slice(piece);
        splitter.push(piecetext).forEach(function (rec) { enqueue(packer.push(rec)); });
        noteKind();
        progress();
        return yielder().then(produce);
      }
      return o.read().then(function (text) {
        if (text === null) {
          splitter.end().forEach(function (rec) { enqueue(packer.push(rec)); });
          noteKind();
          enqueue(packer.end());
          readDone = true;
          progress();
          pump();
          return;
        }
        pending = text;
        return produce();
      });
    }

    return new Promise(function (resolve) {
      finish = resolve;
      produce().then(null, function (e) { fail({ message: (e && e.message) || "could not read the file" }); });
    });
  }

  /* Disk-friendly output. add(text) keeps the text as Blob parts, folded into one Blob every
     `flushBytes` so the engine can back it with a file; or, with `writable` (anything with
     write(string) and close(), e.g. a FileSystemWritableFileStream), streams each piece straight
     out and keeps nothing. finish() resolves to the Blob, or null when streamed. */
  function createOutput(opts) {
    opts = opts || {};
    var flushBytes = opts.flushBytes || 8 * 1024 * 1024, writable = opts.writable || null;
    var parts = [], partBytes = 0, size = 0, chain = Promise.resolve(), error = null;
    return {
      size: function () { return size; },
      error: function () { return error; },
      add: function (text) {
        if (!text) return;
        size += byteLen(text);
        if (writable) {
          chain = chain.then(function () { return writable.write(text); }).catch(function (e) { error = error || e; });
          return;
        }
        parts.push(text); partBytes += text.length;
        if (partBytes >= flushBytes) { parts = [new Blob(parts)]; partBytes = 0; }
      },
      finish: function () {
        return chain.then(function () {
          if (writable) return writable.close().then(function () { return null; }, function (e) { error = error || e; return null; });
          return new Blob(parts, { type: "text/plain;charset=utf-8" });
        });
      }
    };
  }

  /* Running totals for a run, with per-record detail kept only for the first `limit` records and
     for records that were not cleaned (up to `limit` more). Everything else only counts. */
  function createAccumulator(limit) {
    limit = limit || 1000;
    var acc = { total: 0, format: null, retained: [], errorsKept: 0, errors: {}, errorKinds: 0,
                summary: { records: 0, cleaned: 0, passed_through: 0, peaks: 0, impossible: 0 } };
    acc.add = function (req, res) {
      var same = res.records.length === req.records.length;
      acc.format = acc.format || res.format;
      res.records.forEach(function (rec, j) {
        var idx = acc.total++;
        rec.index = idx;
        if (rec.error) {
          if (acc.errors[rec.error] || acc.errorKinds < 50) {
            if (!acc.errors[rec.error]) acc.errorKinds++;
            acc.errors[rec.error] = (acc.errors[rec.error] || 0) + 1;
          }
        }
        var keep = idx < limit || (rec.error && acc.errorsKept < limit);
        if (!keep) return;
        if (idx >= limit) acc.errorsKept++;
        acc.retained.push({ rec: rec, raw: same ? req.records[j] : null });
      });
      addSummary(acc.summary, res);
    };
    return acc;
  }

  /* Smoothed throughput. add(bytes, nowMs) after each unit of work; rate() is bytes/second (an
     exponential average over windows of at least `windowMs`), eta(remainingBytes) seconds or null. */
  function createRateMeter(windowMs) {
    windowMs = windowMs || 750;
    var t0 = null, since = 0, ema = null, tLast = null;
    return {
      add: function (bytes, now) {
        if (t0 === null) { t0 = tLast = now; }
        since += bytes;
        if (now - tLast >= windowMs) {
          var inst = since / ((now - tLast) / 1000);
          ema = ema === null ? inst : 0.3 * inst + 0.7 * ema;
          since = 0; tLast = now;
        }
      },
      rate: function () { return ema; },
      eta: function (remaining) { return ema && ema > 0 ? remaining / ema : null; }
    };
  }

  function formatBytes(n) {
    if (n < 1024) return n + " B";
    var u = ["KB", "MB", "GB", "TB"], i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
    return (n >= 100 ? n.toFixed(0) : n >= 10 ? n.toFixed(1) : n.toFixed(2)) + " " + u[i];
  }

  function formatDuration(sec) {
    if (sec === null || sec === undefined || !isFinite(sec)) return "estimating...";
    sec = Math.max(0, Math.round(sec));
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    if (h) return h + " h " + (m < 10 ? "0" : "") + m + " min";
    if (m) return m + " min " + (s < 10 ? "0" : "") + s + " s";
    return s + " s";
  }

  /* Calm screen-reader cadence: true at most once per `gapMs`, and only after progress crossed
     another 10% step. */
  function createAnnouncer(gapMs) {
    gapMs = gapMs === undefined ? 8000 : gapMs;
    var lastT = null, step = 0;
    return function due(now, fraction) {
      var s = Math.floor(Math.max(0, Math.min(1, fraction)) * 10);
      if (s <= step) return false;
      if (lastT !== null && now - lastT < gapMs) return false;
      step = s; lastT = now;
      return true;
    };
  }

  var NUMBER = /^[+-]?(?:(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?|nan|inf|infinity)$/i;
  function isNum(t) { return t.length <= 64 && NUMBER.test(t); }
  /* "mz intensity annotation...": two numbers then text; one peak, as on the server. */
  function isAnnotated(t) {
    if (t.length < 3 || !isNum(t[0]) || !isNum(t[1]) || isNum(t[2])) return false;
    var p = t[2].split(":");
    return !(p.length === 2 && isNum(p[0]) && isNum(p[1]));
  }

  /* Only the peak lines of one record, in a form /v1/classify accepts. */
  function peakText(raw, kind) {
    var inside = false, out = [];
    splitLines(raw).forEach(function (line) {
      var s = strip(line), u = s.toUpperCase();
      if (kind === "mgf") {
        if (u === "BEGIN IONS") { inside = true; return; }
        if (u === "END IONS") { inside = false; return; }
      }
      if (lineKind(s, kind, inside) !== "p") return;
      var t = tokens(s);
      out.push(isAnnotated(t) ? t.slice(0, 2).join(" ") : s);
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

  function partialNote(missing, total, unread) {
    if (unread) return "Processing stopped before the whole file was read; " + missing + " of the " + total +
                       " records read so far, and everything after them, are missing from this output (not processed).";
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

  var api = { stripBom: stripBom, byteLen: byteLen, createSplitter: createSplitter, createPacker: createPacker,
              createBlobReader: createBlobReader, createTextReader: createTextReader, mergeResults: mergeResults,
              convertRecords: convertRecords, runPipeline: runPipeline, createOutput: createOutput,
              createAccumulator: createAccumulator, createRateMeter: createRateMeter, formatBytes: formatBytes,
              formatDuration: formatDuration, createAnnouncer: createAnnouncer, detectKind: detectKind, splitRecords: splitRecords,
              packRanges: packRanges, splitIntoChunks: splitIntoChunks, peakText: peakText,
              cleanedFilename: cleanedFilename,
              outputFilename: outputFilename, partialNote: partialNote,
              isRecordTooLarge: isRecordTooLarge, passThroughResult: passThroughResult, zoomRange: zoomRange, panRange: panRange,
              niceTicks: niceTicks };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.DenoiseLib = api;
})(typeof self !== "undefined" ? self : this);
