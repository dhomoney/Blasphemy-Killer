#!/usr/bin/env python3
"""Extract one version's section from CHANGELOG.md.

The release workflow turns a v* tag into GitHub release notes with this, so the
changelog stays the single source of truth for what shipped: the notes on the
releases page are the section verbatim, never a paraphrase that can drift.

A heading may carry a short descriptor after an em dash, which becomes the
release title:

    ## 2.5.1 — broken muxes and stale assets

Without one the release is titled after the tag alone.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Version, then an optional "— descriptor" trailing the heading.
_HEADING = re.compile(
    r"^## (?P<version>\d+\.\d+\.\d+)(?:\s*[—-]\s*(?P<title>\S.*?))?\s*$",
    re.MULTILINE,
)


def section(text: str, version: str) -> tuple[str, str]:
    """Return (body, title) for one version. Raises KeyError if absent."""
    matches = list(_HEADING.finditer(text))
    for i, m in enumerate(matches):
        if m.group("version") != version:
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        return text[m.end():end].strip(), (m.group("title") or "").strip()
    raise KeyError(version)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--version", required=True, help="e.g. 2.5.1")
    ap.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"))
    ap.add_argument("--title", action="store_true",
                    help="print the heading's descriptor instead of the body")
    args = ap.parse_args()

    try:
        body, title = section(args.changelog.read_text(), args.version)
    except KeyError:
        # Fail rather than publish a release with empty notes.
        print(f"{args.changelog}: no section for {args.version}", file=sys.stderr)
        return 1

    if args.title:
        print(title)
    elif not body:
        print(f"{args.changelog}: section for {args.version} is empty", file=sys.stderr)
        return 1
    else:
        print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
