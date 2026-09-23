# Impossible-mass denoising

A noise filter for MS/MS fragment spectra. For each fragment m/z it asks whether **any** CHNOPS
composition could produce that exact mass at that polarity, and removes the peaks where none can.

Read "impossible" precisely: impossible **under the configured model**, which by default is
C/H/N/O/P/S only, singly charged, monoisotopic, halogens and alkali metals off. A halogenated,
alkali-adducted, deuterated or multiply charged fragment can be perfectly real and still be
rejected. Halogens can be switched on — see [Halogens](#halogens). Multiply charged ions are
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

Two precomputed tables must sit beside `impossible_mass_denoise.py`: `possible_mass_table.npz`
(1.5 MB, CHNOPS) and `possible_mass_table_halogen.npz` (1.9 MB, CHNOPS plus Cl/F/Br/I). Each holds
the reachable masses on a 1 mDa grid up to 1700 Da. Without a table the module falls back to
solving each mass, which is correct but far slower — up to a minute per mass with halogens.

## Use

```python
from denoise import denoise_spectrum

kept = denoise_spectrum(peaks, mode="neg")                   # peaks = [(mz, intensity), ...]
kept = denoise_spectrum(peaks, mode="neg", halogens=True)    # also allow Cl/F/Br/I
```

`mode` is `"neg"` or `"pos"`. The return is the surviving peaks, same tuples, same order.
Intensities are passed through untouched — this filter removes peaks, it never rescales them.

`mode` is validated: anything other than `"neg"` or `"pos"` raises `ValueError`. This matters —
the underlying module treats any string that is not exactly `"neg"` as positive, so an unvalidated
`"negative"` would silently score the wrong polarity.

An empty result is legitimate and means every peak in that spectrum was impossible.

Also available: `is_possible(mz, mode, halogens=False)` for a single peak, `fingerprint()` for the
chemistry hash, `table_status()` and `halogen_table_status()` for whether each table loaded, and
`cache_size()` / `clear_cache()` (see below).

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

The halogen table is exactly as fast: 5.0 µs cold, same as CHNOPS.

Memory: each table is under 2 MB on disk and loads once per process, on first use.

**The filter memoises every m/z it is asked about, and that cache is unbounded.** It grows by
roughly 26 MB of resident memory per 100,000 distinct masses. For batch jobs this is free speed and
the process exits anyway. For a long-lived service, call `clear_cache()` periodically — it costs
only the re-lookups, which are microseconds. `cache_size()` reports the current entry count.

## Configuration — do not change these without re-validating

The validated settings are `d_max=0`, `union=True`, halogens off, alkali off, and they are the
defaults in `denoise.py`. The module exposes other validity models for research use; they were
measured to be worse. `halogens=True` is supported and fast, but it is a trade-off rather than a
strict improvement — read [Halogens](#halogens) before turning it on.

Log `fingerprint()` alongside results. Two runs that disagree on that hash are not running the same
chemistry, and their outputs are not comparable. The validated value is `5aae4ec26694da4d`.

## Halogens

`denoise_spectrum(peaks, mode, halogens=True)` adds Cl, F, Br and I to the alphabet. What that buys
and what it costs:

**It keeps fragments only halogens can explain.** Brominated and chlorinated compounds often
fragment in negative mode by losing a halide, so bromide (m/z 78.919) or chloride can be the base
peak. No CHNOPS composition reaches those masses, so the default filter deletes them, and with them
most of the spectrum's intensity. The halogen alphabet keeps them.

**It rejects less noise everywhere else.** More elements make more masses explainable, so fewer
peaks can be flagged. Share of masses the filter can reject, 5 mDa tolerance, negative mode:

| mass range | CHNOPS | with halogens |
|---|---|---|
| 50–150 Da | 83% | 82% |
| 150–250 Da | 64% | 60% |
| 250–350 Da | 45% | 40% |
| 350–450 Da | 26% | 19% |
| 450–550 Da | 8% | 2% |
| 50–700 Da overall | 35% | 31% |

**Fluorine alone rarely needs it.** Fluorine sits within 0.002 Da of a whole-number mass, so
perfluorinated fragments usually land near some CHNOPS composition and survive the default filter
anyway. The damage comes from chlorine and bromine, whose masses sit far from whole numbers.

Turn it on when halogenated compounds matter to you and you can accept the weaker filtering above
about 300 Da. The halogen table was verified against the solver with zero disagreements on 320
random masses at 5 and 20 mDa and on 398 fragment peaks from real halogenated spectra. It carries
the same chemistry fingerprint as the CHNOPS table; the alphabet is recorded in each table's config.

## What it does not do

- It does not decide whether an annotation is correct. It removes peaks; everything downstream is
  unchanged.
- It cannot help a spectrum whose peaks are all chemically plausible. On our TTOF methods it finds
  nothing to remove in over half of spectra, because at lower mass accuracy more masses have a
  valid composition.
- It saturates at high mass. At the default 5 mDa tolerance it can reject almost nothing above
  about 550 Da, and nothing at all above about 670 Da, where every 1 mDa slot holds a valid
  composition.
- It is not a formula assignment. A `True` verdict means "some composition exists", not "this
  composition is the one".
- A `False` verdict means "no composition in the alphabet exists at this tolerance, singly
  charged, monoisotopic". It does not mean the peak is not a real ion — see the note at the top
  about halogens, alkali adducts, deuterium and multiple charge.
- It does not model isotopes. The alphabet uses the lightest isotope of each element, so the heavy
  isotope peaks of chlorine and bromine — ³⁷Cl at about a quarter of natural chlorine, ⁸¹Br at
  about half of natural bromine — are rejected even with `halogens=True`.
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

Released under the MIT License — see `LICENSE`.

Method: J. Meija, "Mathematical tools in analytical mass spectrometry" (2006) — the Diophantine
feasibility test for elemental compositions. The chemical-validity layer on top (SENIOR rules,
degree of unsaturation, element caps, and a union of an ion-tolerant and a neutral-strict model)
is ours.
