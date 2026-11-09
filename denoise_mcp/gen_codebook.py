"""Write `reason-codes.json`, the codebook the server publishes as a resource.

    python -m denoise_mcp.gen_codebook

The file is generated, not hand-written: `explain.py` is what emits these codes, so a code added
there appears here by running this, and a code documented here but never emitted is what the tests
in `tests/test_explain.py` and `tests/test_deployable.py` catch. Regenerate in the same commit as
any change to the reason codes.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import explain

FILENAME = "reason-codes.json"


def book() -> dict:
    """The codebook: every reason the filter can give, in the order `explain` lists them."""
    return {
        "codes": list(explain.REASON_CODES),
        "meaning": dict(explain.REASON_MEANING),
        "strong": sorted(explain.STRONG_CODES),
        "labels": list(explain.LABEL_STEPS),
        "table_max_mz": explain.TABLE_MAX_MZ,
        "tolerance_da": explain.DEPLOYED_TOL,
        "notes": ("A reason code is something this build of the filter actually checked. Codes are "
                  "per peak and several can apply at once. 'strong' codes name a chemistry the "
                  "caller could declare to change the verdict; the others only state a distance. "
                  "Above table_max_mz nothing is ever reported impossible: the tables say possible "
                  "for every mass there, so a removal that high is a fault, not a verdict."),
    }


def main() -> int:
    path = Path(__file__).with_name(FILENAME)
    path.write_text(json.dumps(book(), indent=2, sort_keys=True) + "\n")
    print("wrote %s (%d codes)" % (path.name, len(book()["codes"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
