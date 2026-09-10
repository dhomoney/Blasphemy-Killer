from pathlib import Path

import pytest

from blasphemy_killer import config as config_mod
from blasphemy_killer.config import load_config


@pytest.fixture(autouse=True)
def no_user_config(monkeypatch, tmp_path):
    monkeypatch.setattr(config_mod, "USER_CONFIG_PATH", tmp_path / "nonexistent.toml")


def test_defaults_load():
    cfg = load_config()
    assert "god damn" in cfg.phrases
    assert cfg.model == "small"
    assert cfg.pad_before == 0.3
    assert ".mkv" in cfg.extensions


def test_user_config_overrides(tmp_path: Path):
    override = tmp_path / "config.toml"
    override.write_text(
        '[detection]\nphrases = ["zounds"]\npad_before_ms = 500\n'
        '[transcription]\nmodel = "medium"\n'
    )
    cfg = load_config(override)
    assert cfg.phrases == ["zounds"]
    assert cfg.pad_before_ms == 500
    assert cfg.model == "medium"
    # untouched sections keep defaults
    assert cfg.write_report is True


def test_cookies_default_unset():
    assert load_config().cookies is None


def test_cookies_from_config(tmp_path: Path):
    override = tmp_path / "config.toml"
    override.write_text('[download]\ncookies = "~/cookies.txt"\n')
    cfg = load_config(override)
    assert cfg.cookies == Path.home() / "cookies.txt"


def test_config_dir_defaults_to_home(monkeypatch):
    monkeypatch.delenv("BK_CONFIG_DIR", raising=False)
    assert config_mod._config_dir() == Path.home() / ".config" / "blasphemy-killer"


def test_config_dir_env_override(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("BK_CONFIG_DIR", str(tmp_path / "cfg"))
    assert config_mod._config_dir() == tmp_path / "cfg"
