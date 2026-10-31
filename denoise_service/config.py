"""Service configuration: one YAML file, strictly validated, no environment variables.

Everything an operator can tune lives in the file passed with `--config`. Unknown keys, wrong types
and non-positive limits fail startup with a `ConfigError` naming the key, so a typo such as
`max_peek` can never silently leave a protective limit at its default.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Dict, List

import yaml

__all__ = ["Config", "ConfigError", "load_config"]


class ConfigError(ValueError):
    """The config file is missing, unparsable, or has an unknown key or invalid value."""


@dataclass(frozen=True)
class Config:
    """Service settings; defaults are the proposed public limits.

    Rate limits: the page cleans a file in requests of at most 100 records / 400 KB, and the single
    worker handles about 10 of them per second (roughly 1 MB/s of MSP). A 1 GiB file is therefore
    some 2,700 to 10,000 requests; at the old 120 per minute that would take over an hour, so the
    defaults are 900 per minute sustained (15 per second, above the worker's own pace so the limiter
    is not the bottleneck) with a burst of 60. Throughput stays bounded by `max_pending`,
    `max_inflight_per_client` and `max_body_bytes`, which are unchanged.
    """
    host: str = "127.0.0.1"
    port: int = 8300
    max_peaks: int = 5000
    max_batch: int = 200
    max_body_bytes: int = 1048576
    max_label: int = 12
    max_label_lookups: int = 200
    max_request_lookups: int = 200000
    max_pending: int = 8
    max_inflight_per_client: int = 4
    max_cache_entries: int = 200000
    rate_per_minute: int = 900
    rate_burst: int = 60
    cache_clear_seconds: int = 3600
    trusted_proxy: bool = True
    cors_origins: List[str] = field(default_factory=lambda: ["*"])


def _check(name: str, value: Any, default: Any) -> Any:
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ConfigError("%s must be true or false" % name)
    elif isinstance(default, int):
        # bool is an int subclass; `port: true` is a typo, not a port.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError("%s must be an integer" % name)
        if value <= 0:
            raise ConfigError("%s must be positive, got %d" % (name, value))
        if name == "port" and value > 65535:
            raise ConfigError("port must be at most 65535, got %d" % value)
    elif isinstance(default, str):
        if not isinstance(value, str) or not value:
            raise ConfigError("%s must be a non-empty string" % name)
    else:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError("%s must be a list of strings" % name)
    return value


def load_config(path: str) -> Config:
    """Read and validate the YAML file at `path`; an empty file yields the defaults."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except OSError as exc:
        raise ConfigError("cannot read config %s: %s" % (path, exc))
    except yaml.YAMLError as exc:
        raise ConfigError("invalid YAML in %s: %s" % (path, exc))
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("config %s must be a mapping of key: value" % path)
    defaults = Config()
    known = {f.name for f in fields(Config)}
    for key in raw:
        if key not in known:
            raise ConfigError("unknown config key: %s" % key)
    values: Dict[str, Any] = {}
    for name, value in raw.items():
        values[name] = _check(name, value, getattr(defaults, name))
    return Config(**values)
