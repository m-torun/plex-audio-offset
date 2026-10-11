# plex-audio-offset

Shift the audio of Plex playback earlier or later, live, from a web page on
your phone. The offset is remembered for each player, so a TV whose sound runs
late keeps its correction for everything it plays.

This project is the daemon that does the shifting. The page is a separate
small project, [plex-buddy](https://github.com/m-torun/plex-buddy), which talks
to this daemon's API.

It works with the stock Plex apps. Nothing is installed on the player and no
media file is changed.

It does this by putting a wrapper in front of Plex's transcoder on the server.
Read "How it works" and "Limits" before installing it on a server you rely on.

## How it works

Plex Media Server starts a `Plex Transcoder` process for every stream it has
to convert. That transcoder is a build of FFmpeg, and it still accepts filter
commands on its standard input while it runs.

- A small wrapper takes the place of `Plex Transcoder`. For playback it appends
  a timing filter to the audio filter chain, gives the transcoder a control
  pipe, and starts the real transcoder. Everything else (DVR recording,
  analysis, thumbnails, conversions to a file) is passed through untouched.
- Audio that Plex would copy to the player untouched cannot be filtered. When
  it is AC-3, the wrapper has it decoded, shifted and encoded again as AC-3
  with the same channels (640 kb/s for surround, 256 kb/s for stereo), so the
  player gets the format it asked for.
- A service keeps the saved offsets, watches Plex's sessions to learn which
  player each transcoder serves, and sends the commands.
- The service answers an HTTP API on `127.0.0.1:8765` (see "API"). The page
  that shows what is playing and sets offsets is plex-buddy.

The wrapper is built to stay out of the way. Jobs that do not read a local
file and report progress to Plex never get past a three-line shell check. For
the rest, anything unfamiliar about the command line, and any error, ends in
the real transcoder being started with exactly the arguments Plex gave. With
the service stopped, every start is passed through.

### Why a change is not instant

A change can only alter audio the server has not sent yet, and the player
already holds some seconds of the stream. You hear the change when playback
reaches the first audio that was made after it.

Left alone, Plex's transcoder is soon minutes ahead of the player, and a
change would take that long to be heard. So the service paces it: about 10 s
ahead of the player normally, and about 3 s ahead while the page is open. The
page counts down to the moment the change is heard. While the player is
paused the transcoder is held, and the countdown waits.

On a Roku Ultra a change was heard 3 to 4 seconds after the tap, with the
picture playing on. That is close to the least a player like it allows: it
played without a stall with 1.5 to 3 s of stream in hand.

Making the player reload the stream instead does not get there faster. A
seek to where the player already is stays inside its buffer and changes
nothing, and a seek that does start a new stream costs 4 s or more of
loading. A stream that starts for any other reason (a new playback, or a skip
on the remote beyond what is transcoded) has the current offset from its
first frame.

## What an offset applies to

- **A player, not a show.** Setting the offset on a stream sets it for the
  player showing that stream: at once for what it is playing, and from then on
  for everything it plays. To correct one recording that is itself out of
  sync, set the offset back afterwards.
- **One player.** Each installed Plex app is its own player with its own
  offset: a Roku, Plex Web in one browser and a phone app are three players.
  New players start at 0.
- **One stream at a time.** Every stream has its own transcoder and its own
  card on the page. Streams playing at the same moment are adjusted separately.

## Requirements

- Plex Media Server on Linux with systemd, Python 3.6+ and passwordless sudo
  for the install. Tested with Plex 1.43 on Ubuntu 20.04.
- ffmpeg and numpy only for the measuring tool under `tools/`.

## Limits

- Plex must be processing the stream. Direct Play, where Plex sends the file
  as it is, cannot be shifted; the page lists such a stream with the reason.
  The Plex apps for Apple TV, iPhone and iPad play most files this way. Turning
  off "Allow Direct Play" in such an app's settings makes it ask the server for
  a processed stream, which can be shifted. Not tested on those devices.
- Of the audio Plex copies, only AC-3 is handled. Other copied codecs are left
  as they are and cannot be shifted.
- Copied AC-3 is encoded again for every stream, also at an offset of 0, so
  that it can be shifted at any moment. `--leave-copied-audio` turns that off.
- Library files only. Live TV is passed through unchanged.
- A change is heard a few seconds after it is made, not instantly: 3 to 4 s
  with the page open for a moment, up to about 10 s for a first change made
  right after opening it.
- The page has no login.
- Tested on one server with one real player (a Roku Ultra) and a simulated
  one (see below). Plex builds the transcoder's command line, and a later Plex
  version may build it differently; the wrapper then passes it through and
  the offset stops applying until the tool is updated.

## Install

From a checkout on any machine with ssh access to the server:

    ./deploy.sh user@server

or on the server itself:

    sudo ./install.sh install

Options: `--port N` (default 8765), `--plex-home DIR`, `--user NAME`,
`--python PATH`. The API has no login and listens on the server itself only;
install plex-buddy on the same machine for the page.

The installer registers the wrapper with `dpkg-divert`, so a Plex upgrade
replaces the real transcoder and leaves the wrapper in place.

To remove it and put the original transcoder back:

    ./deploy.sh user@server uninstall          # add --purge to drop saved offsets

Stopping the service (`systemctl stop plex-audio-offset`) switches the wrapper
to pure pass-through without uninstalling anything.

## Use

Play something, open the page, and move the offset until lips and sound agree.

- Minus moves the sound earlier, plus moves it later. Steps are 10 and 50 ms;
  the range is ±2000 ms.
- The status line says when a change will be heard.
- Players you have seen before are listed under "Saved players" and can be
  adjusted or forgotten while idle.

A new session gets its player's saved offset from the start. When Plex has
not yet said which player a new stream is for, it starts with the offset of
the player seen last, and is corrected within seconds if that was the wrong
one.

Encoding AC-3 again needs Plex's AC-3 encoder, which Plex downloads on demand
into a folder that is new after each Plex upgrade. When it is missing, the
stream is left as a copy, the service starts a few-second transcode of the
same item to make Plex fetch the encoder, and the next seek or playback picks
it up. That request shows up in Plex's device list as `plex-audio-offset`.
Run the service with `--leave-copied-audio` to never touch copied audio.

## API

All on `127.0.0.1:8765`, JSON in and out, no login.

| Request | Does |
| --- | --- |
| `GET /api/state` | Every stream (player, title, position, offset, whether it can be shifted and why not, when a pending change will be heard), the saved players, and the offset limit. |
| `POST /api/offset` | `{"session": id, "offset_ms": n}` for the player of a stream, or `{"client": id, "offset_ms": n}` for a saved player. |
| `POST /api/forget` | `{"client": id}` drops a saved player and its offset. |
| `POST /api/tuning` | `{"session": id, "on": true}` keeps a stream close to live so a change is heard within seconds; an interface sends it every 20 s while its page is open, and `false` when it closes. |

To reach the API from another machine, start the service with `--listen 0.0.0.0`.

## Testing on a real player

`tools/player_test.py` plays an item on a real player through Plex's
remote-control protocol and checks the service against it: the saved offset
at the start, pacing with the page open and closed, live changes, pause and
resume, and a seek. It stops playback and puts the watch state and the
player's offset back. Nobody needs to hold the remote, but the screen shows
the item while it runs (about two minutes).

    python3 tools/player_test.py --player roku --rating-key 1234 \
        --plex-address 192.0.2.10:32400 --launch-url http://192.0.2.20:8060/launch/13535

`--launch-url` opens the Plex app when it is not running; the example is the
one for a Roku.

## Testing without a player

`tools/sim_client.py` plays a library item the way a player would and saves
what it receives. `tools/measure_offset.py` compares that with the source file
and prints the audio shift over time. Both run on the server; the second needs
ffmpeg and numpy.

    python3 tools/sim_client.py --rating-key 1234 --out /tmp/run --seconds 60
    python3 tools/sim_client.py --rating-key 1234 --out /tmp/run --takes-audio ac3   # copied audio
    python3 tools/measure_offset.py --source /path/to/source.ts --stream /tmp/run/stream.ts

The simulated player reports its position to Plex and restores the item's
watch state when it ends. It stays in the page's list of saved players until
you forget it there.

Unit tests:

    python3 -m unittest discover -s tests -t .

## Files on the server

| Path | What |
| --- | --- |
| `/opt/plex-audio-offset` | the code |
| `/usr/lib/plexmediaserver/Plex Transcoder` | the wrapper |
| `/usr/lib/plexmediaserver/Plex Transcoder.real` | the real transcoder |
| `/var/lib/plex-audio-offset/offsets.json` | saved offsets |
| `/run/plex-audio-offset` | control pipes and state while the service runs |

Logs: `journalctl -u plex-audio-offset` and `/run/plex-audio-offset/shim.log`.
