"""ffprobe/ffmpeg helpers: probing, audio extraction, verification, atomic replace."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

MARKER_KEY = "BLASPHEMY_KILLER"


class MediaError(Exception):
    pass


class VerifyError(MediaError):
    pass


@dataclass
class AudioStream:
    index: int  # index among audio streams (0-based), i.e. the n in 0:a:n
    codec: str
    channels: int
    bitrate: int | None
    default: bool


@dataclass
class MediaInfo:
    path: Path
    duration: float
    container: str  # ffprobe format_name, e.g. "matroska,webm"
    audio: list[AudioStream] = field(default_factory=list)
    n_video: int = 0
    n_subtitle: int = 0
    video_codecs: list[str] = field(default_factory=list)
    marker: str | None = None

    @property
    def transcription_stream(self) -> AudioStream | None:
        """The default-disposition audio stream, falling back to the first one."""
        for s in self.audio:
            if s.default:
                return s
        return self.audio[0] if self.audio else None


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def probe(path: Path) -> MediaInfo:
    proc = _run([
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ])
    if proc.returncode != 0:
        raise MediaError(f"ffprobe failed for {path}: {proc.stderr.strip()}")
    data = json.loads(proc.stdout)

    fmt = data.get("format", {})
    tags = {k.upper(): v for k, v in fmt.get("tags", {}).items()}

    audio: list[AudioStream] = []
    n_video = 0
    n_subtitle = 0
    video_codecs: list[str] = []
    for stream in data.get("streams", []):
        kind = stream.get("codec_type")
        if kind == "audio":
            bitrate = stream.get("bit_rate")
            audio.append(AudioStream(
                index=len(audio),
                codec=stream.get("codec_name", ""),
                channels=int(stream.get("channels", 2)),
                bitrate=int(bitrate) if bitrate else None,
                default=bool(stream.get("disposition", {}).get("default", 0)),
            ))
        elif kind == "video":
            # Attached cover art shows up as a video stream; don't count it.
            if not stream.get("disposition", {}).get("attached_pic", 0):
                n_video += 1
                video_codecs.append(stream.get("codec_name", ""))
        elif kind == "subtitle":
            n_subtitle += 1

    return MediaInfo(
        path=path,
        duration=float(fmt.get("duration", 0.0)),
        container=fmt.get("format_name", ""),
        audio=audio,
        n_video=n_video,
        n_subtitle=n_subtitle,
        video_codecs=video_codecs,
        marker=tags.get(MARKER_KEY),
    )


def extract_wav(path: Path, stream: AudioStream, out: Path) -> None:
    """Extract one audio stream as mono 16 kHz PCM WAV (what whisper wants)."""
    proc = _run([
        "ffmpeg", "-y", "-nostdin", "-v", "error",
        "-i", str(path),
        "-map", f"0:a:{stream.index}",
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(out),
    ])
    if proc.returncode != 0:
        raise MediaError(f"audio extraction failed for {path}: {proc.stderr.strip()}")


def verify_output(original: MediaInfo, candidate: Path) -> MediaInfo:
    """Check the rendered temp file before it replaces the original."""
    if not candidate.is_file() or candidate.stat().st_size == 0:
        raise VerifyError("output file missing or empty")
    try:
        info = probe(candidate)
    except MediaError as exc:
        raise VerifyError(f"output not probeable: {exc}") from exc

    tolerance = max(0.5, original.duration * 0.005)
    if abs(info.duration - original.duration) > tolerance:
        raise VerifyError(
            f"duration mismatch: {info.duration:.2f}s vs {original.duration:.2f}s"
        )
    if info.n_video != original.n_video:
        raise VerifyError(f"video stream count changed: {info.n_video} vs {original.n_video}")
    if len(info.audio) != len(original.audio):
        raise VerifyError(f"audio stream count changed: {len(info.audio)} vs {len(original.audio)}")
    if info.n_subtitle != original.n_subtitle:
        raise VerifyError(f"subtitle stream count changed: {info.n_subtitle} vs {original.n_subtitle}")
    if info.video_codecs != original.video_codecs:
        raise VerifyError(f"video codec changed: {info.video_codecs} vs {original.video_codecs}")
    return info


def atomic_replace(tmp: Path, original: Path, *, keep_backup: bool = False) -> None:
    """Atomically replace original with tmp (same directory, same filesystem)."""
    if keep_backup:
        backup = original.with_name(original.name + ".bak")
        os.replace(original, backup)
    os.replace(tmp, original)
