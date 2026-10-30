"""Run the service: `python -m denoise_service --config config.yaml`."""
import argparse
import sys

import uvicorn

from denoise_service.app import create_app
from denoise_service.config import ConfigError, load_config


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="denoise_service")
    parser.add_argument("--config", required=True, help="path to the YAML config file")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print("config error: %s" % exc, file=sys.stderr)
        return 2
    # One worker: the formula cache and the rate limiter are shared in-process.
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, workers=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
