#!/usr/bin/env bash
set -euo pipefail

# The tests run on the CI image's own Python, which is newer than the nodes'
# (3.9): requirements-service.lock pins wheels for 3.9..3.12, so installing it
# here would force a source build of numpy. The lock is instead pinned by
# test_deploy_data.py (hash format) and exercised for real by the deploy's
# readiness probe on the nodes (fingerprint + smoke classification).
python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt -r requirements-service.txt
.venv/bin/python -m pytest denoise_service
