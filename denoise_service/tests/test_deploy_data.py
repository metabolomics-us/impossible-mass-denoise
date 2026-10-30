"""The data the deploy reads from this repo: it must describe the code in the same commit."""
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from denoise_service.app import create_app
from denoise_service.config import Config

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def client():
    with TestClient(create_app(Config())) as c:
        yield c


def test_smoke_json_matches_the_live_service(client):
    smoke = json.loads((ROOT / "ops" / "deploy" / "smoke.json").read_text())
    health = client.get("/healthz").json()
    out = client.post("/v1/classify", json=smoke["request"]).json()
    assert health["fingerprint"] == smoke["fingerprint"]
    assert [i["possible"] for i in out["ions"]] == smoke["expect"]
    # A smoke test that cannot fail proves nothing: both verdicts must occur.
    assert True in smoke["expect"] and False in smoke["expect"]


def test_lock_file_hash_pins_every_requirement():
    text = (ROOT / "requirements-service.lock").read_text()
    pins = re.findall(r"^([A-Za-z0-9_.-]+)==\S+ \\$", text, flags=re.M)
    assert {"fastapi", "uvicorn", "numpy", "orjson", "pyyaml"} <= {p.lower() for p in pins}
    blocks = re.split(r"^(?=[A-Za-z0-9_.-]+==)", text, flags=re.M)[1:]
    assert blocks and all("--hash=sha256:" in b for b in blocks)
