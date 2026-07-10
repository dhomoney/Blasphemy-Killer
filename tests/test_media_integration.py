import shutil
import wave
from pathlib import Path

import pytest

from blasphemy_killer.match import Match, build_intervals
from blasphemy_killer.media import (
    MARKER_KEY, VerifyError, atomic_replace, extract_wav, probe, verify_output,
)
from blasphemy_killer.mute import build_filter_script, render, stamp_only

from conftest import mean_volume_db, video_md5


def test_probe_mp4(fixture_mp4: Path):
    info = probe(fixture_mp4)
    assert abs(info.duration - 10.0) < 0.5
    assert info.n_video == 1
    assert len(info.audio) == 1
    assert info.audio[0].codec == "aac"
    assert info.marker is None


def test_probe_mkv_two_tracks(fixture_mkv_2audio: Path):
    info = probe(fixture_mkv_2audio)
    assert len(info.audio) == 2
    assert info.audio[0].codec == "ac3"
    assert info.transcription_stream.index == 0


def test_extract_wav(fixture_mp4: Path, tmp_path: Path):
    out = tmp_path / "audio.wav"
    info = probe(fixture_mp4)
    extract_wav(fixture_mp4, info.audio[0], out)
    with wave.open(str(out)) as wav:
        assert wav.getframerate() == 16000
        assert wav.getnchannels() == 1


def test_build_filter_script(fixture_mkv_2audio: Path):
    info = probe(fixture_mkv_2audio)
    script = build_filter_script(info.audio, [(3.0, 4.0), (7.0, 7.5)])
    assert script == (
        "[0:a:0]volume=0:enable='between(t,3.000,4.000)+between(t,7.000,7.500)'[a0];\n"
        "[0:a:1]volume=0:enable='between(t,3.000,4.000)+between(t,7.000,7.500)'[a1]\n"
    )


def test_render_mutes_and_preserves_video(fixture_mp4: Path, tmp_path: Path):
    src = tmp_path / "movie.mp4"
    shutil.copy(fixture_mp4, src)
    md5_before = video_md5(src)

    info = probe(src)
    intervals = build_intervals(
        [Match("x", "x", 3.0, 4.0), Match("x", "x", 7.0, 7.5)],
        pad_before=0.0, pad_after=0.0,
    )
    out_tmp = src.with_name(f".{src.stem}.bk-tmp{src.suffix}")
    render(info, intervals, out_tmp, tmp_path / "filter.txt")
    verify_output(info, out_tmp)
    atomic_replace(out_tmp, src)

    # muted windows silent, untouched windows loud
    assert mean_volume_db(src, 3.2, 3.8) < -80
    assert mean_volume_db(src, 7.1, 7.4) < -80
    assert mean_volume_db(src, 1.0, 2.5) > -25
    assert mean_volume_db(src, 5.0, 6.5) > -25
    # video stream bit-exact
    assert video_md5(src) == md5_before
    # marker present -> second run would skip
    assert probe(src).marker is not None
    assert probe(src).marker.endswith(";2")


def test_render_mutes_all_tracks(fixture_mkv_2audio: Path, tmp_path: Path):
    src = tmp_path / "movie.mkv"
    shutil.copy(fixture_mkv_2audio, src)
    info = probe(src)
    out_tmp = src.with_name(f".{src.stem}.bk-tmp{src.suffix}")
    render(info, [(3.0, 4.0)], out_tmp, tmp_path / "filter.txt")
    verify_output(info, out_tmp)
    atomic_replace(out_tmp, src)

    for track in (0, 1):
        assert mean_volume_db(src, 3.2, 3.8, audio_index=track) < -80
        assert mean_volume_db(src, 1.0, 2.5, audio_index=track) > -25
    assert len(probe(src).audio) == 2
    assert probe(src).marker is not None


def test_stamp_only_marks_without_reencode(fixture_mp4: Path, tmp_path: Path):
    src = tmp_path / "clean.mp4"
    shutil.copy(fixture_mp4, src)
    info = probe(src)
    out_tmp = src.with_name(f".{src.stem}.bk-tmp{src.suffix}")
    stamp_only(info, out_tmp)
    verify_output(info, out_tmp)
    atomic_replace(out_tmp, src)
    assert probe(src).marker.endswith(";0")
    assert mean_volume_db(src, 1.0, 9.0) > -25  # audio untouched


def test_verify_rejects_truncated(fixture_mp4: Path, tmp_path: Path):
    info = probe(fixture_mp4)
    truncated = tmp_path / "trunc.mp4"
    import subprocess
    subprocess.run(
        ["ffmpeg", "-y", "-nostdin", "-v", "error", "-i", str(fixture_mp4),
         "-t", "5", "-c", "copy", str(truncated)], check=True,
    )
    with pytest.raises(VerifyError, match="duration"):
        verify_output(info, truncated)


def test_verify_rejects_empty(fixture_mp4: Path, tmp_path: Path):
    info = probe(fixture_mp4)
    empty = tmp_path / "empty.mp4"
    empty.touch()
    with pytest.raises(VerifyError):
        verify_output(info, empty)


def test_keep_backup(fixture_mp4: Path, tmp_path: Path):
    src = tmp_path / "movie.mp4"
    shutil.copy(fixture_mp4, src)
    replacement = tmp_path / "new.mp4"
    shutil.copy(fixture_mp4, replacement)
    atomic_replace(replacement, src, keep_backup=True)
    assert (tmp_path / "movie.mp4.bak").exists()
    assert src.exists()
