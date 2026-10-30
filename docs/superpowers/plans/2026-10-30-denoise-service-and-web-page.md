# Denoise Service and Web Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A public, low-latency REST service and a small UC Davis-styled web page, in the `impossible-mass-denoise` repo, that label every ion of a spectrum as possible or impossible.

**Architecture:** A new package `denoise_service/` beside Ziyue's untouched `denoise.py` imports `denoise_spectrum` and reuses its exact semantics by running it on index-tagged peaks. A FastAPI app holds the lookup tables in memory, serves JSON via `orjson`, and serves the static page from the same process. Config comes from one YAML file.

**Tech Stack:** Python 3.9+ (the repo floor), FastAPI, uvicorn (one worker), orjson, PyYAML, numpy; pytest and httpx for tests.

**Spec:** `docs/superpowers/specs/2026-10-30-binview-impossible-ion-overlay-design.md` (in `lc-binbase-scheduler`, branch `feat/binview-denoise-overlay`). This plan covers spec parts 1 and 2 only. The Go backend setting (part 4) and BinView (part 3) get separate plans, written against the API contract fixed here.

**Where to work:** a clone of `metabolomics-us/impossible-mass-denoise` on a new branch `feat/denoise-service`, from `main`. Never edit `denoise.py`, `impossible_mass_denoise.py` or the `.npz` tables.

## Global Constraints

- Ziyue's modules and tables stay byte-identical (CI check in Task 6). The chemistry fingerprint must report `5aae4ec26694da4d`.
- Public URL `https://denoise.metabolomics.us`, host `whiteale.fiehnlab.ucdavis.edu`; open to anyone, no login.
- Input text format is standard MS/MS text, `mz:intensity` pairs. `mode` is `pos` or `neg` and is required; anything else is rejected, never defaulted.
- UC Davis colours: blue `#022851`, gold `#FFBF00`. Plot: possible ions blue `#022851`, impossible ions red `#c62828` (style A).
- The service never logs spectrum contents (peaks or text), only counts, status and timings.
- All configuration lives in one YAML file passed with `--config <path>`; no environment variables (my call, following the operator's standing rule for the lab's services). Unknown YAML keys fail startup.
- Wording: "impossible" always means "not explained by CHNOPS, singly charged, monoisotopic", never "not a real ion".
- Proposed limits, all configurable (my call): 5000 peaks per spectrum, 200 spectra per batch, 1 MiB request body, each of `deuterium`/`carbon13`/`nitrogen15` at most 12 and their lookup product `(d+1)(c+1)(n+1)` at most 200, rate limit 120 requests per minute per client with burst 30.

## Review Focus

1. Tables not loaded: without them every lookup falls back to a solver taking up to a minute per mass, so one request could hang the service. Startup must refuse to run, and `/healthz` must report it.
2. Label options multiply lookups per peak; unbounded values are a cheap denial of service. Reject over-limit values with 422.
3. Bad numbers: NaN, infinity, zero, negative m/z; negative intensity; empty spectrum; 5001 peaks.
4. Duplicate m/z peaks and unsorted input: verdicts must stay aligned with input order, one per input ion.
5. Messy pasted text: Windows line endings, tabs, commas, blank lines, MSP header lines (`Name:`, `Num Peaks:`), and a bad token halfway through (must give 422 naming the line, not a 500 or a silent skip).
6. Cache growth in a long-lived process (~26 MB per 100,000 distinct masses): must be cleared on a timer.

---

## File Structure

- Create `denoise_service/__init__.py` — package marker, exposes `__version__`.
- Create `denoise_service/parse.py` — text to peak list.
- Create `denoise_service/classify.py` — verdicts and summary, the only module importing `denoise`.
- Create `denoise_service/config.py` — YAML config schema, limits, strict loading.
- Create `denoise_service/limits.py` — per-client token-bucket rate limiter.
- Create `denoise_service/app.py` — FastAPI app factory `create_app(config)`, endpoints, startup checks.
- Create `denoise_service/__main__.py` — `python -m denoise_service --config path` runs uvicorn.
- Create `denoise_service/static/{index.html,app.js,style.css}` — the page.
- Create `denoise_service/tests/test_*.py`, `denoise_service/config.example.yaml`.
- Create `requirements-service.txt`, `ops/ci/test_service.sh`, `ops/deploy/denoise-service.service`, `ops/deploy/README.md`.
- Modify `.woodpecker.yaml` (second gate step), `README.md` (short "Service" section).

## API contract (consumed by the BinView plan)

`POST /v1/classify`, JSON body, exactly one of `peaks` or `spectrum`:

```json
{"mode": "neg", "peaks": [[86.06, 45], [104.11, 100]],
 "halogens": false, "deuterium": 0, "carbon13": 0, "nitrogen15": 0, "precursor_mz": null}
```

or `"spectrum": "86.06:45 104.11:100"` in place of `peaks`. Response 200:

```json
{"fingerprint": "5aae4ec26694da4d", "mode": "neg",
 "ions": [{"mz": 86.06, "intensity": 45.0, "possible": true}],
 "summary": {"total": 2, "impossible": 0, "intensity_total": 145.0,
             "intensity_impossible": 0.0, "intensity_impossible_fraction": 0.0}}
```

`ions` has one entry per input ion, in input order. `POST /v1/classify/batch` takes `{"spectra": [<classify body plus optional "id">, ...]}` and returns `{"results": [<classify response plus "id">, ...]}` in the same order. `GET /healthz` returns `{"status": "ok", "fingerprint": ..., "tables": {"chnops": ..., "halogen": ...}, "cache_size": n}`. Errors are `{"detail": "..."}` with 422 (bad input), 413 (too large), 429 (rate limited, with `Retry-After`).

---

### Task 1: Spectrum text parser

**Files:**
- Create: `denoise_service/__init__.py`, `denoise_service/parse.py`
- Test: `denoise_service/tests/test_parse.py`

**Interfaces:**
- Produces: `parse_spectrum(text: str) -> list[tuple[float, float]]`; `class SpectrumParseError(ValueError)` whose message names the 1-based line number and the offending token.

Rules (this is the decision the implementer cannot guess): split on any line ending; skip blank lines; skip MSP header lines (a line whose first character is a letter and which contains a colon, such as `Name: X`, `Num Peaks: 5`); in the remaining lines split tokens on whitespace, `;` and `,`; a token `a:b` is one peak; otherwise a line's numeric tokens must be an even count and pair up as `mz intensity` in order. Numbers may use scientific notation. Intensities and m/z must be finite; m/z must be greater than zero; intensity not negative. No peaks at all is an error.

- [ ] **Step 1: Write the failing tests** — `test_parse_colon_pairs` (`"86.06:45 104.11:100"` gives two tuples), `test_parse_two_column_lines`, `test_parse_msp_record_skips_headers` (a record with `Name:`, `Num Peaks:` and tab-separated peaks), `test_parse_crlf_commas_semicolons_blank_lines`, `test_parse_scientific_notation`, `test_parse_error_names_line_and_token` (`"86.06:45\n104.11:abc"` raises with "line 2" and "abc"), `test_parse_rejects_odd_number_count_on_line`, `test_parse_rejects_nonfinite_zero_negative_mz_and_negative_intensity` (parametrised: `nan`, `inf`, `0:5`, `-3:5`, `5:-1`), `test_parse_rejects_empty`.
- [ ] **Step 2: Run to verify failure** — `python -m pytest denoise_service/tests/test_parse.py -v`; expected: collection error, module missing.
- [ ] **Step 3: Implement** `parse_spectrum` and `SpectrumParseError` in `denoise_service/parse.py` per the rules above (a compiled regex for the number and a line loop; no third-party code).
- [ ] **Step 4: Run to verify pass** — same command; expected: all pass.
- [ ] **Step 5: Commit** — `git add denoise_service && git commit -m "feat(service): spectrum text parser"`.

### Task 2: Classifier

**Files:**
- Create: `denoise_service/classify.py`
- Test: `denoise_service/tests/test_classify.py`

**Interfaces:**
- Consumes: `parse_spectrum` is not needed here; input is `list[tuple[float, float]]`.
- Produces: `@dataclass(frozen=True) class Options` with fields `mode: str`, `halogens: bool = False`, `deuterium: int = 0`, `carbon13: int = 0`, `nitrogen15: int = 0`, `precursor_mz: float | None = None`; `classify(peaks: Sequence[tuple[float, float]], options: Options) -> Classification`; `@dataclass(frozen=True) class Classification` with `possible: list[bool]` (aligned with input) and `summary: dict` with the six keys of the API summary; `fingerprint() -> str`.

Approach (decided): call `denoise.denoise_spectrum` on `(mz, intensity, index)` triples and mark an ion possible exactly when its index survives, so precursor protection, label options and halogen isotope chaining match Ziyue's filter by construction. Do not call `keep_top`. `intensity_impossible_fraction` is `0.0` when total intensity is `0`.

- [ ] **Step 1: Write the failing tests** — `test_known_ion_verdicts` (use masses whose verdict you first confirm with `denoise.is_possible`: one clearly possible, e.g. the exact mass of `H2PO4-` 96.96910 in `neg`, and one impossible found by scanning 100–150 in 0.001 steps for the first `is_possible(mz, "neg") == False`; hard-code both numbers in the test), `test_verdicts_follow_input_order_with_duplicates_and_unsorted` (the same mz twice, descending order), `test_matches_denoise_spectrum_survivors` (random 200-peak spectrum, seeded; set of possible indices equals the survivor set from `denoise_spectrum` for each of: default, `halogens=True`, `precursor_mz=<an impossible peak's mz>`, `deuterium=9`, `carbon13=6, nitrogen15=2`), `test_summary_numbers` (counts and intensity fraction), `test_zero_total_intensity_fraction_is_zero`, `test_empty_peak_list_gives_empty_result`, `test_invalid_mode_raises_value_error`, `test_mz_above_table_ceiling_is_possible` (1800.123).
- [ ] **Step 2: Run to verify failure** — `python -m pytest denoise_service/tests/test_classify.py -v`; expected: import error.
- [ ] **Step 3: Implement** `Options`, `Classification`, `classify` and `fingerprint` in `denoise_service/classify.py`.
- [ ] **Step 4: Run to verify pass** — expected: all pass. Also run `python test_smoke.py`; expected: exit 0 (proves Ziyue's code is unaffected).
- [ ] **Step 5: Commit** — `feat(service): classifier reusing denoise_spectrum semantics`.

### Task 3: Config and rate limiter

**Files:**
- Create: `denoise_service/config.py`, `denoise_service/limits.py`, `denoise_service/config.example.yaml`
- Test: `denoise_service/tests/test_config.py`, `denoise_service/tests/test_limits.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) class Config` with `host: str = "127.0.0.1"`, `port: int = 8300`, `max_peaks: int = 5000`, `max_batch: int = 200`, `max_body_bytes: int = 1048576`, `max_label: int = 12`, `max_label_lookups: int = 200`, `rate_per_minute: int = 120`, `rate_burst: int = 30`, `cache_clear_seconds: int = 3600`, `trusted_proxy: bool = True`, `cors_origins: list[str] = ["*"]`; `load_config(path: str) -> Config` (strict: unknown key, wrong type or non-positive limit raises `ConfigError`); `class TokenBucket` with `allow(key: str, now: float) -> tuple[bool, float]` returning allowed and seconds until the next token.

- [ ] **Step 1: Write the failing tests** — `test_load_config_defaults_and_overrides`, `test_load_config_rejects_unknown_key`, `test_load_config_rejects_wrong_type_and_nonpositive`, `test_bucket_allows_burst_then_refuses`, `test_bucket_refills_over_time` (inject `now`), `test_bucket_keys_are_independent`, `test_bucket_evicts_idle_keys` (memory stays bounded when many keys appear).
- [ ] **Step 2: Run to verify failure** — `python -m pytest denoise_service/tests/test_config.py denoise_service/tests/test_limits.py -v`; expected: import errors.
- [ ] **Step 3: Implement** both modules; `load_config` uses `yaml.safe_load`; `TokenBucket` is a plain dict of `(tokens, last)` with periodic eviction, no dependency.
- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Commit** — `feat(service): strict YAML config and token-bucket limiter`.

### Task 4: API app

**Files:**
- Create: `denoise_service/app.py`, `denoise_service/__main__.py`
- Test: `denoise_service/tests/test_api.py`

**Interfaces:**
- Consumes: `parse_spectrum`, `SpectrumParseError`, `classify`, `Options`, `fingerprint`, `Config`, `TokenBucket`.
- Produces: `create_app(config: Config) -> FastAPI`. Startup (lifespan) raises `RuntimeError` unless `denoise.table_status()` and `denoise.halogen_table_status()` both start with `"loaded"` and `fingerprint()` equals `5aae4ec26694da4d`. Responses are built with `orjson.dumps` and `Response(media_type="application/json")`. A background task calls `denoise.clear_cache()` every `cache_clear_seconds`.

Behaviour to implement exactly: validation in a pydantic model (exactly one of `peaks`/`spectrum`; `mode` in `{"pos","neg"}`; label counts `0..max_label` and lookup product within `max_label_lookups`; peak count within `max_peaks`; finite numbers as in Task 1); body larger than `max_body_bytes` gives 413 before parsing; rate limit keyed by the first `X-Forwarded-For` entry when `trusted_proxy` else the socket peer, 429 with `Retry-After`; CORS from `cors_origins`; the batch endpoint fails the whole request with 422 naming the spectrum index if any one spectrum is invalid; request logging records method, path, status, ion count and milliseconds only.

- [ ] **Step 1: Write the failing tests** (FastAPI `TestClient`, config with small limits) — `test_classify_json_peaks_shape_and_order`, `test_classify_text_spectrum_matches_peaks_form`, `test_classify_requires_exactly_one_of_peaks_and_spectrum`, `test_mode_required_and_validated` (missing, `"negative"`, `"NEG"` all 422), `test_bad_text_returns_422_naming_line`, `test_over_limit_peaks_422`, `test_oversized_body_413`, `test_label_limits_422` (`deuterium=13`; `deuterium=12, carbon13=12` over the product), `test_nonfinite_and_nonpositive_mz_422`, `test_empty_spectrum_422`, `test_batch_order_ids_and_error_index`, `test_batch_over_limit_422`, `test_healthz_reports_tables_and_fingerprint`, `test_startup_refuses_when_tables_missing` (monkeypatch `denoise.table_status` to return `"unusable: missing"`; creating the app and entering lifespan raises), `test_rate_limit_429_with_retry_after`, `test_cors_header_for_any_origin`, `test_logs_do_not_contain_spectrum_values` (caplog contains none of the posted m/z strings).
- [ ] **Step 2: Run to verify failure** — `python -m pytest denoise_service/tests/test_api.py -v`; expected: import error.
- [ ] **Step 3: Implement** `create_app` and `__main__.py` (`--config` required, runs `uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, workers=1)`; one worker so the cache and limiter are shared).
- [ ] **Step 4: Run to verify pass**, then start `python -m denoise_service --config denoise_service/config.example.yaml` and run `curl -s -X POST localhost:8300/v1/classify -H 'content-type: application/json' -d '{"mode":"neg","spectrum":"96.9691:100 150.123:20"}'`; expected: JSON with two ions and a summary.
- [ ] **Step 5: Commit** — `feat(service): classify, batch and health endpoints`.

### Task 5: Web page

**Files:**
- Create: `denoise_service/static/index.html`, `app.js`, `style.css`
- Modify: `denoise_service/app.py` (mount `/` static, `/docs` stays the OpenAPI UI)
- Test: `denoise_service/tests/test_static.py`

**Interfaces:**
- Consumes: `POST /v1/classify` contract above, called with `fetch` and the `spectrum` field.
- Produces: page at `/`. No CDN or external assets (public page must work offline from the service); plot is inline SVG built in `app.js`.

Page content (from the approved mockup): UC Davis blue header with the title "Impossible-mass denoise" and gold subtitle; paste box (placeholder `86.06:45 104.11:100 ...`); polarity radio `neg`/`pos` with no default selected until chosen (a missing choice shows an inline message instead of guessing); halogens checkbox; precursor m/z field; gold Classify button; result area with the summary line ("6 of 15 ions impossible, 14% of intensity"), a stick plot (possible blue, impossible red, legend with counts), a verdict table with m/z, intensity and verdict, "Copy as JSON", and a link to `/docs`. A fixed caveat under the result: "Impossible means not explained by CHNOPS, singly charged, monoisotopic. Works for Orbitrap data; off by default for TTOF; not for QTOF. Ions above 1700 m/z are always reported possible." Server error messages from 422 are shown verbatim. Escape all user text when inserting into the DOM.

- [ ] **Step 1: Write the failing test** — `test_root_serves_page_with_brand_colours_and_no_external_assets` (GET `/` is 200 HTML containing `#022851`, `#FFBF00`, "Impossible-mass denoise", and no `http://` or `https://` URLs other than relative links), `test_static_assets_served` (`/static/app.js` 200, content type JavaScript).
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** the three static files and the mount in `create_app`.
- [ ] **Step 4: Run to verify pass**, then a manual check: start the service, open it in a browser via the existing HerdR open flow, paste the sample from Task 4, and confirm blue and red sticks, the summary line, and that choosing no polarity shows the inline message. Note the result in the commit message.
- [ ] **Step 5: Commit** — `feat(service): UC Davis styled paste-and-plot page`.

### Task 6: CI, latency check and deployment files

**Files:**
- Create: `requirements-service.txt`, `ops/ci/test_service.sh`, `ops/deploy/denoise-service.service`, `ops/deploy/README.md`
- Modify: `.woodpecker.yaml` (add a second gate step after `gate`), `README.md` (a short "Service" section pointing to `ops/deploy/README.md`)
- Test: `denoise_service/tests/test_latency.py`, `denoise_service/tests/test_untouched.py`

**Interfaces:** none consumed or produced for later code tasks.

- [ ] **Step 1: Write the failing tests** — `test_latency_100_peaks_median_under_5ms` (in-process `TestClient`, 200 requests of 100 distinct random masses, median wall time under 5 ms; marked so a slow CI host can raise the bound in the test file only), `test_ziyue_files_untouched` (SHA-256 of `denoise.py`, `impossible_mass_denoise.py` and both `.npz` files equals the values recorded in the test, taken from `main` at plan time).
- [ ] **Step 2: Run to verify** — the untouched test passes against `main`; the latency test fails only if the service is slow (record the measured median in the commit message).
- [ ] **Step 3: Add** `requirements-service.txt` (`fastapi`, `uvicorn`, `orjson`, `PyYAML`, `pytest`, `httpx` with lower bounds), `ops/ci/test_service.sh` (same venv pattern as `ops/ci/test_smoke.sh`, then `pytest denoise_service`), the Woodpecker step `service-gate` running it, and `ops/deploy/denoise-service.service` (systemd unit running `python -m denoise_service --config /etc/denoise-service/config.yaml` as an unprivileged user, `Restart=always`, loopback bind).
- [ ] **Step 4: Write `ops/deploy/README.md`** covering the three things that live outside this repo and need the operator: the nginx vhost for `denoise.metabolomics.us` proxying to `127.0.0.1:8300` with `X-Forwarded-For` set (goes in the `nginx` role of `metabolomics-us/fiehnlab-slurm-cluster`, which owns whiteale's conf.d with purge enabled), the DNS record and certificate for the name, and installing the unit and config on whiteale. Include the exact `curl` smoke test from Task 4 against the public name, and the rollback (stop the unit; nothing else depends on it).
- [ ] **Step 5: Run everything** — `bash ops/ci/test_service.sh` and `python test_smoke.py`; expected: all pass, exit 0.
- [ ] **Step 6: Commit** — `ci(service): gate, latency check and deploy files`.

## Out of scope for this plan

Deploying to whiteale, the nginx vhost, DNS and the certificate (the operator does these, using `ops/deploy/README.md`); the Go backend setting; BinView. The BinView plan consumes only the API contract above.
