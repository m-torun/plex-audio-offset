#!/bin/sh
# Install or remove plex-audio-offset on the machine that runs Plex Media Server.
# Run as root from a checkout:  sudo ./install.sh install
set -eu

PREFIX=/opt/plex-audio-offset
PLEX_HOME=/usr/lib/plexmediaserver
RUN_DIR=/run/plex-audio-offset
STATE_DIR=/var/lib/plex-audio-offset
SERVICE_USER=plex
PORT=8765
PYTHON=/usr/bin/python3
UNIT=/etc/systemd/system/plex-audio-offset.service
MARK='Installed by plex-audio-offset'
PURGE=no

usage() {
    cat <<EOF
usage: install.sh install|uninstall|status [options]

  --port N          port for the phone page (default $PORT)
  --plex-home DIR   directory holding "Plex Transcoder" (default $PLEX_HOME)
  --user NAME       user that Plex Media Server runs as (default $SERVICE_USER)
  --python PATH     Python 3 interpreter (default $PYTHON)
  --purge           with uninstall: also delete the saved offsets
EOF
}

fail() { echo "install.sh: $*" >&2; exit 1; }

[ $# -ge 1 ] || { usage; exit 2; }
ACTION=$1; shift
while [ $# -gt 0 ]; do
    case $1 in
        --port) PORT=$2; shift 2 ;;
        --plex-home) PLEX_HOME=$2; shift 2 ;;
        --user) SERVICE_USER=$2; shift 2 ;;
        --python) PYTHON=$2; shift 2 ;;
        --purge) PURGE=yes; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
done

SRC=$(cd "$(dirname "$0")" && pwd)
TRANSCODER="$PLEX_HOME/Plex Transcoder"
REAL="$TRANSCODER.real"
SHIM="$PREFIX/plex_audio_offset/shim.py"

is_wrapper() { [ -f "$1" ] && head -c 400 "$1" | grep -aq "$MARK"; }

diverted() {
    command -v dpkg-divert >/dev/null 2>&1 && [ -n "$(dpkg-divert --list "$TRANSCODER")" ]
}

# Fill in a packaging template. The values are paths and names without
# characters that are special to sed or to single-quoted shell strings.
render() {
    sed -e "s|@REAL@|$REAL|g" -e "s|@RUN_DIR@|$RUN_DIR|g" -e "s|@PYTHON@|$PYTHON|g" \
        -e "s|@SHIM@|$SHIM|g" -e "s|@PREFIX@|$PREFIX|g" -e "s|@USER@|$SERVICE_USER|g" \
        -e "s|@PORT@|$PORT|g" "$1"
}

restore_transcoder() {
    if is_wrapper "$TRANSCODER" && [ -x "$REAL" ]; then
        mv -f "$REAL" "$TRANSCODER"
    fi
    if diverted; then
        dpkg-divert --quiet --local --no-rename --remove "$TRANSCODER"
    fi
}

do_install() {
    [ "$(id -u)" = 0 ] || fail "run as root"
    [ -x "$PYTHON" ] || fail "no Python at $PYTHON"
    [ -e "$TRANSCODER" ] || fail "no transcoder at $TRANSCODER"
    id "$SERVICE_USER" >/dev/null 2>&1 || fail "no user $SERVICE_USER"
    case "$PLEX_HOME$PYTHON$SERVICE_USER$PORT" in
        *[\|\&\\\']*) fail "unsupported character in an option" ;;
    esac

    install -d -m 755 "$PREFIX/plex_audio_offset/web"
    install -m 644 "$SRC"/plex_audio_offset/*.py "$PREFIX/plex_audio_offset/"
    install -m 644 "$SRC/plex_audio_offset/web/index.html" "$PREFIX/plex_audio_offset/web/"
    "$PYTHON" -I "$SHIM" --self-test >/dev/null || fail "the shim does not run under $PYTHON"

    render "$SRC/packaging/plex-audio-offset.service.in" > "$UNIT"
    systemctl daemon-reload
    systemctl enable --quiet plex-audio-offset.service
    systemctl restart plex-audio-offset.service
    tries=0
    until [ -e "$RUN_DIR/enabled" ]; do
        tries=$((tries + 1))
        [ $tries -le 50 ] || fail "the service did not start; see: journalctl -u plex-audio-offset"
        sleep 0.2
    done

    # Put the wrapper in place of the transcoder without ever leaving the
    # path empty: Plex may start a transcoder at any moment.
    wrapper="$PLEX_HOME/.plex-audio-offset-wrapper.$$"
    render "$SRC/packaging/transcoder-wrapper.sh.in" > "$wrapper"
    chmod 755 "$wrapper"
    if ! is_wrapper "$TRANSCODER"; then
        # Tell dpkg, so that a Plex upgrade replaces the real transcoder and
        # leaves the wrapper alone.
        if command -v dpkg-divert >/dev/null 2>&1 && ! diverted; then
            dpkg-divert --quiet --local --no-rename --divert "$REAL" --add "$TRANSCODER"
        fi
        ln -f "$TRANSCODER" "$REAL" 2>/dev/null || cp -p "$TRANSCODER" "$REAL"
    fi
    [ -x "$REAL" ] || { rm -f "$wrapper"; fail "no real transcoder at $REAL"; }
    mv -f "$wrapper" "$TRANSCODER"
    if ! "$TRANSCODER" -version >/dev/null 2>&1; then
        restore_transcoder
        fail "the wrapped transcoder does not run; the original was put back"
    fi

    echo "plex-audio-offset is installed."
    echo "Open http://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT/ on your phone."
}

do_uninstall() {
    [ "$(id -u)" = 0 ] || fail "run as root"
    restore_transcoder
    if [ -e "$UNIT" ]; then
        systemctl disable --quiet --now plex-audio-offset.service || true
        rm -f "$UNIT"
        systemctl daemon-reload
    fi
    rm -rf "$PREFIX"
    if [ "$PURGE" = yes ]; then
        rm -rf "$STATE_DIR"
    fi
    echo "plex-audio-offset is removed."
}

do_status() {
    if is_wrapper "$TRANSCODER"; then
        echo "transcoder: wrapped (real one at $REAL)"
    else
        echo "transcoder: original"
    fi
    echo "service: $(systemctl is-active plex-audio-offset.service 2>/dev/null || true)"
    [ -e "$RUN_DIR/enabled" ] && echo "shim: on" || echo "shim: off"
}

case $ACTION in
    install) do_install ;;
    uninstall) do_uninstall ;;
    status) do_status ;;
    *) usage; exit 2 ;;
esac
