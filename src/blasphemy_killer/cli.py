"""Command-line interface and per-file pipeline orchestration."""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

import click

from . import __version__
from .config import Config, load_config
from .download import DownloadError, download, is_url
from .media import MediaError, VerifyError
from .pipeline import (
    Finished, MarkerUnsigned, MatchFound, MatchingComplete, Skipped, process,
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
                 verbose: bool, tmp_dir: Path) -> dict:
    """Run the full pipeline on one file, echoing progress to the terminal.

    All the work lives in pipeline.process; this only turns its events into
    the lines the CLI has always printed.
    """
    def echo(event) -> None:
        match event:
            case Skipped("already-processed", marker):
                click.echo(f"  skipped (already processed: {_display(marker)})")
            case Skipped("no-audio", _):
                click.echo("  skipped (no audio streams)")
            case MarkerUnsigned():
                click.echo("  done-marker present but not signed by this machine — reprocessing")
            case MatchFound(m):
                click.echo(
                    f"  [{_timestamp(m.start)} - {_timestamp(m.end)}] "
                    f"\"{_display(_mask(m.text))}\"  ({_mask(m.phrase)})"
                )
            case MatchingComplete(0):
                click.echo("  no matches found")
            case Finished(result) if result.status == "processed":
                click.echo(
                    f"  muted {len(result.intervals)} interval(s) in {result.elapsed:.0f}s"
                )

    result = process(
        path, cfg, dry_run=dry_run, force=force, tmp_dir=tmp_dir, on_event=echo,
    )
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
@click.option("--threads", type=int, help="CPU threads for transcription.")
@click.option("--no-report", is_flag=True, help="Skip the JSON sidecar report.")
@click.option("--keep-backup", is_flag=True, help="Keep the original file as .bak.")
@click.option("--list-phrases", is_flag=True, help="Print the effective phrase list and exit.")
@click.option("-v", "--verbose", is_flag=True)
@click.version_option(__version__)
def main(inputs, recursive, config_path, model, language, dry_run, output,
         cookies, force, pad_ms, threads, no_report, keep_backup, list_phrases,
         verbose):
    """Mute audio segments that take the Lord's name in vain in video files.

    PATH_OR_URL may be video files, directories, or http(s) URLs
    (downloaded with yt-dlp, then processed the same way).
    """
    cfg = load_config(config_path)
    if model:
        cfg.model = model
    if language:
        cfg.language = "" if language == "auto" else language
    if pad_ms is not None:
        cfg.pad_before_ms = cfg.pad_after_ms = pad_ms
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

    files: list[Path] = []
    failed: list[str] = []
    for url in urls:
        click.echo(f"downloading {url}")
        try:
            files.append(download(url, output, cfg.cookies))
        except DownloadError as exc:
            click.echo(f"  download failed: {_display(exc)}", err=True)
            failed.append(url)
    files.extend(_collect_files(paths, cfg.extensions, recursive))

    if not files and not failed:
        click.echo("nothing to process")
        return

    processed = skipped = 0
    with tempfile.TemporaryDirectory(prefix="blasphemy-killer-") as tmp:
        tmp_dir = Path(tmp)
        for path in files:
            click.echo(_display(path))
            try:
                result = process_file(
                    path, cfg, dry_run=dry_run, force=force,
                    verbose=verbose, tmp_dir=tmp_dir,
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
