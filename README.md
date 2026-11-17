# blasphemy-killer

Scans video and audio files for spoken phrases that take the Lord's name in
vain and mutes the audio during those moments — in place, safely.

How it works: the audio is transcribed locally with
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) (word-level
timestamps, CPU-only, nothing leaves your machine), a configurable phrase list
is matched against the transcript, and ffmpeg rewrites the file with the audio
silenced over each match. The video stream is copied bit-for-bit — only audio
is re-encoded.

## Quick start (Docker)

The image bundles ffmpeg, ffprobe, deno and every Python dependency, so Docker
is the only prerequisite.

```bash
docker run --rm \
  -e PUID=$(id -u) -e PGID=$(id -g) \
  -v "$PWD:/media" \
  -v bk-config:/config \
  -v bk-cache:/cache \
  dhomoney/blasphemy-killer:2 --dry-run /media/movie.mp4
```

That's a mouthful to retype, so it's worth a shell function:

```bash
bk() {
  docker run --rm -e PUID=$(id -u) -e PGID=$(id -g) \
    -v "$PWD:/media" -v bk-config:/config -v bk-cache:/cache \
    dhomoney/blasphemy-killer:2 "$@"
}

bk --dry-run /media/movie.mp4     # audit
bk /media/movie.mp4               # clean it
bk -r /media                      # a whole library
```

Or with the bundled `docker-compose.yml`:

```bash
docker compose run --rm blasphemy-killer -r /media
```

### The volumes

| Mount | What it holds | If you skip it |
|---|---|---|
| `/media` | Your files, bind-mounted from the host | Nothing to process |
| `/config` | `config.toml`, `marker.key`, any uploaded `cookies.txt`, and cleans promised but not yet run | **Every file gets re-transcribed on every run** |
| `/cache` | The whisper model (~460 MB on first run) | Re-downloaded every run |

The web UI uses exactly the same three volumes.

`/config` deserves the emphasis. `marker.key` is the per-machine secret that
signs the "already cleaned" tag on each file; without a persistent `/config`
a fresh key is generated on every `docker run`, no existing tag verifies, and
your whole library is transcribed and re-encoded again from scratch. Use a
named volume and leave it alone.

### Three things to get right

- **Mount the directory, not the file.** `-v "$PWD/movie.mp4:/media/movie.mp4"`
  will not work. Cleaning writes a temp file next to the original and renames it
  over the top, which a single-file bind mount cannot support. Mount the parent
  directory.
- **Set `PUID`/`PGID`** (or `--user "$(id -u):$(id -g)"`) so cleaned files stay
  owned by you instead of root. The container drops to that uid before it
  touches any media.
- **On SELinux hosts** (Fedora, RHEL) append `:z` to the media mount:
  `-v "$PWD:/media:z"`.
- **On Windows** (Docker Desktop, PowerShell) `$(id -u)` is bash syntax: drop
  the `PUID`/`PGID` flags and write the mount as `-v "${PWD}:/media"`. The
  `bk` shell function is bash too, so define it in Git Bash or WSL, or use
  `docker compose run --rm blasphemy-killer …`.

### Other useful flags

```bash
# Fully offline once the model is cached — no network at all
docker run --rm --network none ... dhomoney/blasphemy-killer:2 /media/movie.mp4

# Cap CPU use; transcription sizes its thread pool from the cgroup quota
docker run --rm --cpus 4 ... dhomoney/blasphemy-killer:2 -r /media
```

`--network none` also disables URL downloads and the first-run model fetch, so
warm the cache volume once before using it.

## Web UI

If you would rather not compose `docker run` lines, the same image serves a
small web UI: browse your library, see what has already been cleaned, queue
files, and watch progress and matches arrive live.

```bash
docker compose up web        # then open http://127.0.0.1:8080
```

or directly:

```bash
docker run --rm \
  -e PUID=$(id -u) -e PGID=$(id -g) \
  -p 127.0.0.1:8080:8080 \
  -v "$PWD:/media" -v bk-config:/config -v bk-cache:/cache \
  dhomoney/blasphemy-killer:2 serve
```

Dry run is the default for files you queue from the listing, and cleaning them
for real takes an explicit confirmation. A URL you paste is the exception: it
was a request for a cleaned file, so it is cleaned (see
[Pasting a URL](#pasting-a-url)).
Each running job shows a live progress bar for the whole clean, with the
current stage alongside ("rendering 40% · 88% overall"). A running job can be
cancelled (it stops at the next transcription checkpoint, usually a second or
two, or just before the original would be replaced).

### Choosing a whisper model

The toolbar's **Model** dropdown picks the whisper model for the next jobs you
queue — and for the clean that follows a URL download — from `tiny` (fastest,
least accurate) up to `large-v3`. It defaults to the `model` set in
`config.toml` (`small`), which remains the way to change the default itself.
A job's finished row notes the model when it differs from the default, and a
first use of any model downloads it once to the cache volume (the `small`
default is ~460 MB).

### Pasting a URL

The bar above the listing takes a video URL and hands it to yt-dlp, the same
way the CLI does. The download lands in **the directory you are currently
browsing** and shows its progress in the queue.

When it lands it is cleaned, without being asked twice: the file goes straight
into a clean-in-place job — transcribed, matched and muted, exactly as the CLI
does for a URL — and the player opens on the cleaned file with its matches
already on the timeline. That follow-up is a queue entry of its own, so it can
be watched and cancelled like any other job. Untick **Clean it when it lands**
beside the URL bar to download and stop there; the file can still be queued
from the listing afterwards.

That promise outlives the server, too. It is written to the config volume when
it is made, so a restart between the download landing and the clean running
re-queues it at startup instead of quietly dropping it — the file would
otherwise sit there looking merely "not scanned". Downloads themselves are not
resumed.

Cancelling a download discards the partial file rather than leaving it in your
library under a `.part` extension you would never see, and nothing is cleaned
if the download never finished.

Age-gated, members-only or private videos need cookies, and the row under the
URL bar uploads a `cookies.txt` for them. The browser reads the file and sends
its **contents**, never a path — an unauthenticated page that opened
server-side paths would be a way to read any file on the host. What arrives is
checked for being a real Netscape cookies.txt (a JSON export, the usual
mistake, is refused with an explanation rather than failing three minutes into
a download) and written to `cookies.txt` in the config directory, mode `600`
(on Windows, file permissions come from the folder's ACL instead).

The UI shows how many cookies are in place and can remove them again. It never
sends them back to the browser. An uploaded file takes precedence over a
`cookies` path set in `config.toml`; a file named there shows up too, but the
UI will not offer to delete something it did not put there. See
[Cookies for URL downloads](#cookies-for-url-downloads) for the CLI side.

### Playing and scrubbing

Clicking a filename opens it in a player. Seeking is the browser's own, backed
by range requests, so scrubbing through a long file does not download it first.
Once a file has been scanned, its matches appear as marks on a strip under the
player and as a list beside it; clicking either jumps to a second or so before
that moment, which is the quickest way to hear what the scan found and check a
mute landed where it should.

Playback depends on what your browser can decode. Downloads are merged to mp4
and play everywhere; `.mkv` and `.avi` files in your library often will not
play natively even though they are served correctly.

### It has no authentication — keep it on your own machine

Publishing as `127.0.0.1:8080:8080` (what the compose file does) means only
this machine can reach it. Publishing as `-p 8080:8080` means `0.0.0.0`, which
hands **everyone on your network** the ability to overwrite your media library.
There is no login. For remote access, put an authenticating reverse proxy in
front of it.

One wrinkle worth knowing: the server binds `0.0.0.0` *inside* the container,
and that is not an exposure decision. A container's loopback is its own, so a
server bound to `127.0.0.1` in there would be unreachable through any published
port. The host-side publish address is what actually limits access.

Running natively, `--host` defaults to `127.0.0.1`, which is IPv4-only — open
the `http://127.0.0.1:8080` URL it prints rather than `localhost`, which some
browsers resolve to `::1` first and which would refuse the connection.

The UI covers browsing, downloading, playback, queueing and per-job model
selection. The phrase list and the rest of the settings stay in `config.toml` —
see [Configuration](#configuration).

## Usage

Flags are the same whether you run the image or a native install.

```bash
# Audit first: see what would be muted, change nothing
blasphemy-killer --dry-run movie.mp4

# Clean a file in place (original is verified + atomically replaced)
blasphemy-killer movie.mp4

# A whole directory, recursively
blasphemy-killer -r /media/videos

# Download with yt-dlp, then clean the download
blasphemy-killer https://youtube.com/watch?v=... -o clean-video.mp4

# Keep the original as movie.mp4.bak (only when something was muted)
blasphemy-killer --keep-backup movie.mp4

# Audio files work too (mp3, m4a, flac, ogg, opus, wav)
blasphemy-killer podcast.mp3
```

Useful flags: `-m/--model` (whisper model, default `small`; try `medium` for
mumbled dialogue), `--pad-ms` (mute padding around each phrase, default 300),
`--force` (reprocess already-cleaned files), `--list-phrases`,
`--no-report`, `-n/--dry-run`, `--cookies`, `--no-progress`, `-v/--verbose`
(print each stage as it starts), `--language` (`auto` to detect), `--threads`,
`--center-channel/--no-center-channel`, `--vad/--no-vad`, `--vad-threshold`,
`-c/--config`. `blasphemy-killer --help` lists them all.

### Progress

Progress goes to stderr. stdout carries the file names, matches and results,
as before, plus yt-dlp's own lines while a URL downloads (its progress bar is
off while the status line is on). On a terminal it is one line that updates
in place:

```
[3/12] 21% movie.mkv - transcribing 58%
```

The leading figure is the whole run: with one file it is that file's overall
progress; with several, `[3/12]` is added and the figure is finished files plus
this one's share, over the file count. Anywhere else (a pipe, cron,
`docker logs`) it is a plain line each time the file passes another 10% (and
at most every 30 s otherwise, if anything changed):

```
  transcribing 60% (overall 52%)              # one file
  transcribing 60% (file 52%, run 21%)        # several files
```

The overall figure weighs the stages by their usual cost: transcription about
70%, the render about 18%, the audio extraction about 9% (a dry run has no
render, so there transcription is most of the bar). Loading the whisper
model (a download, the first time) has no measurable length and shows as
`loading model...`. With voice-activity detection on, transcription moves in
media time, so it jumps over long silences. `--no-progress` turns it off.

### Cookies for URL downloads

Videos that need a logged-in session (age-gated, members-only, private, or
region-locked) download only if yt-dlp gets your cookies. Export them in
Netscape format — e.g. with the "Get cookies.txt LOCALLY" browser extension —
and point `--cookies` at the file.

With Docker, put the file in the config volume so yt-dlp can also write the
refreshed cookies back:

```bash
bk --cookies /config/cookies.txt https://youtube.com/watch?v=... -o /media/clean.mp4
```

Natively:

```bash
blasphemy-killer --cookies ~/cookies.txt https://youtube.com/watch?v=...
```

To avoid passing it every time, set it in your config instead:

```toml
[download]
cookies = "~/cookies.txt"
```

`--cookies` overrides the config value. The web UI uploads to `cookies.txt` in
the config directory, and a file uploaded there wins over both for downloads
started from the UI. Note that yt-dlp may rewrite the file
with refreshed cookies after a download, and that the file grants access to
your logged-in accounts — keep it out of version control (this repo's
`.gitignore` and `.dockerignore` both exclude `cookies.txt`) and readable only
by you (`chmod 600`; on Windows, keep it in your user profile).

## Configuration

Defaults ship with the package. Override any of them in
`~/.config/blasphemy-killer/config.toml` (on Windows
`%USERPROFILE%\.config\blasphemy-killer\config.toml`) — `/config/config.toml`
inside the image — or in a file passed via `--config`:

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
center_channel = true   # 5.1/7.1: transcribe the dialogue (center) channel, not a flat downmix
vad_filter = true       # set false to transcribe everything, including speech under loud music
vad_threshold = 0.35    # lower = keeps quieter speech (more recall, a bit more noise)
```

The shipped defaults (`src/blasphemy_killer/data/default_config.toml`) also
cover `beam_size`, `cpu_threads` (0 = auto) and a `[processing]` table:
`extensions` (which files a directory scan picks up), `write_report` and
`keep_backup`.

Run `blasphemy-killer --list-phrases` to see the effective list. Matching is
case- and space-insensitive ("g** d***" also catches the one-word and
hyphenated spellings) and anchored to word boundaries ("c*****" never matches
inside "christmas"). Note the default list includes the standalone names, so
reverent uses are muted too — trim the list if you want different behavior.

`BK_CONFIG_DIR` overrides where `config.toml` and `marker.key` live. The image
sets it to `/config`.

## Running natively

Still fully supported, and the right choice if you'd rather not have the ~1.3 GB
image around.

### Requirements

- Python 3.12+
- ffmpeg / ffprobe on PATH
- [deno](https://deno.com) on PATH — only needed for downloading URLs.
  YouTube extraction requires a JavaScript runtime
  ([yt-dlp EJS](https://github.com/yt-dlp/yt-dlp/wiki/EJS)); without one,
  yt-dlp warns `No supported JavaScript runtime could be found` and some
  formats may be missing. Deno is yt-dlp's default runtime and runs the
  extraction JS fully sandboxed. Install it user-locally with:

  ```bash
  curl -fsSL https://deno.land/install.sh | sh
  ```

  or grab the release zip from [denoland/deno](https://github.com/denoland/deno/releases)
  and put the `deno` binary somewhere on PATH (e.g. `~/.local/bin`). node, bun
  and quickjs also work if you already have one.

### Install

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

First run downloads the whisper model (~460 MB for `small`) to
`~/.cache/huggingface`.

On Windows (PowerShell): `.venv\Scripts\Activate.ps1`, then
`blasphemy-killer --dry-run movie.mp4`. The config directory is
`%USERPROFILE%\.config\blasphemy-killer` (same layout as Linux; `BK_CONFIG_DIR`
overrides it), and the model cache is `%USERPROFILE%\.cache\huggingface`.

The web UI works natively too:

```bash
uv run blasphemy-killer-serve --media-root ~/Videos
```

Natively, `--media-root` defaults to `/media`, so pass your library
(`--media-root ~/Videos`, on Windows e.g. `--media-root D:\Videos`).
`BK_MEDIA_ROOT`, `BK_WEB_HOST` and `BK_WEB_PORT` set the same defaults from
the environment.

## Behavior notes

- **All audio tracks** are muted over the same intervals (alternate mixes and
  commentary tracks contain the same dialogue).
- Each cleaned file gets a `BLASPHEMY_KILLER` metadata tag so re-running over a
  directory skips it (`--force` overrides). Files with zero matches are stamped
  too, so they aren't re-transcribed. AVI/TS/WAV containers may not retain the
  tag.
- The tag is signed (HMAC) with a per-machine key auto-generated at
  `marker.key` in the config directory, so a hostile file can't arrive
  pre-stamped to dodge cleaning. Unverifiable tags — including files stamped
  by older versions or on another machine — are reprocessed once and
  re-stamped. Moving between a native install and the image counts as another
  machine unless you copy `marker.key` across.
- Audio-only files are re-encoded back to their original codec at the original
  bitrate (lossless formats like FLAC and WAV stay lossless).
- Safety: output is written to a temp file beside the original, checked
  (duration, stream counts, video codec), then atomically swapped in. On any
  failure the original is untouched. On Windows, close the file in any player
  first: a file that is open elsewhere cannot be replaced, and the clean fails
  with the original untouched.
- A `<file>.bk.json` sidecar records what was found and muted (`--no-report`
  to skip).

## Development

```bash
uv sync
uv run pytest

# Real-speech end-to-end tests (need network for gTTS)
BK_E2E=1 scripts/e2e_speech.sh    # native
BK_E2E=1 scripts/e2e_docker.sh    # containerized
```

`scripts/e2e_docker.sh` runs the image named in `BK_IMAGE` (default
`blasphemy-killer:2.5.2`; build it first with
`docker build -t blasphemy-killer:2.5.2 .`) and additionally checks host file
ownership, done-marker persistence across runs, and the web UI. Both e2e
scripts need `uvx` (for gTTS) and bash.

CI (`.github/workflows/ci.yml`) runs `uv run pytest -q` on Ubuntu only;
Windows is tested by hand. Pushing a `vX.Y.Z` tag builds and pushes the
multi-arch image and publishes a GitHub release whose notes are the
`## X.Y.Z` section of CHANGELOG.md (the tag fails if that section is missing
or empty), so rename `## Unreleased` before tagging, and bump
`docker-compose.yml`, `scripts/e2e_docker.sh` and the README's `BK_IMAGE`
default with the version.
