#!/bin/sh
# Copy this checkout to the Plex server and run the installer there.
#
#   ./deploy.sh user@host [install|uninstall|status] [installer options]
#
# Needs key-based ssh and passwordless sudo on the server.
set -eu

[ $# -ge 1 ] || { sed -n '2,6p' "$0"; exit 2; }
HOST=$1; shift
[ $# -ge 1 ] || set -- install

HERE=$(cd "$(dirname "$0")" && pwd)
REMOTE=$(ssh "$HOST" 'mktemp -d')
trap 'ssh "$HOST" "rm -rf \"$REMOTE\""' EXIT

# Keep macOS tar from adding ._ files and extended attributes.
case $(tar --version 2>/dev/null) in
    bsdtar*) PLAIN=--no-xattrs ;;
    *) PLAIN= ;;
esac
COPYFILE_DISABLE=1 tar -C "$HERE" $PLAIN --exclude __pycache__ -cf - install.sh packaging plex_audio_offset |
    ssh "$HOST" "tar -C \"$REMOTE\" -xf -"

ARGS=
for arg in "$@"; do
    ARGS="$ARGS '$arg'"
done
ssh "$HOST" "sudo sh \"$REMOTE/install.sh\" $ARGS"
