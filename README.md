# blasphemy-killer

Scans video and audio files for spoken phrases that take the Lord's name in
vain and mutes the audio during those moments — in place, safely.

How it works: the audio is transcribed locally with
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) (word-level
timestamps, CPU-only, nothing leaves your machine), a configurable phrase list
is matched against the transcript, and ffmpeg rewrites the file with the audio
silenced over each match. The video stream is copied bit-for-bit — only audio
is re-encoded.

## Requirements

- Python 3.12+
- ffmpeg / ffprobe on PATH

## Install

```bash
uv sync            # or: pip install -e .
```

`uv sync` installs the `blasphemy-killer` command into the project's virtual
environment (`.venv/`), so it isn't on your PATH by default. Run it either
way:

```bash
# Option 1: let uv handle the venv (run from the project directory)
uv run blasphemy-killer --dry-run movie.mp4

# Option 2: activate the venv, then use the command directly
source .venv/bin/activate
blasphemy-killer --dry-run movie.mp4
```

The examples below assume an activated venv; prefix them with `uv run`
otherwise.

## Usage

```bash
# Audit first: see what would be muted, change nothing
blasphemy-killer --dry-run movie.mp4

# Clean a file in place (original is verified + atomically replaced)
blasphemy-killer movie.mp4

# A whole directory, recursively
blasphemy-killer -r /media/videos

# Download with yt-dlp, then clean the download
blasphemy-killer https://youtube.com/watch?v=... -o clean-video.mp4

# Keep the original as movie.mp4.bak
blasphemy-killer --keep-backup movie.mp4

# Audio files work too (mp3, m4a, flac, ogg, opus, wav)
blasphemy-killer podcast.mp3
```

Useful flags: `-m/--model` (whisper model, default `small`; try `medium` for
mumbled dialogue), `--pad-ms` (mute padding around each phrase, default 300),
`--force` (reprocess already-cleaned files), `--list-phrases`,
`--no-report`, `-n/--dry-run`.

## Configuration

Defaults ship with the package. Override any of them in
`~/.config/blasphemy-killer/config.toml` or a file passed via `--config`:

```toml
[detection]
pad_before_ms = 300
pad_after_ms = 300
# Phrases are spelled out here only because matching requires the literal text.
# (Censored in this example; run --list-phrases to see the actual defaults.)
phrases = ["g** d***", "j**** c*****", "oh my g**"]   # replaces the default list

[transcription]
model = "small"
language = "en"
```

Run `blasphemy-killer --list-phrases` to see the effective list. Matching is
case- and space-insensitive ("g** d***" also catches the one-word and
hyphenated spellings) and anchored to word boundaries ("c*****" never matches
inside "christmas"). Note the default list includes the standalone names, so
reverent uses are muted too — trim the list if you want different behavior.

## Behavior notes

- **All audio tracks** are muted over the same intervals (alternate mixes and
  commentary tracks contain the same dialogue).
- Each cleaned file gets a `BLASPHEMY_KILLER` metadata tag so re-running over a
  directory skips it (`--force` overrides). Files with zero matches are stamped
  too, so they aren't re-transcribed. AVI/TS/WAV containers may not retain the
  tag.
- The tag is signed (HMAC) with a per-machine key auto-generated at
  `~/.config/blasphemy-killer/marker.key`, so a hostile file can't arrive
  pre-stamped to dodge cleaning. Unverifiable tags — including files stamped
  by older versions or on another machine — are reprocessed once and
  re-stamped.
- Audio-only files are re-encoded back to their original codec at the original
  bitrate (lossless formats like FLAC and WAV stay lossless).
- Safety: output is written to a temp file beside the original, checked
  (duration, stream counts, video codec), then atomically swapped in. On any
  failure the original is untouched.
- A `<file>.bk.json` sidecar records what was found and muted (`--no-report`
  to skip).
- First run downloads the whisper model (~460 MB for `small`) to
  `~/.cache/huggingface`.
