"""Model Context Protocol surface for the impossible-mass metabolomics filter.

Three layers, deliberately separated:

* `limits.py`, `explain.py`, `clean.py` - pure Python plus numpy, Python 3.9 compatible, no web or
  MCP libraries. This is the code that answers "why was this peak removed?" and it runs unchanged
  on the production analysis nodes, which are Python 3.9.
* `server.py` - the MCP tools, schemas and streaming. Needs Python >= 3.10 because the MCP SDK
  does, so it is deployed in its own virtualenv next to the 3.9 analysis stack.
* `__main__.py` - the entry point operators run: `python -m denoise_mcp --stdio`, or
  `--host 127.0.0.1 --port 8301 --stateless` behind a reverse proxy at denoise.mcp.metabolomics.us.

Verdicts are produced by `denoise_service.classify`, the same call the REST service makes
(`denoise_service/tests/test_mcp_agreement.py` proves the two surfaces agree), so adding this
surface cannot create a second, drifting answer to "is this peak possible?".
"""
from __future__ import annotations

from . import clean, explain, limits
from .clean import Peak, Removed, Result, clean_spectrum
from .limits import LimitError, Limits

__all__ = ["clean", "clean_spectrum", "explain", "limits", "Peak", "Removed", "Result",
           "LimitError", "Limits"]
