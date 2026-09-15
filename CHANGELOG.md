# Changelog

## 2.3.0

Pasting a URL into the web UI now ends where you wanted it to end: with a
cleaned file. The download was only ever half the job, and finishing it by
hand — find the file that just landed, tick it, untick dry run, confirm the
dialog — was a second decision nobody was asking to make.

### Changed

- **A URL downloaded in the web UI is cleaned when it lands.** The worker that
  finishes the download queues the clean itself, using the name yt-dlp settled
  on, so it cannot be aimed at the wrong file or start while bytes are still
  arriving. The clean is a queue entry of its own — its own progress, its own
  matches, cancellable like any other job — rather than a hidden phase of the
  download, and the player now opens when *it* finishes, on the cleaned file
  with its matches already on the timeline.

  This is the one place the UI cleans without a confirmation dialog, which is
  deliberate: the dialog guards files that were already in your library, and a
  URL you just pasted is not one of them. Nothing is cleaned if the download
  failed or was cancelled.

- **A promised clean survives a restart.** Only the automatic ones: a job you
  queued from the listing is a live decision you can make again, but one queued
  on your behalf while you were somewhere else would otherwise vanish in a
  restart, leaving the file looking merely "not scanned" with nothing anywhere
  saying a promise had been dropped. It is written to the config volume when it
  is made — atomically, flushed to the disk first, validated when it is read
  back rather than trusted because it exists, all the way this release's
  predecessor learned to do it — and re-queued at startup. Entries whose file
  has moved, or whose path does not resolve inside the media root, are dropped.
  Downloads are not resumed; a crash mid-download leaves a `.part` that yt-dlp
  owns.

- **`POST /api/downloads` takes `clean` (default `true`)**, and download jobs
  carry `clean`; the job it queues carries `source`, the id of the download it
  came from.

### Added

- **A "Clean it when it lands" checkbox** beside the URL bar, ticked by
  default. Untick it for the old behaviour — download only — and queue the
  file from the listing whenever you like.

### Fixed

- **A promised clean can no longer be swallowed by a job already queued.** The
  queue folds a repeat request for a path into whatever is already waiting,
  which is right for a person clicking twice and wrong for a promise: if a dry
  run happened to be queued for the same name, the file was never cleaned and
  the UI went on saying "cleaning next" forever. Only an identical clean still
  waiting is adopted now, and it is stamped with the download it belongs to.

- **Cancelling a clean no longer leaves an invisible temp file behind.**
  `Cancelled` is not an error, so it sailed past the clause that cleans up
  after a failure — and the leftover is a dot-file, which the library listing
  filters out, so it sat in the media directory forever. Found while going over
  the cancel paths, which auto-queued cleans make a routine thing to use.

- **Cancelling during rendering now actually cancels.** Rendering is the long
  pass and had no checkpoint after it, so a cancel raised while ffmpeg worked
  was never seen: the UI said "cancelling…" and the file was replaced anyway.
  There is now a last check immediately before the swap, which is the last
  moment stopping is worth anything.

- **A worker publishing an event into a closed event loop no longer takes the
  worker thread down** during shutdown.

The CLI is unchanged: `blasphemy-killer <url>` has always downloaded and then
cleaned, and this brings the web UI in line with it.

## 2.2.1

Three fixes to URL downloads, all found while investigating a first run of
2.2.0 that sat at "downloading 0%" and never moved. The stall itself was not
this program — the Docker Desktop VM's guest kernel panicked underneath it,
in its gRPC-FUSE bind mount, while yt-dlp was creating the `.part` file. What
the crash then left behind was ours.

### Fixed

- **A cookies.txt that survived a crash no longer breaks every download.**
  The upload was swapped in atomically, but an atomic replace only promises
  that the name flips between two complete files — it promises nothing about
  the contents having reached the disk first. A machine that dies in that gap
  leaves a zero-byte `cookies.txt`, which yt-dlp refuses outright, so every
  later download fails with "does not look like a Netscape format cookies
  file" while the UI still reports cookies in place. The upload is now flushed
  to the disk before the rename, and the rename itself is persisted after it.
- **A cookies file is checked when it is read, not just when it is written.**
  Existence was taken as proof a stored file was usable. It is now validated
  at the point a download asks for it, so an unusable one is ignored and
  downloads fall back to the `cookies` path in `config.toml` instead of
  failing. The bad file is left on disk rather than deleted, and uploading a
  new one recovers as it always did. `GET /api/cookies` stops advertising an
  unreadable file as present, which is what made this invisible.
- **Download progress can no longer exceed 100%.** When yt-dlp has only
  `total_bytes_estimate` to go on it can guess low, and the fraction sailed
  past 1.0 — an observed run reported 334%, which the UI rendered as both an
  overflowing bar and a "downloading 334%" label. Clamped at the source, and
  again where it is rendered.

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
