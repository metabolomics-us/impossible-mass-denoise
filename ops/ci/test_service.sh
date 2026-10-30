#!/usr/bin/env bash
set -euo pipefail

python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt -r requirements-service.txt
.venv/bin/python -m pytest denoise_service
