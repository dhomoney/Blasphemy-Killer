"""The release workflow turns a v* tag into release notes with
scripts/changelog_section.py, so a changelog edit that breaks extraction breaks
releasing. These cover the parsing and guard the shipped CHANGELOG.md."""

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

from blasphemy_killer import __version__

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "changelog_section.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("changelog_section", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


changelog_section = _load_script()

SAMPLE = """# Changelog

## 2.1.0 — a descriptor

Lead paragraph.

### Added

- Something.

## 2.0.0

Older release.
"""


def test_section_stops_at_the_next_heading():
    body, _ = changelog_section.section(SAMPLE, "2.1.0")
    assert "Lead paragraph." in body
    assert "Older release." not in body


def test_heading_descriptor_becomes_the_title():
    assert changelog_section.section(SAMPLE, "2.1.0")[1] == "a descriptor"
    assert changelog_section.section(SAMPLE, "2.0.0")[1] == ""


def test_last_section_runs_to_the_end_of_the_file():
    body, _ = changelog_section.section(SAMPLE, "2.0.0")
    assert body == "Older release."


def test_missing_version_raises():
    with pytest.raises(KeyError):
        changelog_section.section(SAMPLE, "9.9.9")


def test_cli_exits_nonzero_for_a_missing_version(tmp_path: Path):
    path = tmp_path / "CHANGELOG.md"
    path.write_text(SAMPLE)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--version", "9.9.9", "--changelog", str(path)],
        capture_output=True, text=True)
    assert proc.returncode == 1
    assert "no section for 9.9.9" in proc.stderr


def test_every_version_in_the_changelog_yields_notes():
    text = (ROOT / "CHANGELOG.md").read_text()
    versions = re.findall(r"^## (\d+\.\d+\.\d+)", text, re.MULTILINE)
    assert versions
    for version in versions:
        body, _ = changelog_section.section(text, version)
        assert body, version


def test_the_current_version_has_a_changelog_section():
    """A tag whose version has no section fails the release job. Catching it
    here means a version bump without a changelog entry fails CI first."""
    body, _ = changelog_section.section(
        (ROOT / "CHANGELOG.md").read_text(), __version__)
    assert body
