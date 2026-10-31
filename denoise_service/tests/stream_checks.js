"use strict";
/* Node-side checks for the streaming pieces of lib.js, driven by test_static_stream.py. Everything
   here runs the real code: real Blobs and TextDecoder, real HTTP servers, and (for `service`) the
   real denoise service. Usage: node stream_checks.js <check> '<json args>'; prints JSON. */
const http = require("http");
const fs = require("fs");
const os = require("os");
const path = require("path");
const L = require(path.join(__dirname, "..", "static", "lib.js"));
const [name, argjson] = process.argv.slice(2);
const A = JSON.parse(argjson || "null");

function rng(seed) {   // mulberry32
  let a = seed >>> 0;
  return function () {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function cutPoints(len, r, n) {
  const pts = new Set([0, len]);
  for (let i = 0; i < n; i++) pts.add(Math.floor(r() * (len + 1)));
  return Array.from(pts).sort((a, b) => a - b);
}

function splitAt(text, pts) {
  const sp = L.createSplitter({});
  let out = [];
  for (let i = 0; i + 1 < pts.length; i++) out = out.concat(sp.push(text.slice(pts[i], pts[i + 1])));
  return out.concat(sp.end());
}

function listen(handler) {
  return new Promise((resolve) => {
    const srv = http.createServer(handler);
    srv.listen(0, "127.0.0.1", () => resolve({ srv, url: "http://127.0.0.1:" + srv.address().port }));
  });
}

function postTo(base) {
  return function (body) {
    return fetch(base + "/v1/convert", { method: "POST", headers: { "Content-Type": "application/json" },
                                          body: JSON.stringify(body) })
      .then((r) => r.json().then((j) => ({ status: r.status, json: j, retryAfter: parseInt(r.headers.get("Retry-After"), 10) || 0 }),
                                  () => ({ status: r.status, json: null, retryAfter: 0 })));
  };
}

function readBody(req) {
  return new Promise((resolve) => { const b = []; req.on("data", (c) => b.push(c)); req.on("end", () => resolve(Buffer.concat(b).toString("utf8"))); });
}

/* A tiny server speaking the /v1/convert contract: every record's text is upper-cased, so output
   order is checkable, and each reply is delayed by a random time so replies finish out of order. */
function contractServer(opts) {
  const state = { active: 0, maxActive: 0, count: 0, bodies: [] };
  const r = rng(opts.seed || 1);
  return listen(async (req, res) => {
    const raw = await readBody(req);
    const body = JSON.parse(raw);
    const n = state.count++;
    state.active++; state.maxActive = Math.max(state.maxActive, state.active);
    state.bodies.push(body.text.length);
    await new Promise((ok) => setTimeout(ok, opts.delayMs ? Math.floor(r() * opts.delayMs) : 0));
    state.active--;
    const send = (st, obj, headers) => { res.writeHead(st, Object.assign({ "Content-Type": "application/json" }, headers || {})); res.end(JSON.stringify(obj)); };
    if (opts.failOn !== undefined && n === opts.failOn) return send(500, { detail: "boom" });
    if (opts.busyOn !== undefined && n === opts.busyOn) return send(429, { detail: "slow down" }, { "Retry-After": "1" });
    if (opts.maxText && body.text.length > opts.maxText) return send(413, { detail: "too large" });
    const recs = L.splitRecords(body.text, body.format);
    send(200, { format: body.format, text: body.text.toUpperCase(),
                records: recs.map((x, i) => ({ index: i, name: null, total: 1, impossible: 0, error: null, mode: "pos", precursor_mz: null })),
                summary: { records: recs.length, cleaned: recs.length, passed_through: 0, peaks: recs.length, impossible: 0 } });
  }).then((s) => Object.assign(s, { state }));
}

function msp(n, extra) {
  const out = [];
  for (let i = 0; i < n; i++) out.push("Name: rec" + i + (extra || "") + "\nIonMode: Positive\nNum Peaks: 2\n181.0707 100\n100.5 20\n\n");
  return out.join("");
}

function pipelineOpts(base, text, over) {
  const state = { cancelled: false };
  const o = Object.assign({
    read: null, onResult: null, detectMin: 1024, isCancelled: () => state.cancelled, opts: {},
    send: (req) => L.convertRecords({ post: postTo(base), kind: req.kind, opts: {}, isCancelled: () => state.cancelled,
                                      minWaitSeconds: 0 }, req.records),
  }, over || {});
  return { o, state };
}

const checks = {
  /* random slicing of one text must not change the records (compared with the server's Python) */
  splitter_slices(a) {
    const r = rng(a.seed || 7);
    const bad = [];
    for (let i = 0; i < a.rounds; i++) {
      const out = splitAt(a.text, cutPoints(a.text.length, r, Math.floor(r() * 40)));
      if (JSON.stringify(out) !== JSON.stringify(a.expected)) bad.push(i);
    }
    return { bad: bad, count: a.expected.length };
  },
  /* the same through real bytes, slices cut mid-character, in both reader modes */
  blob_reader(a) {
    const bytes = Buffer.from(a.text, "utf8");
    const results = [];
    const jobs = [];
    [true, false].forEach((forceSlice) => [1, 2, 3, 5, 64, 1000].forEach((sliceBytes) => {
      jobs.push((async () => {
        const rd = L.createBlobReader(new Blob([bytes]), { sliceBytes, forceSlice });
        const sp = L.createSplitter({});
        let out = [], t, text = "";
        while ((t = await rd.read()) !== null) { text += t; out = out.concat(sp.push(t)); }
        out = out.concat(sp.end());
        results.push({ forceSlice, sliceBytes, ok: JSON.stringify(out) === JSON.stringify(a.expected), same: text === a.decoded, read: rd.bytesRead() === bytes.length });
      })());
    }));
    return Promise.all(jobs).then(() => results);
  },
  packer(a) {
    const groups = L.splitRecords(a.text, L.detectKind(a.text));
    const ranges = L.packRanges(groups, a.maxBytes, a.maxRecords);
    const pk = L.createPacker(a.maxBytes, a.maxRecords);
    let reqs = [];
    groups.forEach((g) => { reqs = reqs.concat(pk.push(g)); });
    reqs = reqs.concat(pk.end());
    const viaRanges = ranges.map((rg) => groups.slice(rg[0], rg[1]));
    return { same: JSON.stringify(reqs.map((x) => x.records)) === JSON.stringify(viaRanges),
             bytesOk: reqs.every((x) => x.bytes === Buffer.byteLength(x.records.join(""))), n: reqs.length };
  },
  byte_len(a) { return a.map((s) => [L.byteLen(s), Buffer.byteLength(s)]); },

  /* ordering, concurrency <= 3, and backpressure against a server that answers out of order */
  async ordering(a) {
    const srv = await contractServer({ delayMs: 40, seed: 3 });
    const text = msp(a.records);
    const seen = [], maxQueued = 8;
    let maxBacklog = 0, firstResultRead = null, rd;
    const { o } = pipelineOpts(srv.url, text, {});
    rd = L.createTextReader(text, 4096);
    Object.assign(o, { read: () => rd.read(), maxBytes: 400000, maxRecords: a.maxRecords, pieceChars: 2048, detectMin: 1024, maxQueued: maxQueued,
      onResult: (req, res) => { seen.push(req.seq); if (firstResultRead === null) firstResultRead = rd.bytesRead(); },
      onProgress: (st, q) => { maxBacklog = Math.max(maxBacklog, q.queued); } });
    const t0 = Date.now();
    const res = await L.runPipeline(o);
    srv.srv.close();
    return { res, inOrder: seen.every((v, i) => v === i), n: seen.length, maxActive: srv.state.maxActive,
             maxBacklog, maxQueued, firstResultRead, total: text.length, ms: Date.now() - t0 };
  },
  async failure(a) {
    const srv = await contractServer({ failOn: a.failOn, delayMs: 20, seed: 5 });
    const text = msp(a.records), got = [];
    const { o } = pipelineOpts(srv.url, text, {});
    const rd = L.createTextReader(text, 8192);
    Object.assign(o, { read: () => rd.read(), maxRecords: 5, onResult: (req) => got.push(req.seq) });
    const res = await L.runPipeline(o);
    srv.srv.close();
    return { res, got, contiguous: got.every((v, i) => v === i) };
  },
  async retry_after(a) {
    const srv = await contractServer({ busyOn: 1, seed: 6 });
    const text = msp(a.records), got = [], notes = [];
    const { o, state } = pipelineOpts(srv.url, text, {});
    const rd = L.createTextReader(text, 8192);
    o.send = (req) => L.convertRecords({ post: postTo(srv.url), kind: req.kind, opts: {}, isCancelled: () => state.cancelled, onStatus: (t) => notes.push(t) }, req.records);
    Object.assign(o, { read: () => rd.read(), maxRecords: 5, onResult: (req) => got.push(req.seq) });
    const t0 = Date.now();
    const res = await L.runPipeline(o);
    srv.srv.close();
    return { res, got: got.length, ms: Date.now() - t0, notes };
  },
  async halving(a) {
    const srv = await contractServer({ maxText: a.maxText, seed: 8 });
    const text = msp(a.records), outs = [];
    const { o } = pipelineOpts(srv.url, text, {});
    const rd = L.createTextReader(text, 8192);
    Object.assign(o, { read: () => rd.read(), maxRecords: 10, onResult: (req, res) => outs.push(res) });
    const res = await L.runPipeline(o);
    srv.srv.close();
    return { res, text: outs.map((x) => x.text).join(""), upper: text.toUpperCase(), orig: text, posts: srv.state.count,
             passed: outs.reduce((n, x) => n + x.summary.passed_through, 0) };
  },
  async cancel(a) {
    const srv = await contractServer({ delayMs: 30, seed: 9 });
    const text = msp(a.records), got = [];
    const { o, state } = pipelineOpts(srv.url, text, {});
    const rd = L.createTextReader(text, 4096);
    Object.assign(o, { read: () => rd.read(), maxRecords: 5, onResult: (req) => { got.push(req.seq); if (got.length === 3) state.cancelled = true; } });
    const res = await L.runPipeline(o);
    srv.srv.close();
    return { res, got, readAll: rd.bytesRead() >= text.length, total: text.length };
  },
  async sink_failure() {
    const text = msp(30), { o } = pipelineOpts("http://127.0.0.1:1", text, {});
    const rd = L.createTextReader(text, 4096);
    Object.assign(o, { read: () => rd.read(), send: (req) => Promise.resolve({ text: req.text, records: [], summary: { records: 0 } }),
      maxRecords: 5, onResult: () => { throw new Error("disk full"); } });
    return L.runPipeline(o);
  },

  /* the real service: pipeline output == per-chunk sequential /v1/convert output */
  async service(a) {
    const text = fs.readFileSync(a.file, "utf8");
    const blob = new Blob([fs.readFileSync(a.file)]);
    const rd = L.createBlobReader(blob, { sliceBytes: a.sliceBytes || 65536 });
    const out = L.createOutput({ flushBytes: 1 << 16 }), acc = L.createAccumulator(1000);
    const state = { cancelled: false };
    const o = { read: () => rd.read(), isCancelled: () => false, maxBytes: 400000, maxRecords: 100,
      send: (req) => L.convertRecords({ post: postTo(a.url), kind: req.kind, opts: a.opts || {}, isCancelled: () => false }, req.records),
      onResult: (req, res) => { out.add(res.text); acc.add(req, res); } };
    const t0 = Date.now();
    const res = await L.runPipeline(o);
    const piped = await (await out.finish()).text();
    const ms = Date.now() - t0;
    let expected = "", sum = 0;
    for (const chunk of L.splitIntoChunks(text, 400000, 100)) {
      const r = await postTo(a.url)(Object.assign({ text: chunk, format: res.kind || L.detectKind(text.slice(0, 65536)) }, a.opts || {}));
      expected += r.json.text; sum += r.json.summary.records;
    }
    return { res, equal: piped === expected, bytes: piped.length, records: acc.total, expectedRecords: sum,
             retained: acc.retained.length, summary: acc.summary, ms };
  },

  async output_blob() {
    const out = L.createOutput({ flushBytes: 10 });
    const texts = [];
    for (let i = 0; i < 50; i++) { texts.push("é" + i + "\n"); out.add(texts[i]); }
    const blob = await out.finish();
    return { same: (await blob.text()) === texts.join(""), size: out.size() === Buffer.byteLength(texts.join("")), blobSize: blob.size };
  },
  async output_writable(a) {
    const file = path.join(os.tmpdir ? a.dir : a.dir, "streamed.txt");
    const fd = fs.createWriteStream(file);
    const writable = { write: (t) => new Promise((ok, bad) => fd.write(t, (e) => (e ? bad(e) : ok()))), close: () => new Promise((ok) => fd.end(ok)) };
    const out = L.createOutput({ writable });
    const texts = [];
    for (let i = 0; i < 20; i++) { texts.push("line é" + i + "\n"); out.add(texts[i]); }
    const blob = await out.finish();
    return { blob, same: fs.readFileSync(file, "utf8") === texts.join(""), size: out.size() };
  },
  accumulator() {
    const acc = L.createAccumulator(5);
    const mk = (n, errAt) => {
      const records = [], raws = [];
      for (let i = 0; i < n; i++) { raws.push("r" + i); records.push({ index: 0, error: errAt.includes(i) ? "no ion mode" : null, total: 2, impossible: 1 }); }
      return [{ records: raws }, { format: "msp", records, summary: { records: n, cleaned: n - errAt.length, passed_through: errAt.length, peaks: 2 * n, impossible: n } }];
    };
    let a = mk(4, []); acc.add(a[0], a[1]);
    a = mk(10, [1, 8]); acc.add(a[0], a[1]);
    a = mk(20, [0, 3, 5, 19]); acc.add(a[0], a[1]);
    return { total: acc.total, kept: acc.retained.map((x) => x.rec.index), raws: acc.retained.map((x) => x.raw).slice(0, 2),
             errors: acc.errors, summary: acc.summary, format: acc.format };
  },
  meter() {
    const m = L.createRateMeter(500);
    for (let t = 0; t <= 5000; t += 100) m.add(100000, t);   // 1 MB/s
    const steady = m.rate();
    for (let t = 5100; t <= 8000; t += 100) m.add(300000, t); // 3 MB/s
    const faster = m.rate();
    const an = L.createAnnouncer(8000), fired = [];
    for (let t = 0; t <= 60000; t += 500) if (an(t, t / 60000)) fired.push(t);
    return { steady, faster, eta: m.eta(10e6), none: L.createRateMeter().eta(5), fired,
             fmt: [L.formatBytes(0), L.formatBytes(1536), L.formatBytes(5 * 1024 * 1024), L.formatBytes(1073741824)],
             dur: [L.formatDuration(5), L.formatDuration(125), L.formatDuration(3725), L.formatDuration(null)] };
  },
};

Promise.resolve(checks[name](A)).then((r) => process.stdout.write(JSON.stringify(r)), (e) => { console.error(e); process.exit(1); });
