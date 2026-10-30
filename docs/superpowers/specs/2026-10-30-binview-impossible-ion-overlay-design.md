# BinView impossible-ion overlay and denoise service — design

Date: 2026-10-30. Branch: `feat/binview-denoise-overlay` (from `origin/master_ai`).
Spans two repositories: `metabolomics-us/impossible-mass-denoise` (service and web page) and
`lc-binbase-scheduler` (BinView and the Go backend).

## Intent

When someone looks at a bin's spectrum in BinView, they want to see at a glance which ions are
*possible* and which are *impossible* under Ziyue's impossible-mass denoise algorithm. That shows how
clean or noisy the bin is, whether it carries improper noise, and what the filter would remove later.
The view is read-only: nothing is removed or rescaled. It runs alongside the production version of
BinView and must not change it.

## Parts

### 1. Denoise service (repo `impossible-mass-denoise`, separate package)

Ziyue's code (`impossible_mass_denoise.py`, `denoise.py`, the two `.npz` tables) stays untouched and
is imported by the service. The service and the web page live in the same repository but in their
own package.

- Fast Python REST server (FastAPI with orjson is the proposal). One process loads the tables once.
  A cold lookup is about 6 µs per peak, so network time dominates latency.
- Public URL `https://denoise.metabolomics.us`, hosted on `whiteale.fiehnlab.ucdavis.edu`.
  **Open to anyone**, no login.
- `POST /v1/classify`: input a peak list (`[[mz, intensity], ...]`) or the text format
  `mz:int mz:int` (standard MS/MS text, space or newline separated), plus `mode` (`pos` or `neg`,
  required) and optional `halogens`, `deuterium`, `carbon13`, `nitrogen15`, `precursor_mz`.
  Output: one verdict (possible or impossible) per input ion, in input order, plus a summary
  (ion count, impossible count, share of total intensity that would be removed). Verdicts come from
  the per-peak `is_possible` logic, so every ion is labelled rather than only the survivors.
  *Proposed, not decided:* exact JSON field names, and whether the summary uses the same semantics as
  `denoise_spectrum` for `precursor_mz` and halogen isotope chaining.
- A batch endpoint for several spectra per call, and `GET /healthz` reporting table status and the
  chemistry fingerprint (validated value `5aae4ec26694da4d`).
- Because the service is public: CORS open to all origins, a cap on peaks per request and spectra
  per batch, and a per-client rate limit. The module's m/z cache is unbounded, so the service clears
  it periodically.
- Every response carries the fingerprint so results stay comparable.

### 2. Web page (same package, served by the service)

A small page at the service root in UC Davis colours (blue `#022851`, gold `#FFBF00`).
A paste box for `mz:int` text, a polarity switch, optional halogens and precursor m/z fields, and a
Classify button. Results appear as a spectrum plot with possible ions blue and impossible ions red
(style A below), and a table of m/z, intensity, verdict. Link to API docs.

### 3. BinView (repo `lc-binbase-scheduler`, `master_ai`)

- For any bin, BinView automatically calls the service with the bin's spectrum text
  (`headSpectrum` / `tailSpectrum`) and the bin's `ionization` (positive or negative), and colours
  the existing `<app-spectra-chart>` ECharts plot: **possible ions blue, impossible ions red**
  (plain two-colour style; no extra ✕ markers). A small badge shows "N impossible".
- It only calls and colours for bins whose method is enabled (part 4).
- If the service is down, slow or rejects the call, the chart still renders as today, but the error
  is shown to the user (a small visible note next to the chart). The repo forbids `catchError`
  that silently swallows errors, so the failure must surface, not disappear.
- Runs concurrently with the production BinView; no behaviour change for production.

### 4. Administration setting: which methods the overlay applies to

The filter is only valid for some instruments (the README: Orbitrap yes, TTOF off by default,
QTOF no), so it must not apply everywhere.

- A new page under Administration (the `/admin` routes, guarded by `adminGuard`; existing
  `/system/...` settings pages are the visual pattern) lists the
  methods with an on/off toggle each.
- **Admins only can change it.** The list is stored in the backend and shared by everyone; other
  users read it so BinView knows which bins to call for. It is not a per-browser preference.
- Default is off for every method until an admin enables it *(my call)*.
- The page can show the README's instrument guidance next to the toggles *(my call)*.

## Goals

- Instantly see possible versus impossible ions in a bin's spectrum.
- Low latency; BinView feels no slower.
- A public tool anyone can paste a spectrum into, and a documented REST API.

## Non-goals and anti-goals

- No removal of ions, no rescaling, no change to Ziyue's algorithm or tables.
- No change to production BinView behaviour.
- Anti-goal: a red ion presented as "not a real ion". "Impossible" means impossible under CHNOPS,
  singly charged, monoisotopic; halogenated, alkali, deuterated or multiply charged ions can be real.
  The UI wording must not overclaim.

## Constraints

- Works on a worktree of `origin/master_ai`; the user's `master` checkout stays untouched.
- Ziyue's module is a drop-in directory, not pip-installable; the service package must import it
  without modifying it.
- Admin-only writes; reads for all signed-in BinView users.

## Calls made after the builder check (mine, for review)

- **Bin to method:** the bin/compound already carries a `method` string (BinView reads it for
  ionization today). Toggles are keyed by that method name; if a stable method id is available
  alongside it, prefer the id and show the name. The list shows all known methods.
- **Which spectrum:** the chart's ions come from the same spectrum text BinView passes in, so
  verdicts are matched back by m/z (not index), and the badge counts only the ions actually plotted.
  If the chart shows head and tail spectra, each gets its own request and its own colouring.
- **Parameters BinView sends:** `mode` from the bin's ionization, `precursor_mz` when the bin has
  one, halogens off. The badge and tooltip read "not explained by CHNOPS", never "not a real ion".
- **Privacy:** the service is open to anyone by the operator's decision; BinView calls it straight
  from the browser. The service must not log spectrum payloads. Its base URL is an environment
  setting (`environment.ts`), so local and test builds can point elsewhere.
- **Failures:** if the admin setting cannot be read, fail closed (no overlay) with a visible note.
  Read the setting once per session and reuse it.

## Backend: shared with production, always backwards compatible

`master_ai` BinView uses the same Go backend and database as production, and production `go-modules`
must always stay backwards compatible. So the admin setting is built as follows:

- **Additive only:** one new table and new endpoints (read for signed-in users, write for admins).
  No existing table, column, endpoint or response shape changes. Production BinView never calls the
  new endpoints and sees no difference.
- **Default off:** an empty table means every method is off, so deploying the backend changes
  nothing until an admin flips a toggle.
- **Rollout order:** backend first (it is inert), then the `master_ai` frontend, then the service
  can go live independently. The frontend must tolerate the endpoint being absent (visible note, no
  overlay), since it may be deployed against a backend that lacks it.
- **Owner (my call):** the endpoints belong in `go-modules`, following its existing admin-settings
  pattern; the builder confirms the exact module. Migration follows the repo's normal process.

## Left to the builder

Package layout and name, framework details, JSON field names, rate-limit numbers, the plot library
for the web page, how the admin setting is stored and authorised in the Go backend, deployment
(whiteale nginx, certificate, process supervision, CI), and tests.
