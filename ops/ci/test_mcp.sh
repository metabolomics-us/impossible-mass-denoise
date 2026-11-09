#!/usr/bin/env bash
set -euo pipefail

# Gate for the MCP surface (denoise_mcp). It gets its own venv: the MCP SDK needs Python >= 3.10,
# while everything else in this repo is pinned for the nodes' Python 3.9. The deployment installs
# requirements-mcp.lock (hashes, --only-binary); this gate installs the loose requirements-mcp.txt,
# because a gate that cannot install on the runner's Python proves nothing about the code.
python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    sys.exit("the MCP gate needs Python >= 3.10 (the MCP SDK requires it); this runner runs %s"
             % sys.version.split()[0])
PY

VENV="${VENV:-.venv-mcp}"          # set it to keep an existing .venv-mcp when running locally
rm -rf "$VENV"
python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --disable-pip-version-check -r requirements.txt -r requirements-mcp.txt

# Reason codes come from explain.py; the published codebook is generated from them. A code added,
# renamed or dropped without regenerating the file fails here rather than at a client reading it.
"$VENV/bin/python" -m denoise_mcp.gen_codebook
if ! git diff --quiet -- denoise_mcp/reason-codes.json; then
  echo "denoise_mcp/reason-codes.json is stale: run 'python -m denoise_mcp.gen_codebook' and commit it" >&2
  git --no-pager diff -- denoise_mcp/reason-codes.json >&2
  exit 1
fi

# -q keeps the log readable; the two integration layers spawn a real server (stdio, then HTTP) so
# this step is slower than the service gate on purpose.
"$VENV/bin/python" -m pytest denoise_mcp -q --cov=denoise_mcp --cov-report=term-missing \
    --cov-fail-under=80
