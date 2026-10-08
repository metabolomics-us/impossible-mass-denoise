# Public spectrum fixture

`massbank_aces_su_256.json` contains the m/z and intensity pairs of 256 public MS2 records,
`MSBNK-ACES_SU-AS000001` through `MSBNK-ACES_SU-AS000256`, from
[MassBank-data](https://github.com/MassBank/MassBank-data/tree/befc8a1e2f2aef899747797c081a5d80fab12fe7/ACES_SU)
at commit `befc8a1e2f2aef899747797c081a5d80fab12fe7`. The records are credited to ACESx,
Jonathan W. Martin Group, Stockholm University and marked CC BY by their authors. Each original
record is available as `ACES_SU/<accession>.txt` at that revision.

Only accession, ion mode, m/z and intensity are retained here. The source names and formulas were
checked to exclude deuterium-labelled records, so the spectra can serve as a negative control.
The fixture contains no LC-BinBase data.
