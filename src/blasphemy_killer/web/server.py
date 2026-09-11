"""Entry point for `blasphemy-killer-serve` (and `docker run ... serve`)."""

from __future__ import annotations

import os
from pathlib import Path

import click
import uvicorn

from .. import __version__
from ..config import load_config


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--host", default=lambda: os.environ.get("BK_WEB_HOST", "127.0.0.1"),
              help="Address to bind. Defaults to localhost; the image sets 0.0.0.0 "
                   "because a container's loopback is not reachable from the host.")
@click.option("--port", default=lambda: int(os.environ.get("BK_WEB_PORT", "8080")),
              type=int, help="Port to listen on (default 8080).")
@click.option("--media-root", type=click.Path(file_okay=False, path_type=Path),
              default=lambda: Path(os.environ.get("BK_MEDIA_ROOT", "/media")),
              help="Directory the UI may browse and process (default /media).")
@click.option("-c", "--config", "config_path",
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Extra config file (merged over defaults).")
@click.version_option(__version__)
def main(host: str, port: int, media_root: Path, config_path: Path | None) -> None:
    """Serve the blasphemy-killer web UI."""
    from .app import create_app

    root = media_root.expanduser().resolve()
    if not root.is_dir():
        raise click.UsageError(f"media root is not a directory: {root}")

    cfg = load_config(config_path)

    # One stream, in order: split across stdout and stderr this interleaves
    # unreadably in `docker logs`.
    banner = [
        f"blasphemy-killer {__version__}",
        f"  media root: {root}",
        f"  listening:  http://{host}:{port}",
    ]
    if host not in ("127.0.0.1", "localhost", "::1"):
        # Inside a container this is always the case and is fine -- what limits
        # access there is the address the port is published on.
        banner += [
            "",
            "  NOTE: this UI has no authentication and rewrites media in place.",
            "        Anyone who can reach this port can overwrite your files.",
            "        In Docker, publish it as 127.0.0.1:8080:8080 to keep it to",
            "        this machine, or put an authenticating proxy in front.",
            "",
        ]
    click.echo("\n".join(banner))
    uvicorn.run(create_app(root, cfg), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
