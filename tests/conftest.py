import subprocess
from pathlib import Path

import pytest


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-nostdin", "-v", "error", *args], check=True)


@pytest.fixture(scope="session")
def fixture_mp4(tmp_path_factory) -> Path:
    """10s test video: testsrc2 + 440Hz sine, one AAC audio track."""
    path = tmp_path_factory.mktemp("media") / "fixture.mp4"
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc2=duration=10:size=320x240:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-b:a", "128k",
        str(path),
    )
    return path


@pytest.fixture(scope="session")
def fixture_mkv_2audio(tmp_path_factory) -> Path:
    """10s MKV with two audio tracks (440Hz and 880Hz sines)."""
    path = tmp_path_factory.mktemp("media") / "fixture2.mkv"
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc2=duration=10:size=320x240:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
        "-f", "lavfi", "-i", "sine=frequency=880:duration=10",
        "-map", "0:v", "-map", "1:a", "-map", "2:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "ac3", "-b:a", "192k",
        "-disposition:a:0", "default",
        str(path),
    )
    return path


def mean_volume_db(path: Path, start: float, end: float, audio_index: int = 0) -> float:
    """Mean volume (dB) of a window of one audio stream, via ffmpeg volumedetect."""
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-ss", str(start), "-to", str(end), "-i", str(path),
         "-map", f"0:a:{audio_index}",
         "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    for line in proc.stderr.splitlines():
        if "mean_volume" in line:
            return float(line.split("mean_volume:")[1].strip().split(" ")[0])
    raise AssertionError(f"no volumedetect output:\n{proc.stderr}")


def video_md5(path: Path) -> str:
    """MD5 of the video stream packets (detects any re-encode)."""
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
         "-map", "0:v:0", "-c", "copy", "-f", "md5", "-"],
        capture_output=True, text=True, check=True,
    )
    return proc.stdout.strip()
