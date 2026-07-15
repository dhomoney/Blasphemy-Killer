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
