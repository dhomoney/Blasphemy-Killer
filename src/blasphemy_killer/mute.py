"""Build the ffmpeg mute render: filter script generation + command execution."""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import secrets
import subprocess
from datetime import date
from pathlib import Path

from . import __version__
from .config import USER_CONFIG_PATH
from .media import MARKER_KEY, AudioStream, MediaInfo, MediaError, arg_path

KEY_PATH = USER_CONFIG_PATH.parent / "marker.key"

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


def _marker_hmac_key() -> bytes:
    """Per-machine secret for signing done-markers, created on first use."""
    if not KEY_PATH.is_file():
        KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass  # another process won the race; use its key
        else:
            with os.fdopen(fd, "wb") as fh:
                fh.write(secrets.token_bytes(32))
    return KEY_PATH.read_bytes()


def _marker_mac(key: bytes, version: str, stamp: str, n_muted: str,
                n_video: int, n_audio: int, duration_bucket: int) -> str:
    msg = f"{version};{stamp};{n_muted};{n_video};{n_audio};{duration_bucket}"
    return hmac.new(key, msg.encode(), hashlib.sha256).hexdigest()[:16]


def marker_value(src: MediaInfo, n_muted: int) -> str:
    """version;date;n_muted;mac — the MAC binds the marker to this machine's
    key and the file's coarse shape, so a file can't arrive pre-stamped."""
    stamp = date.today().isoformat()
    mac = _marker_mac(
        _marker_hmac_key(), __version__, stamp, str(n_muted),
        src.n_video, len(src.audio), round(src.duration / 10),
    )
    return f"{__version__};{stamp};{n_muted};{mac}"


def marker_valid(info: MediaInfo) -> bool:
    """Verify a probed file's done-marker. The duration bucket is checked with
    ±1 slack: the render can drift the duration slightly, and a false negative
    only costs a re-transcription (never a wrongful skip)."""
    parts = (info.marker or "").split(";")
    if len(parts) != 4:
        return False
    version, stamp, n_muted, mac = parts
    key = _marker_hmac_key()
    bucket = round(info.duration / 10)
    return any(
        hmac.compare_digest(
            mac,
            _marker_mac(key, version, stamp, n_muted,
                        info.n_video, len(info.audio), b),
        )
        for b in (bucket - 1, bucket, bucket + 1)
    )


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
        "-metadata", f"{MARKER_KEY}={marker_value(src, n_muted)}",
    ]
    if any(name in src.container for name in ("mp4", "mov", "m4a")):
        args += ["-movflags", "use_metadata_tags"]
    return args


def render(src: MediaInfo, intervals: list[tuple[float, float]],
           out_tmp: Path, filter_script: Path) -> None:
    """Render a copy of src with audio muted over intervals; video/subs stream-copied."""
    filter_script.write_text(build_filter_script(src.audio, intervals), encoding="utf-8")

    cmd = ["ffmpeg", "-y", "-nostdin", "-v", "error", "-i", arg_path(src.path),
           "-filter_complex_script", arg_path(filter_script),
           "-map", "0:v?"]
    for stream in src.audio:
        cmd += ["-map", f"[a{stream.index}]"]
    cmd += ["-map", "0:s?", "-map", "0:t?",
            "-c:v", "copy", "-c:s", "copy", "-c:t", "copy"]
    for stream in src.audio:
        cmd += _audio_codec_args(stream)
    cmd += _metadata_args(src, len(intervals))
    cmd.append(arg_path(out_tmp))

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise MediaError(f"ffmpeg render failed: {proc.stderr.strip()}")


def stamp_only(src: MediaInfo, out_tmp: Path) -> None:
    """Metadata-only remux (-c copy) to stamp the done-marker on files with zero matches."""
    cmd = ["ffmpeg", "-y", "-nostdin", "-v", "error", "-i", arg_path(src.path),
           "-map", "0", "-c", "copy"]
    cmd += _metadata_args(src, 0)
    cmd.append(arg_path(out_tmp))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise MediaError(f"ffmpeg remux failed: {proc.stderr.strip()}")
