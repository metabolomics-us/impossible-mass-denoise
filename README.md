# Impossible-mass denoising

A noise filter for MS/MS fragment spectra. For each fragment m/z it asks whether **any** CHNOPS
composition could produce that exact mass at that polarity, and removes the peaks where none can.

Read "impossible" precisely: impossible **under the configured model**, which is C/H/N/O/P/S only,
singly charged, halogens and alkali metals off. A halogenated, alkali-adducted, deuterated or
multiply charged fragment can be perfectly real and still be rejected. Multiply charged ions are
the largest such class in our data. If your spectra are rich in any of those, measure the
false-flag rate on your own reference set before deploying.

It needs nothing but the m/z and the polarity — no precursor formula, no adduct, no candidate
structure. That is the point: it runs on unannotated bins, where formula-based denoisers cannot.

## Install

Python 3.9+ and numpy. Nothing else. Verified on a clean virtualenv with Python 3.9.6 and numpy 2.0.2.

```bash
pip install -r requirements.txt
python test_smoke.py          # exits non-zero on any failure
```

This is a drop-in directory, **not** a pip-installable distribution — there is no `pyproject.toml`
and nothing to `pip install .`. Either run from inside the directory, or put it on the path:

```python
import sys; sys.path.insert(0, "/path/to/impossible-mass-denoise")
from denoise import denoise_spectrum
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

On the query spectrum, immediately **before** spectral library search, and **before** any
intensity floor — that is the order the filter was validated in: the filter sees the raw peak list,
and the 1% base-peak cut is applied to what survives. It replaces nothing; that cut can stay.

Applying the floor first has not been tested and is not equivalent, because the floor is relative
to the base peak and removing peaks can change which peak that is.

It is a pure function of the peak list. It has no state, no I/O after the table loads, no network
calls, and is thread-safe for reads. The module memoises results in a process-local dict, so a
long-lived worker gets faster as fragment masses recur.

Applying it to library spectra as well as query spectra raises the similarity scores slightly
further in our tests — it does not improve which candidate ranks first — and it means re-processing
the library. Query-side only is the simpler deployment.

## Performance

Two numbers, because they differ by a factor of eight and only one of them describes a fresh
workload. On this hardware, over 100,000 distinct masses:

| | per peak |
|---|---|
| cold — a mass not seen before | **5.8 µs** |
| warm — a repeat of a mass already asked about | **0.7 µs** |

A 20-peak spectrum of entirely unseen masses costs about 0.12 ms, so 50,000 such spectra is around
6 seconds of CPU. Real workloads land between the two, since fragment masses recur heavily across a
database. Quote the cold figure when sizing.

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
- A `False` verdict means "no CHNOPS composition exists at this tolerance, singly charged". It does
  not mean the peak is not a real ion — see the note at the top about halogens, alkali adducts,
  deuterium and multiple charge.
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
