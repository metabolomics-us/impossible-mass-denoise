#!/usr/bin/env bash
set -euo pipefail

# The service installs from the same hash-pinned lock the deploy builds its
# venv from, so the tested environment is the deployed one. Test-only tools
# stay unpinned and are never deployed.
python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check --require-hashes -r requirements-service.lock
.venv/bin/python -m pip install --disable-pip-version-check "pytest>=8.0" "httpx>=0.27"
.venv/bin/python -m pytest denoise_service
