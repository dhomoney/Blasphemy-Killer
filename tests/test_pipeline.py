"""Event-sequence tests for pipeline.process.

The CLI's terminal output and the web UI are both derived from these events, so
the ordering and payloads are part of the contract.
"""

from pathlib import Path

import pytest

from blasphemy_killer.config import load_config
from blasphemy_killer.match import Word
from blasphemy_killer.pipeline import (
    Cancelled, Finished, MarkerUnsigned, MatchFound, MatchingComplete,
    Progress, Skipped, StageChanged, process,
)


@pytest.fixture
def cfg():
    config = load_config()
    config.write_report = False
    return config


def _record(events: list):
    return events.append


def _stages(events: list) -> list[str]:
    return [e.stage for e in events if isinstance(e, StageChanged)]


def _fake_transcript(monkeypatch, words: list[Word], *, progress=(0.5, 1.0)):
    """Replace transcription so tests stay fast and deterministic."""
    import blasphemy_killer.transcribe as transcribe_mod

    def fake(_wav, *, model, language, cpu_threads=0, beam_size=5,
             on_progress=None, check_cancelled=None):
        for fraction in progress:
            if check_cancelled is not None:
                check_cancelled()
            if on_progress is not None:
                on_progress(fraction)
        return words

    monkeypatch.setattr(transcribe_mod, "transcribe", fake)


def test_file_with_matches_emits_stages_matches_and_finish(
    fixture_mp4: Path, tmp_path: Path, cfg, monkeypatch
):
    target = tmp_path / "clip.mp4"
    target.write_bytes(fixture_mp4.read_bytes())
    _fake_transcript(monkeypatch, [
        Word(text="god", start=1.0, end=1.4),
        Word(text="damn", start=1.4, end=1.9),
    ])

    events: list = []
    result = process(target, cfg, dry_run=True, force=False,
                     tmp_dir=tmp_path, on_event=_record(events))

    assert _stages(events) == ["probing", "extracting", "transcribing", "matching"]
    assert [e.match.phrase for e in events if isinstance(e, MatchFound)] == ["god damn"]
    assert [e.count for e in events if isinstance(e, MatchingComplete)] == [1]
    assert [e.fraction for e in events if isinstance(e, Progress)] == [0.5, 1.0]
    assert isinstance(events[-1], Finished)
    assert result.status == "dry-run"
    assert len(result.matches) == 1


def test_file_without_matches_reports_zero(fixture_mp4: Path, tmp_path: Path, cfg, monkeypatch):
    target = tmp_path / "clip.mp4"
    target.write_bytes(fixture_mp4.read_bytes())
    _fake_transcript(monkeypatch, [Word(text="hello", start=0.0, end=0.5)])

    events: list = []
    result = process(target, cfg, dry_run=True, force=False,
                     tmp_dir=tmp_path, on_event=_record(events))

    assert not [e for e in events if isinstance(e, MatchFound)]
    assert [e.count for e in events if isinstance(e, MatchingComplete)] == [0]
    assert result.matches == []


def test_real_run_renders_and_finishes_processed(
    fixture_mp4: Path, tmp_path: Path, cfg, monkeypatch
):
    target = tmp_path / "clip.mp4"
    target.write_bytes(fixture_mp4.read_bytes())
    _fake_transcript(monkeypatch, [
        Word(text="god", start=1.0, end=1.4),
        Word(text="damn", start=1.4, end=1.9),
    ])

    events: list = []
    result = process(target, cfg, dry_run=False, force=False,
                     tmp_dir=tmp_path, on_event=_record(events))

    assert "rendering" in _stages(events)
    assert "verifying" in _stages(events)
    assert result.status == "processed"
    assert len(result.intervals) == 1


def test_already_marked_file_is_skipped_without_transcribing(
    fixture_mp4: Path, tmp_path: Path, cfg, monkeypatch
):
    target = tmp_path / "clip.mp4"
    target.write_bytes(fixture_mp4.read_bytes())
    _fake_transcript(monkeypatch, [Word(text="hello", start=0.0, end=0.5)])
    process(target, cfg, dry_run=False, force=False, tmp_dir=tmp_path)  # stamps it

    events: list = []
    result = process(target, cfg, dry_run=False, force=False,
                     tmp_dir=tmp_path, on_event=_record(events))

    assert result.status == "skipped"
    assert result.reason == "already-processed"
    assert [e.reason for e in events if isinstance(e, Skipped)] == ["already-processed"]
    assert "transcribing" not in _stages(events)


def test_unsigned_marker_is_announced_then_reprocessed(
    fixture_mp4: Path, tmp_path: Path, cfg, monkeypatch
):
    from blasphemy_killer import mute

    target = tmp_path / "clip.mp4"
    target.write_bytes(fixture_mp4.read_bytes())
    _fake_transcript(monkeypatch, [Word(text="hello", start=0.0, end=0.5)])
    process(target, cfg, dry_run=False, force=False, tmp_dir=tmp_path)

    # Same file, different machine key: the marker no longer verifies.
    monkeypatch.setattr(mute, "KEY_PATH", tmp_path / "other-machine.key")

    events: list = []
    result = process(target, cfg, dry_run=True, force=False,
                     tmp_dir=tmp_path, on_event=_record(events))

    assert any(isinstance(e, MarkerUnsigned) for e in events)
    assert "transcribing" in _stages(events)
    assert result.status == "dry-run"


def test_file_without_audio_is_skipped(tmp_path: Path, cfg):
    import subprocess

    silent = tmp_path / "video_only.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-nostdin", "-v", "error",
         "-f", "lavfi", "-i", "testsrc2=duration=1:size=160x120:rate=10",
         "-c:v", "libx264", "-preset", "ultrafast", str(silent)],
        check=True,
    )

    events: list = []
    result = process(silent, cfg, dry_run=True, force=False,
                     tmp_dir=tmp_path, on_event=_record(events))

    assert result.status == "skipped"
    assert result.reason == "no-audio"
    assert [e.reason for e in events if isinstance(e, Skipped)] == ["no-audio"]


def test_cancellation_propagates_and_leaves_the_file_alone(
    fixture_mp4: Path, tmp_path: Path, cfg, monkeypatch
):
    target = tmp_path / "clip.mp4"
    target.write_bytes(fixture_mp4.read_bytes())
    before = target.read_bytes()
    _fake_transcript(monkeypatch, [Word(text="god", start=1.0, end=1.4)])

    def cancel_now():
        raise Cancelled()

    with pytest.raises(Cancelled):
        process(target, cfg, dry_run=False, force=False, tmp_dir=tmp_path,
                check_cancelled=cancel_now)

    assert target.read_bytes() == before
    assert not list(tmp_path.glob(".*bk-tmp*"))


def _cancel_at(stage_name: str, events: list):
    """Cancel the moment a given stage is announced. Cheaper than counting
    check_cancelled calls, and it says which checkpoint is under test."""
    def check() -> None:
        current = [e.stage for e in events if isinstance(e, StageChanged)]
        if current and current[-1] == stage_name:
            raise Cancelled()
    return check


def test_a_cancel_after_the_temp_file_exists_leaves_nothing_behind(
    fixture_mp4: Path, tmp_path: Path, cfg, monkeypatch
):
    """Cancelling at the rendering checkpoint is past mkstemp. Cancelled is not
    an error, so it used to sail past the except clause that cleans up -- and
    the temp file is a dot-file, which Library.list_dir filters out, so one
    left behind is invisible in the media directory forever."""
    target = tmp_path / "clip.mp4"
    target.write_bytes(fixture_mp4.read_bytes())
    before = target.read_bytes()
    _fake_transcript(monkeypatch, [Word(text="god", start=1.0, end=1.4)])

    events: list = []
    with pytest.raises(Cancelled):
        process(target, cfg, dry_run=False, force=False, tmp_dir=tmp_path,
                on_event=_record(events), check_cancelled=_cancel_at("rendering", events))

    assert target.read_bytes() == before
    assert not list(tmp_path.glob(".*bk-tmp*"))


def test_a_cancel_during_rendering_is_honoured_before_the_swap(
    fixture_mp4: Path, tmp_path: Path, cfg, monkeypatch
):
    """Rendering is the long pass and has no checkpoints of its own, so a
    cancel raised during it can only be answered on the far side. It has to be:
    otherwise "cancelling..." ends with the file replaced anyway."""
    target = tmp_path / "clip.mp4"
    target.write_bytes(fixture_mp4.read_bytes())
    before = target.read_bytes()
    _fake_transcript(monkeypatch, [Word(text="god", start=1.0, end=1.4)])

    events: list = []
    with pytest.raises(Cancelled):
        process(target, cfg, dry_run=False, force=False, tmp_dir=tmp_path,
                on_event=_record(events), check_cancelled=_cancel_at("verifying", events))

    assert target.read_bytes() == before
    assert not list(tmp_path.glob(".*bk-tmp*"))
