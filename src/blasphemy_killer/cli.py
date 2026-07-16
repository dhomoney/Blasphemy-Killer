"""Command-line interface and per-file pipeline orchestration."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

import click

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _display(text: object) -> str:
    """Neutralize terminal control/escape sequences in untrusted strings
    (filenames, file metadata, transcripts, ffmpeg stderr) before echoing."""
    return _CONTROL_CHARS_RE.sub("?", str(text))


def _write_report(report_path: Path, report: dict) -> None:
    """Write the JSON sidecar, refusing to follow a pre-placed symlink."""
    fd = os.open(
        report_path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
        0o644,
    )
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(report, indent=2))

from . import __version__
from .config import Config, load_config
from .download import DownloadError, download, is_url
from .match import build_intervals, find_matches
from .media import MediaError, VerifyError, atomic_replace, extract_wav, probe, verify_output
from .mute import marker_valid, render, stamp_only


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
    """Run the full pipeline on one file. Returns a result dict for the summary."""
    started = time.monotonic()
    info = probe(path)

    if info.marker and not force:
        if marker_valid(info):
            click.echo(f"  skipped (already processed: {_display(info.marker)})")
            return {"status": "skipped"}
        click.echo("  done-marker present but not signed by this machine — reprocessing")
    stream = info.transcription_stream
    if stream is None:
        click.echo("  skipped (no audio streams)")
        return {"status": "skipped"}

    wav = tmp_dir / "audio.wav"
    extract_wav(path, stream, wav)

    from .transcribe import transcribe  # deferred: heavy import, not needed for --help etc.
    words = transcribe(
        wav, model=cfg.model, language=cfg.language or None,
        cpu_threads=cfg.cpu_threads, beam_size=cfg.beam_size,
    )
    wav.unlink(missing_ok=True)

    matches = find_matches(words, cfg.phrases)
    intervals = build_intervals(
        matches, pad_before=cfg.pad_before, pad_after=cfg.pad_after,
        clamp_end=info.duration or None,
    )

    for m in matches:
        click.echo(f"  [{_timestamp(m.start)} - {_timestamp(m.end)}] \"{_display(m.text)}\"  ({m.phrase})")
    if not matches:
        click.echo("  no matches found")

    if dry_run:
        return {"status": "dry-run", "matches": len(matches)}

    # Random name, created 0600 with O_EXCL: not guessable or symlink-plantable
    # by another writer in a shared directory. Still matches .gitignore's
    # `.*.bk-tmp.*` pattern.
    fd, out_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.stem}.bk-tmp.", suffix=path.suffix
    )
    os.close(fd)
    out_tmp = Path(out_name)
    try:
        if intervals:
            render(info, intervals, out_tmp, tmp_dir / "filter.txt")
        else:
            stamp_only(info, out_tmp)
        verify_output(info, out_tmp)
        atomic_replace(out_tmp, path, keep_backup=cfg.keep_backup and bool(intervals))
    except (MediaError, OSError):
        out_tmp.unlink(missing_ok=True)
        raise

    if cfg.write_report:
        report = {
            "tool": "blasphemy-killer",
            "version": __version__,
            "file": str(path),
            "model": cfg.model,
            "matches": [asdict(m) for m in matches],
            "muted_intervals": intervals,
            "duration": info.duration,
            "elapsed_seconds": round(time.monotonic() - started, 1),
        }
        _write_report(path.with_name(path.name + ".bk.json"), report)

    elapsed = time.monotonic() - started
    click.echo(f"  muted {len(intervals)} interval(s) in {elapsed:.0f}s")
    return {"status": "processed", "matches": len(matches), "intervals": len(intervals)}


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("inputs", nargs=-1, metavar="PATH_OR_URL...")
@click.option("-r", "--recursive", is_flag=True, help="Descend into subdirectories.")
@click.option("-c", "--config", "config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), help="Extra config file (merged over defaults).")
@click.option("-m", "--model", help="Whisper model (default: small).")
@click.option("--language", help='Force transcription language, or "auto".')
@click.option("-n", "--dry-run", is_flag=True, help="Report matches without modifying anything.")
@click.option("-o", "--output", type=click.Path(path_type=Path), help="Output filename for a downloaded URL (single-URL invocations only).")
@click.option("--force", is_flag=True, help="Reprocess files that carry the done-marker.")
@click.option("--pad-ms", type=int, help="Symmetric mute padding in milliseconds.")
@click.option("--threads", type=int, help="CPU threads for transcription.")
@click.option("--no-report", is_flag=True, help="Skip the JSON sidecar report.")
@click.option("--keep-backup", is_flag=True, help="Keep the original file as .bak.")
@click.option("--list-phrases", is_flag=True, help="Print the effective phrase list and exit.")
@click.option("-v", "--verbose", is_flag=True)
@click.version_option(__version__)
def main(inputs, recursive, config_path, model, language, dry_run, output,
         force, pad_ms, threads, no_report, keep_backup, list_phrases, verbose):
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
    for path in paths:
        if not path.exists():
            raise click.UsageError(f"no such file or directory: {path}")

    files: list[Path] = []
    failed: list[str] = []
    for url in urls:
        click.echo(f"downloading {url}")
        try:
            files.append(download(url, output))
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
