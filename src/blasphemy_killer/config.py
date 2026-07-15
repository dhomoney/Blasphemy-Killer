"""Configuration loading: shipped defaults merged with an optional user config."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

USER_CONFIG_PATH = Path.home() / ".config" / "blasphemy-killer" / "config.toml"


@dataclass
class Config:
    phrases: list[str] = field(default_factory=list)
    pad_before_ms: int = 300
    pad_after_ms: int = 300
    model: str = "small"
    language: str = "en"
    beam_size: int = 5
    cpu_threads: int = 0
    extensions: list[str] = field(default_factory=list)
    write_report: bool = True
    keep_backup: bool = False

    @property
    def pad_before(self) -> float:
        return self.pad_before_ms / 1000.0

    @property
    def pad_after(self) -> float:
        return self.pad_after_ms / 1000.0


def _default_toml() -> dict:
    ref = resources.files("blasphemy_killer.data") / "default_config.toml"
    return tomllib.loads(ref.read_text(encoding="utf-8"))


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(config_path: Path | None = None) -> Config:
    """Load shipped defaults, then merge the user config and an explicit --config file."""
    data = _default_toml()
    for path in (USER_CONFIG_PATH, config_path):
        if path is not None and path.is_file():
            data = _merge(data, tomllib.loads(path.read_text(encoding="utf-8")))

    detection = data.get("detection", {})
    transcription = data.get("transcription", {})
    processing = data.get("processing", {})

    return Config(
        phrases=list(detection.get("phrases", [])),
        pad_before_ms=int(detection.get("pad_before_ms", 300)),
        pad_after_ms=int(detection.get("pad_after_ms", 300)),
        model=str(transcription.get("model", "small")),
        language=str(transcription.get("language", "en")),
        beam_size=int(transcription.get("beam_size", 5)),
        cpu_threads=int(transcription.get("cpu_threads", 0)),
        extensions=[e.lower() for e in processing.get("extensions", [])],
        write_report=bool(processing.get("write_report", True)),
        keep_backup=bool(processing.get("keep_backup", False)),
    )
