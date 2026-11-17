"""CLI progress: a status line on stderr, never on stdout."""

from pathlib import Path

import pytest
from click.testing import CliRunner

from blasphemy_killer import cli
from blasphemy_killer.cli import StatusLine
from blasphemy_killer.pipeline import (
    Finished, MatchingComplete, Progress, Result, StageChanged,
)

# A whole run's worth of events, as process() emits them.
EVENTS = [
    StageChanged("probing"), Progress(0.0, "probing", 0.0),
    StageChanged("extracting"), Progress(0.0, "extracting", 0.01),
    Progress(1.0, "extracting", 0.10),
    StageChanged("transcribing"), Progress(0.0, "transcribing", 0.10),
    *[Progress(f / 10, "transcribing", 0.10 + 0.7 * f / 10) for f in range(1, 11)],
    StageChanged("matching"), Progress(0.0, "matching", 0.80),
    MatchingComplete(0),
    StageChanged("rendering"), Progress(0.0, "rendering", 0.81),
    Progress(1.0, "rendering", 0.99),
    Progress(1.0, "rendering", 1.0),
]


@pytest.fixture
def fake_process(monkeypatch):
    def process(path, cfg, *, dry_run, force, tmp_dir, on_event=None, check_cancelled=None):
        result = Result(status="processed")
        for event in EVENTS + [Finished(result)]:
            on_event(event)
        return result

    monkeypatch.setattr(cli, "process", process)


@pytest.fixture
def media(tmp_path: Path) -> list[str]:
    files = []
    for name in ("a.mp4", "b.mp4"):
        (tmp_path / name).write_bytes(b"x")
        files.append(str(tmp_path / name))
    return files


def test_plain_progress_lines_go_to_stderr(fake_process, media):
    res = CliRunner().invoke(cli.main, media[:1])
    assert res.exit_code == 0, res.output
    lines = [line for line in res.stderr.splitlines() if "overall" in line]
    assert lines[0] == "  extracting 100% (overall 10%)"
    assert lines[-1] == "  rendering 100% (overall 100%)"
    assert 5 <= len(lines) <= 11            # one per 10%, not one per event
    assert "overall" not in res.stdout and "\r" not in res.stdout
    assert "no matches found" in res.stdout
    assert "\r" not in res.stderr           # not a terminal: no in-place line


def test_several_files_show_the_run_figure(fake_process, media):
    res = CliRunner().invoke(cli.main, media)
    assert res.exit_code == 0, res.output
    assert "  rendering 100% (file 100%, run 50%)" in res.stderr
    assert "  rendering 100% (file 100%, run 100%)" in res.stderr


def test_no_progress_prints_none(fake_process, media):
    res = CliRunner().invoke(cli.main, ["--no-progress", *media])
    assert res.exit_code == 0, res.output
    assert "%" not in res.stderr
    assert "2 processed, 0 skipped, 0 failed" in res.stdout


def test_verbose_prints_each_stage(fake_process, media):
    res = CliRunner().invoke(cli.main, ["-v", "--no-progress", media[0]])
    assert "  stage: transcribing" in res.stdout
    assert "  stage: rendering" in res.stdout


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_terminal_line_is_rewritten_in_place_and_throttled(capsys):
    clock = Clock()
    status = StatusLine(enabled=True, tty=True, total_files=12, clock=clock)
    status.start_file(2, "movie.mkv")
    status.update("transcribing", 0.58, 0.41)
    status.update("transcribing", 0.59, 0.42)          # too soon: not drawn
    clock.now = 1.0
    status.update("transcribing", 0.60, 0.43)
    status.echo("  [00:01:02.000 - 00:01:03.000] match")
    err = capsys.readouterr()
    assert err.err.count("\r[3/12]") == 2
    assert "\r[3/12] 20% movie.mkv - transcribing 58%" in err.err
    assert "59%" not in err.err
    assert err.out == "  [00:01:02.000 - 00:01:03.000] match\n"
    # The line was wiped (spaces, then back to column 0) before the match.
    assert err.err.endswith("\r" + " " * len("[3/12] 20% movie.mkv - transcribing 60%") + "\r")


def test_terminal_line_shows_model_loading_as_text(capsys):
    status = StatusLine(enabled=True, tty=True)
    status.start_file(0, "clip.mp4")
    status.update("loading-model", 0.0, 0.10)
    assert capsys.readouterr().err == "\r10% clip.mp4 - loading model..."


def test_plain_lines_repeat_after_thirty_seconds(capsys):
    clock = Clock()
    status = StatusLine(enabled=True, tty=False, clock=clock)
    status.update("transcribing", 0.01, 0.11)           # crosses 10%
    status.update("transcribing", 0.02, 0.12)           # same bucket, too soon
    clock.now = 31.0
    status.update("transcribing", 0.03, 0.13)           # 30 s on, text changed
    assert capsys.readouterr().err.splitlines() == [
        "  transcribing 1% (overall 11%)",
        "  transcribing 3% (overall 13%)",
    ]


def test_plain_lines_never_repeat_the_same_text(capsys):
    # From the owner's test run: the 30 s line and the next 10% step printed
    # "transcribing 43% (file 40%, run 20%)" twice in a row.
    clock = Clock()
    status = StatusLine(enabled=True, tty=False, total_files=2, clock=clock)
    status.start_file(0, "a.mp4")
    clock.now = 31.0
    status.update("transcribing", 0.43, 0.399)          # 30 s on: printed
    status.update("transcribing", 0.43, 0.401)          # crosses 40%, same text
    status.start_file(1, "b.mp4")                       # new file: may repeat
    status.update("transcribing", 0.43, 0.401)
    assert capsys.readouterr().err.splitlines() == [
        "  transcribing 43% (file 40%, run 20%)",
        "  transcribing 43% (file 40%, run 70%)",
    ]
