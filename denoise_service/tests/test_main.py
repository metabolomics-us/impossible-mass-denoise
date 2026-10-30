"""The access log must reach stderr in production, once, without spectrum values."""
import io
import logging

import pytest

from denoise_service.__main__ import configure_access_logging


@pytest.fixture
def clean_logger():
    log = logging.getLogger("denoise_service.access")
    saved = (list(log.handlers), log.propagate, log.level)
    log.handlers[:] = []
    yield log
    log.handlers[:], log.propagate = saved[0], saved[1]
    log.setLevel(saved[2])


def test_configure_access_logging_is_idempotent_and_emits(clean_logger):
    stream = io.StringIO()
    configure_access_logging(stream)
    configure_access_logging(stream)
    assert len(clean_logger.handlers) == 1 and clean_logger.propagate is False
    clean_logger.info("%s %s %d ions=%s %.1fms", "POST", "/v1/classify", 200, 2, 1.5)
    out = stream.getvalue()
    assert "POST /v1/classify 200 ions=2 1.5ms" in out
    assert out.count("\n") == 1


def test_real_request_line_has_no_spectrum_values(clean_logger):
    from fastapi.testclient import TestClient
    from denoise_service.app import create_app
    from denoise_service.config import Config
    stream = io.StringIO()
    configure_access_logging(stream)
    with TestClient(create_app(Config())) as c:
        c.post("/v1/classify", json={"mode": "neg", "spectrum": "86.0612:45.5 104.1111:100"})
    assert "/v1/classify 200 ions=2" in stream.getvalue()
    for s in ("86.0612", "104.1111", "45.5"):
        assert s not in stream.getvalue()
