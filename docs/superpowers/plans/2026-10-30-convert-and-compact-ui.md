# Format-preserving cleaning, batch conversion and a compact zoomable page — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The denoise page and API can turn any input into its cleaned equivalent in the SAME format with metadata kept (MSP in -> MSP out, MGF in -> MGF out, a bunch of `mz:int` spectrum strings in -> the same number of cleaned strings out), for a single spectrum or a whole file, and the page is compact, zoomable and exports the cleaned spectrum.

**Architecture:** New pure module `denoise_service/convert.py` (detect format, split records, read per-record mode/precursor from metadata, classify with the existing machinery, rewrite keeping original text of kept peaks) behind a new `POST /v1/convert`. The page chunks large inputs client-side at record boundaries, posts chunks sequentially to `/v1/convert`, concatenates the cleaned text, and offers download/copy. The plot gets drag-to-zoom, wheel zoom, pan and reset. Layout becomes two-column and compact.

**Tech Stack:** Python 3.9-compatible FastAPI service (existing `denoise_service`), vanilla JS/SVG page (no build step, no dependencies), pytest; Node (if present) only for testing pure JS helpers.

**Spec:** the owner's request of 2026-10-30 (this plan restates it) plus the merged service design `docs/superpowers/specs/2026-10-30-denoise-service-auto-deploy-design.md` and the earlier plan `docs/superpowers/plans/2026-10-30-denoise-service-and-web-page.md` for conventions. Read `denoise_service/{app,parse,classify,config,limits}.py`, `static/{index.html,app.js,style.css}` and the existing tests first. Work on branch `feat/convert-and-ui` (already created from main in `~/IdeaProjects/impossible-mass-denoise`).

## Global Constraints

- Ziyue's modules (`impossible_mass_denoise.py`, `denoise.py`, the `.npz` tables) are untouched (an existing test enforces it). Cleaning = drop ions the filter calls impossible, nothing else: no rescaling, no renormalising, no reordering, no rounding.
- **Kept peaks are written back with their ORIGINAL text** (the exact m/z and intensity tokens, separators and line breaks of the kept peaks), so unchanged data is byte-identical. Only `Num Peaks:`/`NUM PEAKS` (MSP) is rewritten (same label case, same spacing style, new count). MGF has no count line. All other metadata lines pass through verbatim, in order.
- Output format = input format: `msp` -> MSP records separated exactly as in the input; `mgf` -> `BEGIN IONS`..`END IONS` blocks; `strings` (every non-empty line holds `mz:int` pairs) -> one cleaned line per input line, same line count and order (an all-removed spectrum becomes an empty line, never a dropped line); `columns` (plain two-column or loose-number text, one spectrum) -> the kept lines.
- Per-record mode and precursor come from metadata when present, else from the request defaults: MSP keys (case-insensitive, `_`/space/`-` ignored): `IonMode`/`Ion_mode`/`Ionization` values `positive|pos|p` / `negative|neg|n`; `PrecursorMZ`/`Precursor_MZ`. MGF: `IONMODE=`, `PEPMASS=` (first number), `CHARGE=` sign (`1+`/`1-`) as fallback for mode. Request defaults: `mode` (pos|neg, optional), `precursor_mz` (optional), `halogens`, `deuterium`, `carbon13`, `nitrogen15` as in `/v1/classify`. A record with no determinable mode is NOT cleaned: it is passed through unchanged and reported with `error: "no ion mode"`.
- Failures never lose data: an unparseable or over-limit record is passed through verbatim with a per-record `error` (reason only, never the user's text); the request still returns 200. Request-level limits (body 1 MiB, more than `max_batch` records, summed work over `max_request_lookups`) return the existing 413/422/429/503 error shapes. The page then halves the chunk and retries, honouring `Retry-After` on 429/503.
- Spectrum contents are never logged. Same CORS, CSP, rate limiting and in-flight rules as `/v1/classify` (reuse the same dependency/limiter path, count the work the same way). Python 3.9-compatible syntax; PyYAML/orjson as already used.
- Response shape `POST /v1/convert`: request `{text: str, mode?: "pos"|"neg", precursor_mz?: number, halogens?: bool, deuterium?: int, carbon13?: int, nitrogen15?: int}`; response `{fingerprint, format: "msp"|"mgf"|"strings"|"columns", text: str, records: [{index: int, name: str|null, total: int, impossible: int, error: str|null}], summary: {records: int, cleaned: int, passed_through: int, peaks: int, impossible: int}}`.
- Page: UC Davis colours kept (blue `#022851`, gold `#FFBF00`; plot possible blue, impossible red), accessible (labels, keyboard-operable zoom buttons, `aria-live` status), no external requests, no inline script/style (CSP), works at phone width without horizontal page scroll, no new dependencies.
- Each task ends with `python -m pytest denoise_service` green (also on Python 3.9 if available via `uv venv --python 3.9`); commit messages end with a blank line then `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Do not merge or deploy: push and open a PR; merging to `main` auto-deploys to production.

## Review Focus

- MSP file with Windows (`\r\n`) line endings, a trailing record without a final newline, blank-line runs between records, peak lines using tab, space, `;` or `mz:int` pairs several per line: output keeps the file's own line endings and structure.
- A record whose peaks are all impossible: `Num Peaks: 0` with the headers kept (MSP), a record kept with no peak lines (MGF), an empty line (strings); never a crash or a dropped record.
- Metadata that looks like peaks or peaks that look like metadata (`Name: 123:45`, `Comment:` lines containing colons, `Num Peaks` before or after the peaks).
- Mixed polarity inside one file; one bad record among 199 good ones; a record above `max_peaks`.
- Round trip: cleaning an already clean file is the identity; cleaning with every ion possible returns the input byte for byte.
- Huge pasted text near the 1 MiB body limit; client chunking never splits inside a record; the concatenated output equals what a single request would produce.

---

### Task 1: `convert.py` — detect, split, read metadata, rewrite

**Files:**
- Create: `denoise_service/convert.py`, `denoise_service/tests/test_convert.py`, `denoise_service/tests/fixtures/` (small MSP, MGF and strings samples; peaks chosen from `ops/deploy/smoke.json`: 181.0707 and 85.0284 possible, 100.5 and 123.9 impossible in positive mode)

**Interfaces:**
- Produces: `detect_format(text: str) -> str` (`"msp"|"mgf"|"strings"|"columns"`); `@dataclass Record: index:int; format:str; header_lines:list[str]; peak_lines:list[str]; footer_lines:list[str]; raw:str; name:Optional[str]; mode:Optional[str]; precursor_mz:Optional[float]; peaks:list[tuple[str,str]]` (original token text, in order, each remembering which line/column it came from); `split_records(text: str, fmt: str) -> list[Record]`; `render_record(rec: Record, keep: list[bool]) -> str`; `join_records(records_text: list[str], original_text: str, fmt: str) -> str` (re-uses the input's separator and line ending); `convert(text: str, classify: Callable[[list[tuple[float,float]], str, Optional[float]], list[bool]], default_mode, default_precursor) -> ConvertResult` where `classify(peaks, mode, precursor_mz)` returns per-peak possible flags in order and `ConvertResult` carries `format, text, records:[{index,name,total,impossible,error}], summary`.
- Consumes: `parse.parse_spectrum` semantics (numbers validated the same way; reuse its regex/limits rather than a second number grammar), `Config.max_batch`/`max_peaks` for caps (raise a `ConvertLimitError(reason)` the app maps to 413/422).

- [ ] **Step 1: Write failing tests** in `test_convert.py` (names exactly): `test_detect_format_msp_mgf_strings_columns`; `test_msp_roundtrip_identity_when_all_possible` (byte-identical, including CRLF input); `test_msp_removes_impossible_and_rewrites_num_peaks_keeping_metadata`; `test_msp_peaks_several_per_line_rebuilt_from_original_tokens`; `test_msp_all_impossible_gives_zero_peaks_and_keeps_headers`; `test_msp_mode_and_precursor_read_from_metadata_over_defaults`; `test_msp_no_mode_anywhere_is_passed_through_with_error`; `test_mgf_cleaning_keeps_blocks_and_pepmass_charge`; `test_strings_one_line_per_spectrum_same_count_and_order`; `test_strings_all_removed_is_empty_line_not_dropped`; `test_columns_single_spectrum_keeps_kept_lines`; `test_bad_record_among_good_is_passed_through_verbatim_with_reason_only`; `test_record_over_max_peaks_is_passed_through_with_error`; `test_more_than_max_batch_records_raises_limit_error`; `test_cleaning_twice_is_idempotent`; `test_metadata_with_colons_is_not_mistaken_for_peaks`; `test_trailing_record_without_newline_and_blank_line_runs_preserved`.
- [ ] **Step 2: Run** `python -m pytest denoise_service/tests/test_convert.py -q`; expect FAIL (module missing).
- [ ] **Step 3: Implement** `convert.py` per the Interfaces and Global Constraints. Number grammar and header test come from `parse.py` (import, do not copy). No logging of content.
- [ ] **Step 4: Run** the new tests and the full suite; expect PASS.
- [ ] **Step 5: Commit** `feat(service): format-preserving spectrum conversion core`.

### Task 2: `POST /v1/convert` endpoint

**Files:** Modify `denoise_service/app.py` (and `classify.py` only if a small helper for per-peak flags is needed — reuse `denoise_spectrum` on index-tagged triples exactly like `/v1/classify` so verdicts are identical); Test: `denoise_service/tests/test_api.py` (extend) or new `test_api_convert.py`.

**Interfaces:** Consumes `convert.convert`. Produces the route and Pydantic request model per Global Constraints; shares the limiter, in-flight cap, pending queue, body-size and `max_request_lookups` accounting with `/v1/classify/batch` (work = sum over records of peaks x label lookups); verdict parity: for the same peaks, `/v1/convert` removes exactly the ions `/v1/classify` marks impossible.

- [ ] **Step 1: Failing tests**: `test_convert_msp_end_to_end`; `test_convert_strings_end_to_end`; `test_convert_verdicts_match_classify_for_same_peaks`; `test_convert_options_pass_through_halogens_and_precursor`; `test_convert_rejects_oversized_body_413`; `test_convert_rejects_too_many_records_422`; `test_convert_rate_limit_and_busy_use_existing_error_shapes` (429 with Retry-After, 503); `test_convert_never_logs_spectrum_text` (caplog contains no peak text); `test_convert_openapi_documents_route`.
- [ ] **Step 2: Run**; expect FAIL.
- [ ] **Step 3: Implement** the route next to `/v1/classify/batch`.
- [ ] **Step 4: Run** full suite; expect PASS; run the latency test still under its bound.
- [ ] **Step 5: Commit** `feat(service): POST /v1/convert`.

### Task 3: Page logic — chunking, conversion client, export (pure JS helpers + wiring)

**Files:** Create `denoise_service/static/lib.js` (pure functions, UMD-style so Node can `require` it: `splitIntoChunks(text, maxBytes, maxRecords)`, `detectKind(text)`, `zoomRange(range, factor, center)`, `panRange(range, dx)`, `niceTicks(lo, hi)`, `cleanedFilename(name, format)`), `denoise_service/tests/test_static_js.py` (runs a small Node script over `lib.js` when `node` exists, `pytest.skip` otherwise; pins: chunks never split inside an MSP/MGF record or a strings line, concatenating chunks reproduces the input, filename `sample.msp` -> `sample.cleaned.msp`); Modify `static/app.js`, `static/index.html` (script tag order only here), `denoise_service/app.py` only if the CSP needs `blob:` for downloads (check the current CSP header; use `a[download]` with a Blob URL and revoke it).

**Interfaces:** `lib.js` is also loaded by `index.html` before `app.js`. App behaviour: input via textarea or a file chooser/drop zone (`.msp .mgf .txt`, read with `FileReader`, never uploaded except through `/v1/convert`); one **Clean** action converts everything (single spectrum or batch) by posting chunks (<= 100 records and <= 400000 bytes) sequentially, with a progress line and Cancel, halving the chunk on 413/422 and waiting `Retry-After` on 429/503 (max 5 retries per chunk then a visible error that keeps the partial output); results: cleaned text, per-record table (`index, name, total, impossible, status`), buttons **Download cleaned** (original format and, for a file, `<name>.cleaned.<ext>`), **Copy cleaned**, **Copy as JSON**; choosing a record in a batch plots it (it plots the record's ORIGINAL ions coloured by verdict; fetch verdicts for the selected record with `/v1/classify` using that record's mode/precursor).

- [ ] **Step 1: Failing tests** for `lib.js` in `test_static_js.py` (names: `test_chunks_never_split_a_record`, `test_chunks_concatenate_to_input`, `test_filename_for_cleaned_download`, `test_zoom_range_keeps_center_and_clamps`, `test_detect_kind_matches_server_formats`); a pytest that `index.html` loads `lib.js` before `app.js` and has no inline script/style.
- [ ] **Step 2: Run**; expect FAIL.
- [ ] **Step 3: Implement** `lib.js` and the `app.js` wiring.
- [ ] **Step 4: Run** the full suite; manually verify in a real browser if one is available (Playwright/Chromium via `uv`/`npx` is fine) with a 1000-record MSP, and record the observations in the PR body; otherwise state what was not browser-tested.
- [ ] **Step 5: Commit** `feat(page): batch cleaning client with chunking and export`.

### Task 4: Compact layout and zoomable plot

**Files:** Modify `static/index.html`, `static/style.css`, `static/app.js`; Test: extend `test_static.py` (headers/CSP unchanged, ids present).

**Interfaces:** Two-column layout from 52rem up (left: input, file drop, options on one compact row, Clean button, status; right: plot then results), single column below; header slimmer; the table and per-record list collapse under `<details>`; the long caveat becomes a one-line note with a "details" disclosure. Plot: SVG, x zoom by mouse wheel (around the cursor), drag-select a range to zoom, drag with Shift or two-finger/pinch to pan, double-click or **Reset** to restore, **+ / -** buttons and arrow keys when the plot is focused, y auto-rescales to the visible ions, tick labels via `niceTicks`, tooltip with m/z, intensity and verdict on hover/focus; possible ions blue, impossible red, legend "Possible" / "Not explained by CHNOPS".

- [ ] **Step 1: Failing tests** (HTML structure/CSS only, as far as pytest can see): `test_page_has_zoom_controls_and_download_buttons`; `test_page_has_no_inline_script_or_style`; `test_csp_header_still_blocks_inline` (existing assertions still hold).
- [ ] **Step 2: Run**; expect FAIL.
- [ ] **Step 3: Implement.** Screenshot desktop (1280 wide) and phone (390 wide) with a headless browser and attach to the PR.
- [ ] **Step 4: Run** the full suite; expect PASS.
- [ ] **Step 5: Commit** `feat(page): compact two-column layout and zoomable plot`; push `feat/convert-and-ui` and open a PR to `main` (body: what changed, evidence, "merging auto-deploys", ends with the Claude Code attribution line). Update `ops/deploy/README.md`/README API section with `/v1/convert` and the format rules.
