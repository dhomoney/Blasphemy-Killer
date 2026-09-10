#!/bin/sh
# Drop privileges before anything touches media, then hand off to the CLI.
#
# Two idioms are supported:
#   docker run -e PUID=$(id -u) -e PGID=$(id -g) ...   (NAS/Unraid style)
#   docker run --user "$(id -u):$(id -g)" ...          (already non-root)
#
# Either way the files blasphemy-killer rewrites in place end up owned by the
# caller, not root.
set -e

if [ "$(id -u)" = "0" ]; then
    PUID=${PUID:-1000}
    PGID=${PGID:-1000}
    # The volumes may be brand new (Docker seeds them from the image) or may
    # come from a previous run under a different uid; either way, make them
    # writable for the uid we are about to become.
    chown "$PUID:$PGID" /config /cache 2>/dev/null || true
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups blasphemy-killer "$@"
fi

exec blasphemy-killer "$@"
