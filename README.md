# Impossible-mass denoising

A noise filter for MS/MS fragment spectra. For each fragment m/z it asks whether **any** CHNOPS
composition could produce that exact mass at that polarity. A peak with no chemically legal
composition cannot be a real fragment ion, so it is removed.

It needs nothing but the m/z and the polarity — no precursor formula, no adduct, no candidate
structure. That is the point: it runs on unannotated bins, where formula-based denoisers cannot.

## Install

Python 3.9+ and numpy. Nothing else. Verified on a clean virtualenv with Python 3.9.6 and numpy 2.0.2.

```bash
pip install -r requirements.txt
python test_smoke.py          # exits non-zero on any failure
```

The 1.5 MB `possible_mass_table.npz` must sit beside `impossible_mass_denoise.py`. It is a
precomputed table of reachable masses on a 1 mDa grid up to 1700 Da; without it the module falls
back to solving each mass, which is correct but far slower.

## Use

```python
from denoise import denoise_spectrum

kept = denoise_spectrum(peaks, mode="neg")     # peaks = [(mz, intensity), ...]
```

`mode` is `"neg"` or `"pos"`. The return is the surviving peaks, same tuples, same order.
Intensities are passed through untouched — this filter removes peaks, it never rescales them.

`mode` is validated: anything other than `"neg"` or `"pos"` raises `ValueError`. This matters —
the underlying module treats any string that is not exactly `"neg"` as positive, so an unvalidated
`"negative"` would silently score the wrong polarity.

An empty result is legitimate and means every peak in that spectrum was impossible.

Also available: `is_possible(mz, mode)` for a single peak, `fingerprint()` for the chemistry hash,
`table_status()` for whether the table loaded, and `cache_size()` / `clear_cache()` (see below).

## Where it goes in the pipeline

Immediately **before** spectral library search, on the query spectrum, and after any existing
intensity floor. It replaces nothing — the current 1% base-peak cut can stay.

It is a pure function of the peak list. It has no state, no I/O after the table loads, no network
calls, and is thread-safe for reads. The module memoises results in a process-local dict, so a
long-lived worker gets faster as fragment masses recur.

Applying it to library spectra as well as query spectra is supported and performs slightly better
in our tests, but it means re-processing the library. Query-side only is the simpler deployment.

## Performance

**1.6 µs per peak** measured by the smoke test on this hardware — about 0.03 ms for a 20-peak
spectrum. Filtering a 50,000-spectrum database is a few seconds of CPU. The filter is a table
lookup; if a pipeline using it is slow, the cost is elsewhere.

Memory: the table is ~1.5 MB on disk and loads once per process.

**The filter memoises every m/z it is asked about, and that cache is unbounded.** It grows by
roughly 26 MB of resident memory per 100,000 distinct masses. For batch jobs this is free speed and
the process exits anyway. For a long-lived service, call `clear_cache()` periodically — it costs
only the re-lookups, which are microseconds. `cache_size()` reports the current entry count.

## Configuration — do not change these without re-validating

The deployed settings are `d_max=0`, `union=True`, halogens off, alkali off. `denoise.py` pins
them. The module exposes other validity models for research use; they were measured to be worse.
Halogens off in particular is deliberate: the shipped table is CHNOPS-only, and turning halogens on
falls back to the solver at roughly 300 ms per peak.

Log `fingerprint()` alongside results. Two runs that disagree on that hash are not running the same
chemistry, and their outputs are not comparable. The validated value is `5aae4ec26694da4d`.

## What it does not do

- It does not decide whether an annotation is correct. It removes peaks; everything downstream is
  unchanged.
- It cannot help a spectrum whose peaks are all chemically plausible. On our TTOF methods it finds
  nothing to remove in over half of spectra, because at lower mass accuracy more masses have a
  valid composition.
- It saturates at high mass. Above roughly 670 Da, CHNOPS compositions are dense enough that
  almost any mass is reachable, so there is little left to flag.
- It is not a formula assignment. A `True` verdict means "some composition exists", not "this
  composition is the one".
- Above the table ceiling of 1700 Da every mass is reported possible. That is very nearly true
  chemically, and nothing in LC-BinBase exceeds it, but do not read it as a verdict.

## Status

The filter is validated across the six LC-BinBase acquisition methods; the measurements live in the
lab's internal findings document rather than here.

Two limits matter operationally. The benefit is concentrated on high-mass-accuracy data — at lower
mass accuracy more masses have a valid composition, so there is less to flag. And denoising raises
the similarity of the correct match and of its competitors alike, so treat it as a score
improvement rather than a ranking improvement: it will not by itself change which candidate ranks
first.

## Provenance

`impossible_mass_denoise.py` is the validated research module, unchanged except that the table path
is package-relative and a command-line demo that read internal data files was removed. The table is
built by `build_possible_mass_table.py` in the Fiehn lab research repository; rebuilding it should
reproduce fingerprint `5aae4ec26694da4d`.

No licence is set yet — add one before anyone outside the lab relies on this.

Method: J. Meija, "Mathematical tools in analytical mass spectrometry" (2006) — the Diophantine
feasibility test for elemental compositions. The chemical-validity layer on top (SENIOR rules,
degree of unsaturation, element caps, and a union of an ion-tolerant and a neutral-strict model)
is ours.
