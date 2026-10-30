"""Run the service: `python -m denoise_service --config config.yaml`."""
import argparse
import logging
import sys
from typing import Optional, TextIO

import uvicorn

from denoise_service.app import create_app
from denoise_service.config import ConfigError, load_config


def configure_access_logging(stream: Optional[TextIO] = None) -> None:
    """Send the per-request access line (method, path, status, ions, ms) to stderr.

    Uvicorn's logging config never prints INFO from our logger, so attach one handler ourselves.
    Idempotent: a second call does not add a second handler.
    """
    log = logging.getLogger("denoise_service.access")
    log.setLevel(logging.INFO)
    log.propagate = False
    if any(getattr(h, "_denoise_access", False) for h in log.handlers):
        return
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    handler._denoise_access = True  # type: ignore[attr-defined]
    log.addHandler(handler)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="denoise_service")
    parser.add_argument("--config", required=True, help="path to the YAML config file")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print("config error: %s" % exc, file=sys.stderr)
        return 2
    configure_access_logging()
    # One worker: the formula cache and the rate limiter are shared in-process.
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, workers=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
