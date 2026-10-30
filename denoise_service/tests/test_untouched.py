"""Ziyue's research module must stay byte-for-byte what is on main."""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

EXPECTED = {
    "denoise.py": "8799d5087e3cca5aa146c6fcbcd099c697a4927bdc796cc62cff1c261bf93071",
    "impossible_mass_denoise.py": "c6db26f7385c1df8bb223d3d795295895b1225ad77e1c5149e2aa7aa1d061180",
    "possible_mass_table.npz": "2b8bf83f502459a38184d30d96387bac882110c4b3cd02f65570429c8cfa2276",
    "possible_mass_table_halogen.npz": "8c94605905c92850cd8baaf642bbde93aa3100d52b6655880c9079f46397909f",
}


def test_ziyue_files_untouched():
    for name, digest in EXPECTED.items():
        got = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        assert got == digest, "%s changed" % name
