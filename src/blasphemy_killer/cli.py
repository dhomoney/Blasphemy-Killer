"""Command-line interface and per-file pipeline orchestration."""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

import click

from . import __version__
from .config import Config, load_config
from .download import DownloadError, download, is_url
from .media import MediaError, VerifyError
from .pipeline import (
    Finished, MarkerUnsigned, MatchFound, MatchingComplete, Progress, Skipped,
    StageChanged, process,
)


_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

_MASK_RE = re.compile(r"(?<=[\w'])[\w']")


def _display(text: object) -> str:
    """Neutralize terminal control/escape sequences in untrusted strings
    (filenames, file metadata, transcripts, ffmpeg stderr) before echoing."""
    return _CONTROL_CHARS_RE.sub("?", str(text))


def _mask(text: str) -> str:
    """Obscure a matched blasphemy for display: keep each word's first
    character and star out the rest ("Jesus Christ" -> "J**** C*****")."""
    return _MASK_RE.sub("*", text)


def _timestamp(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


# Stages whose own fraction is measured; the rest show as a bare name.
_MEASURED = {"downloading", "extracting", "transcribing", "rendering"}


def _stage_text(stage: str, fraction: float | None) -> str:
    if stage == "loading-model":
        return "loading model..."
    if stage in _MEASURED and fraction is not None:
        return f"{stage} {fraction * 100:.0f}%"
    return stage


class StatusLine:
    """Progress on stderr, so stdout stays the same lines it always was.

    On a terminal it is one line rewritten in place with a bare carriage
    return (no ANSI codes, so conhost and Windows Terminal behave too), at most
    four times a second, wiped before any other output. Anywhere else -- a
    pipe, cron, docker logs -- it is a plain line each time the file's overall
    figure crosses another 10%, or after 30 s if the text has changed.
    """

    TTY_INTERVAL = 0.25
    PLAIN_INTERVAL = 30.0

    def __init__(self, *, enabled: bool, tty: bool, total_files: int = 1,
                 clock: Callable[[], float] = time.monotonic):
        self.enabled = enabled
        self.tty = tty
        self.total = max(1, total_files)
        self._clock = clock
        self._index = 0
        self._name = ""
        self._drawn = 0
        self._last_draw = clock()
        self._last_stage: str | None = None
        self._bucket = 0
        self._last_text = ""

    def start_file(self, index: int, name: str) -> None:
        self._index = index
        self._name = name
        self._bucket = 0
        self._last_stage = None
        self._last_text = ""
        self._last_draw = self._clock()

    def update(self, stage: str, fraction: float | None, overall: float | None) -> None:
        if not self.enabled:
            return
        now = self._clock()
        changed = stage != self._last_stage
        self._last_stage = stage
        if self.tty:
            if changed or now - self._last_draw >= self.TTY_INTERVAL:
                self._last_draw = now
                self._draw(self._tty_text(stage, fraction, overall))
            return
        # Plain lines: measured against the file's overall, else the stage's own.
        level = overall if overall is not None else fraction
        if level is None:
            return
        bucket = int(level * 10 + 1e-9)
        text = self._plain_text(stage, fraction, overall)
        due = now - self._last_draw >= self.PLAIN_INTERVAL
        if (bucket > self._bucket or due) and text != self._last_text:
            self._last_draw = now
            self._last_text = text
            click.echo(text, err=True)
        self._bucket = max(self._bucket, bucket)

    def _run_fraction(self, overall: float) -> float:
        return (self._index + overall) / self.total

    def _tty_text(self, stage: str, fraction: float | None, overall: float | None) -> str:
        head = f"[{self._index + 1}/{self.total}] " if self.total > 1 else ""
        if overall is not None:
            head += f"{self._run_fraction(overall) * 100:.0f}% "
        name = f"{self._name} - " if self._name else ""
        return f"{head}{name}{_stage_text(stage, fraction)}"

    def _plain_text(self, stage: str, fraction: float | None, overall: float | None) -> str:
        text = f"  {_stage_text(stage, fraction)}"
        if overall is None:
            return text
        if self.total > 1:
            return (f"{text} (file {overall * 100:.0f}%, "
                    f"run {self._run_fraction(overall) * 100:.0f}%)")
        return f"{text} (overall {overall * 100:.0f}%)"

    def _draw(self, text: str) -> None:
        # One column short of the width: a line that wraps can't be redrawn.
        width = shutil.get_terminal_size((80, 24)).columns - 1
        text = text[:width]
        click.echo("\r" + text.ljust(self._drawn), err=True, nl=False)
        self._drawn = len(text)

    def clear(self) -> None:
        """Wipe the in-place line, if one is showing."""
        if self._drawn:
            click.echo("\r" + " " * self._drawn + "\r", err=True, nl=False)
            self._drawn = 0

    def echo(self, message: str, *, err: bool = False) -> None:
        self.clear()
        click.echo(message, err=err)


def _collect_files(paths: list[Path], extensions: list[str], recursive: bool) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            pattern = "**/*" if recursive else "*"
            files.extend(
                p for p in sorted(path.glob(pattern))
                if p.is_file() and p.suffix.lower() in extensions
            )
        else:
            files.append(path)
    return files


def process_file(path: Path, cfg: Config, *, dry_run: bool, force: bool,
                 verbose: bool, tmp_dir: Path, status: StatusLine | None = None) -> dict:
    """Run the full pipeline on one file, echoing progress to the terminal.

    All the work lives in pipeline.process; this only turns its events into
    the lines the CLI has always printed, plus the status line on stderr.
    """
    status = status or StatusLine(enabled=False, tty=False)
    out = status.echo

    def echo(event) -> None:
        match event:
            case StageChanged(stage) if verbose:
                out(f"  stage: {stage}")
            case Progress(fraction, stage, overall):
                status.update(stage, fraction, overall)
            case Skipped("already-processed", marker):
                out(f"  skipped (already processed: {_display(marker)})")
            case Skipped("no-audio", _):
                out("  skipped (no audio streams)")
            case MarkerUnsigned():
                out("  done-marker present but not signed by this machine — reprocessing")
            case MatchFound(m):
                out(
                    f"  [{_timestamp(m.start)} - {_timestamp(m.end)}] "
                    f"\"{_display(_mask(m.text))}\"  ({_mask(m.phrase)})"
                )
            case MatchingComplete(0):
                out("  no matches found")
            case Finished(result) if result.status == "processed":
                out(
                    f"  muted {len(result.intervals)} interval(s) in {result.elapsed:.0f}s"
                )

    try:
        result = process(
            path, cfg, dry_run=dry_run, force=force, tmp_dir=tmp_dir, on_event=echo,
        )
    finally:
        status.clear()
    if result.status == "skipped":
        return {"status": "skipped"}
    if result.status == "dry-run":
        return {"status": "dry-run", "matches": len(result.matches)}
    return {
        "status": "processed",
        "matches": len(result.matches),
        "intervals": len(result.intervals),
    }


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("inputs", nargs=-1, metavar="PATH_OR_URL...")
@click.option("-r", "--recursive", is_flag=True, help="Descend into subdirectories.")
@click.option("-c", "--config", "config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), help="Extra config file (merged over defaults).")
@click.option("-m", "--model", help="Whisper model (default: small).")
@click.option("--language", help='Force transcription language, or "auto".')
@click.option("-n", "--dry-run", is_flag=True, help="Report matches without modifying anything.")
@click.option("-o", "--output", type=click.Path(path_type=Path), help="Output filename for a downloaded URL (single-URL invocations only).")
@click.option("--cookies", "cookies", type=click.Path(exists=True, dir_okay=False, path_type=Path), help="Netscape-format cookies.txt passed to yt-dlp for URL downloads.")
@click.option("--force", is_flag=True, help="Reprocess files that carry the done-marker.")
@click.option("--pad-ms", type=int, help="Symmetric mute padding in milliseconds.")
@click.option("--center-channel/--no-center-channel", "center_channel", default=None, help="5.1/7.1: transcribe the dialogue (center) channel instead of a flat downmix.")
@click.option("--vad/--no-vad", "vad", default=None, help="Gate transcription with voice-activity detection (default on).")
@click.option("--vad-threshold", type=click.FloatRange(0.0, 1.0), help="VAD sensitivity; lower keeps quieter speech (default 0.35).")
@click.option("--threads", type=int, help="CPU threads for transcription.")
@click.option("--no-report", is_flag=True, help="Skip the JSON sidecar report.")
@click.option("--keep-backup", is_flag=True, help="Keep the original file as .bak.")
@click.option("--list-phrases", is_flag=True, help="Print the effective phrase list and exit.")
@click.option("--progress/--no-progress", "progress", default=None, help="Show progress on stderr (default on: a live line on a terminal, a line per 10% otherwise).")
@click.option("-v", "--verbose", is_flag=True, help="Also print each pipeline stage as it starts.")
@click.version_option(__version__)
def main(inputs, recursive, config_path, model, language, dry_run, output,
         cookies, force, pad_ms, center_channel, vad, vad_threshold, threads, no_report, keep_backup, list_phrases,
         progress, verbose):
    """Mute audio segments that take the Lord's name in vain in video and audio files.

    PATH_OR_URL may be media files, directories, or http(s) URLs
    (downloaded with yt-dlp, then processed the same way).
    """
    cfg = load_config(config_path)
    if model:
        cfg.model = model
    if language:
        cfg.language = "" if language == "auto" else language
    if pad_ms is not None:
        cfg.pad_before_ms = cfg.pad_after_ms = pad_ms
    if center_channel is not None:
        cfg.center_channel = center_channel
    if vad is not None:
        cfg.vad_filter = vad
    if vad_threshold is not None:
        cfg.vad_threshold = vad_threshold
    if threads is not None:
        cfg.cpu_threads = threads
    if no_report:
        cfg.write_report = False
    if keep_backup:
        cfg.keep_backup = True
    if cookies is not None:
        cfg.cookies = cookies

    if list_phrases:
        for phrase in cfg.phrases:
            click.echo(phrase)
        return

    if not inputs:
        raise click.UsageError("no files, directories, or URLs given")

    urls = [a for a in inputs if is_url(a)]
    paths = [Path(a) for a in inputs if not is_url(a)]
    if output and (len(urls) != 1 or paths):
        raise click.UsageError("-o/--output requires exactly one URL and no file arguments")
    if cookies is not None and not urls:
        raise click.UsageError("--cookies only applies to URL downloads")
    for path in paths:
        if not path.exists():
            raise click.UsageError(f"no such file or directory: {path}")

    show = progress is not False
    tty = click.get_text_stream("stderr").isatty()
    files: list[Path] = []
    failed: list[str] = []
    for url in urls:
        click.echo(f"downloading {url}")
        fetching = StatusLine(enabled=show, tty=tty)
        try:
            files.append(download(
                url, output, cfg.cookies,
                # With --no-progress, yt-dlp keeps its own bar, as before.
                on_progress=(lambda stage, fraction: fetching.update(stage, fraction, None))
                if show else None,
            ))
        except DownloadError as exc:
            fetching.echo(f"  download failed: {_display(exc)}", err=True)
            failed.append(url)
        finally:
            fetching.clear()
    files.extend(_collect_files(paths, cfg.extensions, recursive))

    if not files and not failed:
        click.echo("nothing to process")
        return

    processed = skipped = 0
    status = StatusLine(enabled=show, tty=tty, total_files=len(files))
    with tempfile.TemporaryDirectory(prefix="blasphemy-killer-") as tmp:
        tmp_dir = Path(tmp)
        for index, path in enumerate(files):
            click.echo(_display(path))
            status.start_file(index, _display(path.name))
            try:
                result = process_file(
                    path, cfg, dry_run=dry_run, force=force,
                    verbose=verbose, tmp_dir=tmp_dir, status=status,
                )
            except (MediaError, VerifyError, OSError) as exc:
                click.echo(f"  FAILED: {_display(exc)}", err=True)
                failed.append(str(path))
                continue
            if result["status"] == "skipped":
                skipped += 1
            else:
                processed += 1

    click.echo(
        f"\n{processed} processed, {skipped} skipped, {len(failed)} failed"
    )
    if failed:
        for name in failed:
            click.echo(f"  failed: {_display(name)}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
