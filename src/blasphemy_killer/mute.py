"""Build the ffmpeg mute render: filter script generation + command execution."""

from __future__ import annotations

import math
import subprocess
from datetime import date
from pathlib import Path

from . import __version__
from .media import MARKER_KEY, AudioStream, MediaInfo, MediaError

# Source codec -> ffmpeg encoder, for codecs we re-encode back to the same family.
_ENCODERS = {
    "aac": "aac",
    "ac3": "ac3",
    "eac3": "eac3",
    "mp3": "libmp3lame",
    "flac": "flac",
    "opus": "libopus",
    "vorbis": "libvorbis",
    "pcm_s16le": "pcm_s16le",
}
_LOSSLESS = {"flac", "pcm_s16le"}


def build_filter_script(streams: list[AudioStream], intervals: list[tuple[float, float]]) -> str:
    """One volume=0 chain per audio stream, enabled over every mute interval."""
    expr = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in intervals)
    chains = [
        f"[0:a:{stream.index}]volume=0:enable='{expr}'[a{stream.index}]"
        for stream in streams
    ]
    return ";\n".join(chains) + "\n"


def marker_value(n_muted: int) -> str:
    return f"{__version__};{date.today().isoformat()};{n_muted}"


def _audio_codec_args(stream: AudioStream) -> list[str]:
    encoder = _ENCODERS.get(stream.codec, "aac")
    args = [f"-c:a:{stream.index}", encoder]
    if stream.codec not in _LOSSLESS:
        bitrate = stream.bitrate or 128_000 * math.ceil(stream.channels / 2)
        args += [f"-b:a:{stream.index}", str(bitrate)]
    args += [f"-ac:a:{stream.index}", str(stream.channels)]
    return args


def _metadata_args(src: MediaInfo, n_muted: int) -> list[str]:
    args = [
        "-map_metadata", "0", "-map_chapters", "0",
        "-metadata", f"{MARKER_KEY}={marker_value(n_muted)}",
    ]
    if any(name in src.container for name in ("mp4", "mov", "m4a")):
        args += ["-movflags", "use_metadata_tags"]
    return args


def render(src: MediaInfo, intervals: list[tuple[float, float]],
           out_tmp: Path, filter_script: Path) -> None:
    """Render a copy of src with audio muted over intervals; video/subs stream-copied."""
    filter_script.write_text(build_filter_script(src.audio, intervals), encoding="utf-8")

    cmd = ["ffmpeg", "-y", "-nostdin", "-v", "error", "-i", str(src.path),
           "-filter_complex_script", str(filter_script),
           "-map", "0:v?"]
    for stream in src.audio:
        cmd += ["-map", f"[a{stream.index}]"]
    cmd += ["-map", "0:s?", "-map", "0:t?",
            "-c:v", "copy", "-c:s", "copy", "-c:t", "copy"]
    for stream in src.audio:
        cmd += _audio_codec_args(stream)
    cmd += _metadata_args(src, len(intervals))
    cmd.append(str(out_tmp))

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise MediaError(f"ffmpeg render failed: {proc.stderr.strip()}")


def stamp_only(src: MediaInfo, out_tmp: Path) -> None:
    """Metadata-only remux (-c copy) to stamp the done-marker on files with zero matches."""
    cmd = ["ffmpeg", "-y", "-nostdin", "-v", "error", "-i", str(src.path),
           "-map", "0", "-c", "copy"]
    cmd += _metadata_args(src, 0)
    cmd.append(str(out_tmp))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise MediaError(f"ffmpeg remux failed: {proc.stderr.strip()}")
