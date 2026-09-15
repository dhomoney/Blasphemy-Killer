# Changelog

## 2.2.0

Paste a URL, watch it download, and play it back without leaving the page.
The web UI gained the half of the CLI it was missing — yt-dlp downloads — plus
a player, so a video can be fetched, scanned and checked in one place.

### Added

- **Paste a URL into the web UI.** The bar above the library hands it to
  yt-dlp, downloads into the directory being browsed, reports progress in the
  queue, and opens the result in the player. The browser supplies the URL and
  nothing else: cookies come from `config.toml`, because an unauthenticated
  page that took a file path would be a way to read any file on the host.
- **A player.** Clicking a filename plays it in the page. `GET /api/media`
  answers range requests, so seeking through a long file does not download it
  first. A scanned file shows its matches as marks on a strip under the player
  and as a list beside it; clicking either seeks to just before that moment.
- **Cookies upload.** The row under the URL bar takes a `cookies.txt` for
  age-gated, members-only or private videos. The browser reads the file and
  sends its contents, never a path, so nothing caller-supplied is ever opened
  server-side; it is checked for actually being Netscape format (a JSON export
  is refused with an explanation instead of failing mid-download) and written
  to the config directory mode `600`. The UI reports how many cookies are in
  place and can remove them, but never reads them back out. An upload wins over
  a `cookies` path in `config.toml`, and is resolved per download, so uploading
  or removing cookies affects the next download without a restart.
- `download()` takes `dest_dir` and an `on_progress` callback, and downloads
  can be cancelled from the queue like any other job. Cancelling discards the
  partial file — `.part` is not a media extension, so anything left behind
  would sit in the library invisibly.

## 2.1.0

A web UI, served by the same image. `docker compose up web`, then browse the
library, queue files, and watch progress instead of composing `docker run`
lines and staring at a silent cursor.

### Added

- **Web UI** at `docker run ... serve` (or `docker compose up web`): browse the
  mounted media directory, see which files already carry the done-marker, queue
  files, and watch transcription progress and matches stream in live over SSE.
  Dry run is the default and cleaning for real takes an explicit confirmation.
  Running jobs can be cancelled.
- `blasphemy-killer-serve` console script for running the UI natively.
- `pipeline.process()`, the per-file pipeline as an event stream. The CLI and
  the web UI are both consumers of it; terminal formatting no longer lives in
  the middle of the pipeline.
- Transcription reports progress. `transcribe()` takes `on_progress` and
  `check_cancelled` callbacks, which is what makes both the progress bar and
  cancellation possible — previously the slowest stage was a black box.
- `scripts/e2e_docker.sh` now covers the web UI as well as the CLI.

### Notes

- The UI has **no authentication** and rewrites media in place. It is published
  on `127.0.0.1` by the compose file for that reason; exposing it on `0.0.0.0`
  gives everyone on your network the ability to overwrite your library.
- The server keeps the whisper model loaded between jobs, so after the first
  file each subsequent one skips the ~10 s model load the CLI pays every run.
- Jobs are held in memory: restarting the container clears the queue.
- CLI output is unchanged from 2.0.0.

## 2.0.0

Docker is now the primary way to install and run blasphemy-killer. Running
natively with `uv`/`pip` still works exactly as before.

### Added

- **Official Docker image**, `dhomoney/blasphemy-killer`, for `linux/amd64` and
  `linux/arm64`. ffmpeg, ffprobe, deno and every Python dependency are baked in,
  so the only prerequisite is Docker.
- `docker-compose.yml` for the recurring "clean my library" case.
- `BK_CONFIG_DIR` environment variable overrides the location of `config.toml`
  and `marker.key` (default `~/.config/blasphemy-killer`, unchanged). The image
  points it at the `/config` volume.
- `scripts/e2e_docker.sh` — containerized end-to-end test covering the mutes,
  host file ownership and done-marker persistence.
- GitHub Actions: `pytest` on every push, multi-arch image build and publish on
  a `v*` tag.

### Changed

- Transcription now sizes its thread pool from the cgroup CPU quota when there
  is one, so `docker run --cpus 2` no longer starts eight whisper threads.
  Outside a container the behavior is unchanged.
- yt-dlp is offered every JS runtime it knows about (deno, node, bun, quickjs)
  instead of only its default, so an already-installed runtime is used when
  present. The image ships deno.
- `--cookies` with no URL arguments is now a usage error rather than being
  silently ignored, matching how `-o/--output` is validated. A cookies path
  set in `config.toml` still stays quiet on file-only runs.

### Notes for existing users

- Done-markers written by 1.x remain valid — `marker_valid()` verifies against
  the version recorded inside the marker, not the running version, so upgrading
  does not trigger a re-scan of an already-cleaned library.
- If you switch from a native install to the image and want that to stay true,
  copy your existing `~/.config/blasphemy-killer/marker.key` into the `/config`
  volume. A fresh key means every previously cleaned file is reprocessed once.
