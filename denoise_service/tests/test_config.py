"""Tests for the strict YAML config loader."""
from pathlib import Path

import pytest

from denoise_service.config import Config, ConfigError, load_config

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.yaml"


def _write(tmp_path, text):
    p = tmp_path / "c.yaml"
    p.write_text(text)
    return str(p)


def test_load_config_defaults_and_overrides(tmp_path):
    assert load_config(_write(tmp_path, "")) == Config()
    cfg = load_config(_write(tmp_path, "port: 9000\ncors_origins: ['https://a.example']\ntrusted_proxy: false\n"))
    assert cfg.port == 9000
    assert cfg.cors_origins == ["https://a.example"]
    assert cfg.trusted_proxy is False
    assert cfg.max_peaks == 5000


def test_defaults_match_proposed_limits():
    c = Config()
    assert (c.host, c.port) == ("127.0.0.1", 8300)
    assert (c.max_peaks, c.max_batch, c.max_body_bytes) == (5000, 200, 1048576)
    assert (c.max_label, c.max_label_lookups) == (12, 200)
    assert (c.max_request_lookups, c.max_pending) == (1000000, 8)
    assert (c.rate_per_minute, c.rate_burst, c.cache_clear_seconds) == (120, 30, 3600)
    assert c.trusted_proxy is True and c.cors_origins == ["*"]


def test_example_yaml_equals_defaults():
    assert load_config(str(EXAMPLE)) == Config()


def test_example_yaml_documents_every_key():
    text = EXAMPLE.read_text()
    for name in Config.__dataclass_fields__:
        assert "\n%s:" % name in "\n" + text, name


def test_load_config_rejects_unknown_key(tmp_path):
    with pytest.raises(ConfigError, match="max_peek"):
        load_config(_write(tmp_path, "max_peek: 3\n"))


@pytest.mark.parametrize("text,key", [
    ("port: '80'\n", "port"),
    ("port: true\n", "port"),
    ("max_peaks: 1.5\n", "max_peaks"),
    ("trusted_proxy: 1\n", "trusted_proxy"),
    ("host: 5\n", "host"),
    ("cors_origins: '*'\n", "cors_origins"),
    ("cors_origins: [1]\n", "cors_origins"),
    ("max_peaks: 0\n", "max_peaks"),
    ("rate_burst: -1\n", "rate_burst"),
    ("cache_clear_seconds: 0\n", "cache_clear_seconds"),
    ("port: 70000\n", "port"),
])
def test_load_config_rejects_wrong_type_and_nonpositive(tmp_path, text, key):
    with pytest.raises(ConfigError, match=key):
        load_config(_write(tmp_path, text))


def test_load_config_rejects_non_mapping_and_bad_yaml(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "- a\n- b\n"))
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "a: [\n"))
    with pytest.raises(ConfigError):
        load_config(str(tmp_path / "missing.yaml"))


def test_config_error_is_value_error():
    assert issubclass(ConfigError, ValueError)
