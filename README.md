# Impossible-mass denoising

A noise filter for MS/MS fragment spectra. For each fragment m/z it asks whether **any** CHNOPS
composition could produce that exact mass at that polarity, and removes the peaks where none can.

Read "impossible" precisely: impossible **under the configured model**, which by default is
C/H/N/O/P/S only, singly charged, monoisotopic, halogens and alkali metals off. A halogenated,
alkali-adducted, deuterated or multiply charged fragment can be perfectly real and still be
rejected. Halogens can be switched on (see [Halogens](#halogens)), and so can deuterium labels
for labelled internal standards (see [Use](#use)). Multiply charged ions are the largest such
class in our data. If your spectra are rich in any of those, measure the false-flag rate on your
own reference set before deploying.

It needs nothing but the m/z and the polarity — no precursor formula, no adduct, no candidate
structure. That is the point: it runs on unannotated bins, where formula-based denoisers cannot.

It removes noise on Orbitrap data. Leave it off for TTOF data unless your own data show a benefit,
and do not use it on QTOF data; see [Which instruments it helps](#which-instruments-it-helps).

## Install

Python 3.9+ and numpy. Nothing else. Verified on a clean virtualenv with Python 3.9.6 and numpy 2.0.2.

```bash
pip install -r requirements.txt
python test_smoke.py          # exits non-zero on any failure
```

The smoke test also carries one case for each failure class found in an audit on LC-BinBase data
(issues #1 to #5). A class that is not fixed yet prints as XFAIL and does not fail the run.
CI also runs `test_audit_controls.py`: a pinned default-result check on 256 public MassBank spectra,
a fake-deuterium negative control, and generated CHNOPS and impossible-mass cases. See
[`tests/README.md`](tests/README.md) for fixture attribution.

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
kept = denoise_spectrum(peaks, mode="pos", deuterium=9)      # a D9-labelled internal standard
kept = denoise_spectrum(peaks, mode="pos", carbon13=6, nitrogen15=2)   # a 13C6 15N2 standard
kept = denoise_spectrum(peaks, mode="pos", precursor_mz=prec)   # never remove the precursor
kept = denoise_spectrum(peaks, mode="neg", precursor_mz=prec, keep_top=3)  # for library matching
```

`mode` is `"neg"` or `"pos"`. The return is the surviving peaks, same tuples, same order.
Intensities are passed through untouched — this filter removes peaks, it never rescales them.

`mode` is validated: anything other than `"neg"` or `"pos"` raises `ValueError`. This matters —
the underlying module treats any string that is not exactly `"neg"` as positive, so an unvalidated
`"negative"` would silently score the wrong polarity.

An empty result is legitimate and means every peak in that spectrum was impossible.

**Deuterium-labelled internal standards.** Deuterium is outside the alphabet, so the filter
removes the deuterated ions of a labelled standard: often its precursor and base peak, sometimes
every peak. Pass the standard's label count, for example `deuterium=9` for a D9 standard. A peak
is then also kept when taking off between one and that many deuterium labels (1.0063 Da each)
leaves a possible ion, so fragments that kept all, some or none of the labels survive, and a peak
that no labelled composition explains is still removed. Take the count from the library entry or
the standard's name, never from the peaks; in lipid names, sphingoid-base notation such as d18:1
is not a label. Use the option only for that standard's spectra: each label adds a lookup and
makes the filter a little more permissive. With `deuterium=9`, the share of masses between 50 and
700 Da that the filter can reject falls from 35% to 31%, and a cold lookup takes about 9 µs
instead of 5 to 6.
For lipid standards with d5 to d9 labels on larger ions, trying every allowed label shift can
make most peaks pass. Check that the option still removes noise on your lipid method before using
it there; the benefit seen for small polar standards does not carry over automatically.

**¹³C and ¹⁵N labelled standards.** `carbon13=n` and `nitrogen15=n` work like `deuterium=`: a peak
is also kept when taking off between one and n labels (1.0034 Da per ¹³C, 0.9970 Da per ¹⁵N)
leaves a possible ion. They combine with `deuterium=` and with each other, for example
`carbon13=6, nitrogen15=2` for a ¹³C₆ ¹⁵N₂ amino acid. The filter tries every mix of 0 to n of
each declared label, so the lookups per peak are (d+1)(c+1)(n+1); declare only the labels the
standard carries and never infer them from the peaks. The need is much smaller than for
deuterium, because a ¹³C or ¹⁵N step is close to the hydrogen step the alphabet already
contains. On 1,000 random small ions (up to 8 carbons) the default removed the labelled ion in
0.7% to 5.5% of cases for ¹³C₁ to ¹³C₆ (more when the ions carry little sulfur or phosphorus),
under 1% for ¹⁵N and 2% to 20% for D₁ to D₉; with the option declared none were removed. For
larger molecules (3 to 40 carbons) the default removes at most 3% for any label. ¹⁷O, ¹⁸O, ³³S
and ³⁴S were tested the same way and the default already accepts them (0% to 0.6% removed, up to
four ¹⁸O or two ³⁴S), so they have no option. Isotopes that are **not** covered: ¹⁴C (radioactive, not a
mass-spectrometry label), ¹⁷O, ¹⁸O, ³³S and ³⁴S (tested, not needed, see above) and the natural
isotope peaks of an unlabelled ion. `count_deuterium` does not read ¹³C or ¹⁵N from names, so pass the counts
yourself.

`count_deuterium(name)` extracts the largest D-label token from a known standard's library name,
or returns 0 if none is present. For example, `count_deuterium("1_D9-Choline iSTD")` is 9 and
`count_deuterium("1_Sphingosine d17:1 iSTD")` is 0. Check the name before passing its count to
`denoise_spectrum`; the helper does not establish that a spectrum belongs to that standard.

**Keeping the precursor.** The filter judges every peak by its m/z alone, so it can remove the
precursor itself: a doubly charged precursor has no singly charged composition, and a precursor
measured more than 5 mDa off its true mass can miss every composition. `precursor_mz=` keeps any
peak within 20 mDa of the precursor m/z you pass, from the library entry or the scan header; the
filter does not guess it from the peaks. The window is wider than the filter's 5 mDa because a
stored precursor m/z and the measured peak can differ by 10 mDa or more in uncalibrated spectra.
It is off by default, costs one peak per spectrum and changes nothing else. A matcher that removes
the precursor before scoring gets the same score with or without it; the option keeps the peak
for anything else that reads it.

**Protecting intense peaks.** `keep_top=k` keeps the `k` most intense input peaks regardless of
their mass verdict (`keep_top=1` protects the base peak). It is off by default, and ties are
resolved by input order. When you also pass `precursor_mz=`, only peaks more than 20 mDa below the
precursor are ranked, so `keep_top=1` protects the strongest fragment. The precursor is already
kept by its own option, and many matchers drop the precursor and every ion above it before
scoring, so a slot spent on those peaks would protect nothing that is scored.

**For library matching, use `keep_top=3` with `precursor_mz=`.** With `keep_top=1` most of the lost
matches remain, because the second and third strongest fragments are often removed as well;
`keep_top=3` recovers far more of them and breaks very few matches that pass without it.

The trade-off: the guard keeps a peak because it is intense, not because its mass is possible.
In library matching, the impossible peaks it keeps are often background ions, such as inorganic
clusters in negative mode, that the library spectrum contains as well. Keeping them stabilises
those matches, but it is not noise removal. Denoising the library spectra with the same call is an
alternative; in our tests it recovered fewer matches than `keep_top=3` and added nothing on top of
it. Use the guard when match stability matters more than removing every peak unexplained by
CHNOPS, and check both correct and incorrect library matches on your own data before using it in a
matching pipeline.

Also available: `is_possible(mz, mode, halogens=False, *, deuterium=0, carbon13=0, nitrogen15=0)` for a single peak,
`fingerprint()` for the chemistry hash, `table_status()` and `halogen_table_status()` for whether
each table loaded, and `cache_size()` / `clear_cache()` (see below).

## Where it goes in the pipeline

On the query spectrum, immediately **before** spectral library search, and **before** any
intensity floor — that is the order the filter was validated in: the filter sees the raw peak list,
and the 1% base-peak cut is applied to what survives. It replaces nothing; that cut can stay.

Applying the floor first has not been tested and is not equivalent, because the floor is relative
to the base peak and removing peaks can change which peak that is.

It is a pure function of the peak list. It has no state, no I/O after the table loads, no network
calls, and is thread-safe for reads. The module memoises results in a process-local dict, so a
long-lived worker gets faster as fragment masses recur.

For library matching, evaluate denoising both the query and the library spectrum with the same
settings and ordering of the intensity floor. In the larger audit this retained more previously
passing identified-compound matches than filtering the query alone. It also means re-processing
the library and can change which matches cross a score threshold. Check incorrect matches and
recalibrate that threshold before deployment; a higher similarity score alone is not evidence of
a better identification.

## Which instruments it helps

The check behind this section used raw MS/MS scans from the lab's LC-BinBase methods. Each fragment
ion was scored by how often it recurs across repeat scans of the same precursor, and the ions the
filter removes were compared with the ions it keeps at the same relative intensity. Real fragments
come back scan after scan; random noise does not.

- On the Orbitrap methods, the removed ions recur far less often than the kept ones. The filter is
  removing noise there.
- On the TTOF HILIC methods, the removed ions recur as often as the kept ones, so the filter
  showed no measurable benefit there. Measure it on your own data before using it on TTOF.
- The one QTOF method with raw files showed only a small difference, and ions recurred rarely
  whether removed or kept.
- Below 1% of the base peak, the difference still depends on the instrument. Removed Orbitrap
  ions recur much less often than kept ions at the same intensity; TTOF shows no measurable gap,
  while the QTOF difference is small.

Recurring across scans is not proof that an ion is real, because an artifact at a fixed m/z recurs
too; the Orbitrap result holds either way.

**Recommendation by instrument.**

| Instrument | Use the filter? |
|---|---|
| Orbitrap | Yes. |
| TTOF | Off by default. Turn it on only after checking both correct and incorrect matches on your own data. |
| QTOF | No. |

The reason is fragment mass accuracy. The filter assumes a real ion lies within 5 mDa of an exact
composition, and the TTOF and QTOF data we checked break that assumption in three ways:

- On TTOF, some real small fragments are recorded tens of mDa off their exact mass, so the filter
  removes them. In labelled internal standards this costs matches that `deuterium=` cannot
  rescue, because the label is not the problem.
- On TTOF, the precursor of some small standards is recorded in the MS/MS scan about 0.2 Da below
  its true m/z. `precursor_mz=` protects only 20 mDa around the m/z you pass, so it cannot reach
  such a peak. An offset this large points to an instrument calibration problem worth checking at
  the source.
- On QTOF, fragment masses scatter by tenths of a Da, so the 5 mDa test cannot recognise real
  fragments at all.

On TTOF the filter also finds nothing to remove in many spectra, because with that mass accuracy few
masses are provably impossible. A wider tolerance would keep the misplaced fragments but not the
offset precursors, and it weakens the filter everywhere; it has not been tested and is not
recommended.

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

**Heavy isotope peaks are kept through their light partner.** The alphabet holds the lightest
isotope of each element, so a ³⁷Cl or ⁸¹Br peak such as ⁸¹Br⁻ often has no composition of its
own. With `halogens=True`, `denoise_spectrum` also keeps a peak that sits 1.997 Da (³⁷Cl − ³⁵Cl)
or 1.998 Da (⁸¹Br − ⁷⁹Br) above a peak it keeps, within 5 mDa. The rule chains, so the M+4 peak of
CCl₃⁻ is kept through its M+2. A heavy isotope peak whose partner is missing or removed is still
removed, and `is_possible`, which sees one peak at a time, still rejects it. The rule needs no new
table and leaves the fingerprint unchanged. It also keeps ³⁴S isotope peaks, which sit 1.996 Da
above their partner and are just as real, and it can keep a weak peak that happens to sit at that
spacing above a kept peak.

Turn it on when halogenated compounds matter to you and you can accept the weaker filtering above
about 300 Da. The halogen table was verified against the solver with zero disagreements on 320
random masses at 5 and 20 mDa and on 398 fragment peaks from real halogenated spectra. It carries
the same chemistry fingerprint as the CHNOPS table; the alphabet is recorded in each table's config.

## Doubly charged ions

The filter treats every peak as singly charged, so a doubly charged ion can be real and still be
removed. When the precursor is multiply charged, as the library adduct says for [M+2H]²⁺ or
[M−2H]²⁻, pass its m/z with `precursor_mz=` and the precursor is kept whatever its verdict.

There is no option to accept any peak as doubly charged. A fragment's charge is not known from
the spectrum, and such a rule makes most masses explainable: at 5 mDa, nothing above about 276 m/z
could be removed any more, and the share of masses the filter can reject between 50 and 700 Da
falls from 35% to 13%. In our Orbitrap data, the extra peaks such a rule would keep behave like
noise across repeat scans. The research module keeps the rule as `possible_z2` for anyone who
wants to measure it on their own data.

## What it does not do

- It does not decide whether an annotation is correct. It removes peaks; everything downstream is
  unchanged.
- It cannot help a spectrum whose peaks are all chemically plausible. On TTOF data it often finds
  nothing to remove; see [Which instruments it helps](#which-instruments-it-helps).
- It saturates at high mass. At the default 5 mDa tolerance it can reject almost nothing above
  about 550 Da, and nothing at all above about 670 Da, where every 1 mDa slot holds a valid
  composition.
- It is not a formula assignment. A `True` verdict means "some composition exists", not "this
  composition is the one".
- A `False` verdict means "no composition in the alphabet exists at this tolerance, singly
  charged, monoisotopic". It does not mean the peak is not a real ion — see the note at the top
  about halogens, alkali adducts, deuterium and multiple charge.
- The per-peak test does not model isotopes: the alphabet uses the lightest isotope of each
  element. `denoise_spectrum` with `halogens=True` keeps a ³⁷Cl or ⁸¹Br peak whose light partner
  it keeps (see [Halogens](#halogens)), but heavy isotope peaks of chlorine and bromine with no
  kept partner — ³⁷Cl at about a quarter of natural chlorine, ⁸¹Br at
  about half of natural bromine — are rejected even with `halogens=True`.
- Above the table ceiling of 1700 Da every mass is reported possible, immediately and without the
  solver. That is exact: every 1 mDa slot in the table's top 16 Da holds a valid composition, and
  adding CH₂ to a valid composition keeps it valid, so every higher mass has one within 1 mDa. The
  flip side is that the filter can remove nothing up there. Such peaks are rare in LC-BinBase but
  do occur.

## Status

The filter has been checked across LC-BinBase acquisition methods; the measurements live in the
lab's internal findings document rather than here.

Two limits matter operationally. The benefit is concentrated on Orbitrap data; see
[Which instruments it helps](#which-instruments-it-helps). And denoising raises the similarity of
the correct match and of its competitors alike, so treat it as a score improvement rather than a
ranking improvement: it will not by itself change which candidate ranks first.

## Service

`denoise_service/` wraps the filter as a public REST service with a paste-and-plot web page.
Start it with:

```
python -m denoise_service --config denoise_service/config.example.yaml
```

| Method and path | Purpose |
| --- | --- |
| `GET /` | Web page: paste a spectrum, see the plot |
| `POST /v1/classify` | Classify one spectrum |
| `POST /v1/classify/batch` | Classify several spectra |
| `POST /v1/convert` | Clean text in its own format: MSP, MGF, `mz:int` strings or two columns |
| `GET /healthz` | Health and chemistry fingerprint |

`POST /v1/convert` takes `{"text": "...", "mode": "pos"|"neg", "precursor_mz": 123.4, "halogens": false,
"deuterium": 0, "carbon13": 0, "nitrogen15": 0, "format": "msp"|"mgf"|"strings"|"columns"}` (only
`text` is required; `format` overrides detection) and returns `{fingerprint, format, text, records, summary}`.
Cleaning removes the ions the filter calls impossible and nothing else: kept peaks keep their original
text, MSP `Num Peaks:` is rewritten when ions were removed, and every other line passes through verbatim.
Input format equals output format, with `strings` giving one cleaned line per input line (an all-removed
spectrum becomes an empty line). Ion mode and precursor come from each record's metadata (MSP
`IonMode`/`Ion_mode`/`Ionization`, `PrecursorMZ`; MGF `IONMODE`, `PEPMASS`, `CHARGE` sign) and otherwise
from the request. A record that cannot be cleaned (no ion mode, unparseable, over `max_peaks`) is
returned unchanged with a short `error` in its `records` entry; the request still succeeds. Each
`records` entry is `{index, name, total, impossible, error, mode, precursor_mz}`.

The public URL is https://denoise.metabolomics.us. Deployment (nginx, DNS, the systemd unit) is in
[ops/deploy/README.md](ops/deploy/README.md). The service imports Ziyue's modules unmodified.

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
