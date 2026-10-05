#!/usr/bin/env python3
"""Stand in for a Plex player, to test plex-audio-offset end to end.

Starts an HLS transcode of a library item the way a player would, fetches the
segments at playback speed, reports its position to Plex, and saves what it
received as an MPEG-TS file (a second one after a seek). Afterwards it puts
the item's watch state back.

Run it on any machine that can reach Plex Media Server. Python 3.6+, no
packages needed.
"""

import argparse
import json
import os
import posixpath
import random
import sys
import time
import xml.etree.ElementTree as ET
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class Plex:
    def __init__(self, url, token, headers):
        self.url = url.rstrip("/")
        self.headers = dict(headers, **{"X-Plex-Token": token})

    def get(self, path, params=None, timeout=30):
        if params:
            path += ("&" if "?" in path else "?") + urlencode(params)
        with urlopen(Request(self.url + path, headers=self.headers), timeout=timeout) as response:
            return response.read()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--rating-key", required=True, help="library item to play")
    parser.add_argument("--out", required=True, help="directory for what is received")
    parser.add_argument("--plex-url", default="http://127.0.0.1:32400")
    parser.add_argument("--token-file", default=os.path.expanduser("~/.config/plex-token"))
    parser.add_argument("--start", type=int, default=300, help="position to start at, in seconds")
    parser.add_argument("--seconds", type=float, default=60, help="how long to play")
    parser.add_argument("--buffer", type=float, default=20,
                        help="how many seconds ahead of its position the player fetches")
    parser.add_argument("--seek-after", type=float, help="seek once, this many seconds in")
    parser.add_argument("--seek-to", type=int, help="position to seek to, in seconds")
    parser.add_argument("--name", default="plex-audio-offset test player")
    parser.add_argument("--client-id", default="plex-audio-offset-sim")
    parser.add_argument("--platform", default="Roku",
                        help="platform to present as; Plex picks the streaming profile from it")
    parser.add_argument("--device", default="Roku Ultra", help="device model to present as")
    parser.add_argument("--takes-audio", metavar="CODEC",
                        help="say the player plays this audio codec as it is (for example ac3), "
                             "so that Plex copies such audio instead of converting it")
    parser.add_argument("--session", help="session id (default: a random one)")
    parser.add_argument("--keep-progress", action="store_true",
                        help="leave the item's watch state as the test changed it")
    args = parser.parse_args()

    with open(args.token_file) as f:
        token = f.read().strip()
    session = args.session or "pao-sim-%08x" % random.getrandbits(32)
    identity = {
        "X-Plex-Client-Identifier": args.client_id, "X-Plex-Session-Identifier": session,
        "X-Plex-Product": args.name, "X-Plex-Device-Name": args.name,
        "X-Plex-Platform": args.platform, "X-Plex-Device": args.device, "X-Plex-Version": "1.0",
    }
    plex = Plex(args.plex_url, token, identity)
    key = "/library/metadata/%s" % args.rating_key
    item = ET.fromstring(plex.get(key))[0]
    duration_ms = int(item.get("duration"))
    before = (int(item.get("viewOffset") or 0), int(item.get("viewCount") or 0))
    source = item.find(".//Part").get("file")
    os.makedirs(args.out, exist_ok=True)

    request = dict(identity, **{
        "hasMDE": 1, "path": key, "mediaIndex": 0, "partIndex": 0, "protocol": "hls",
        "fastSeek": 1, "directPlay": 0, "directStream": 1, "directStreamAudio": 1,
        "location": "lan", "maxVideoBitrate": 20000, "videoResolution": "1920x1080",
        "videoQuality": 100, "subtitles": "none", "offset": args.start, "session": session,
    })
    if args.takes_audio:
        request["X-Plex-Client-Profile-Extra"] = (
            "add-transcode-target-audio-codec(type=videoProfile&context=streaming"
            "&protocol=hls&audioCodec=%s)" % args.takes_audio)
    # Plex refuses to start a transcode it has not been asked to decide on.
    plex.get("/video/:/transcode/universal/decision", request)
    master = plex.get("/video/:/transcode/universal/start.m3u8", request).decode()
    variant = "/video/:/transcode/universal/" + [
        line for line in master.splitlines() if line and not line.startswith("#")][0]
    # The playlist covers the whole item in one-second segments, whatever the
    # start position, so segment N holds second N.
    names = [line for line in plex.get(variant).decode().splitlines()
             if line and not line.startswith("#")]

    def timeline(state, position):
        plex.get("/:/timeline", dict(identity, **{
            "ratingKey": args.rating_key, "key": key, "state": state, "hasMDE": 1,
            "time": int(position * 1000), "duration": duration_ms}), timeout=5)

    print("session %s playing %s from %d s" % (session, source, args.start), flush=True)
    fetched = []
    position = float(args.start)  # where playback is
    upcoming = args.start         # next segment to fetch
    started = clock = None
    reported = 0
    seek_after = args.seek_after if args.seek_to is not None else None
    stream = open(os.path.join(args.out, "stream.ts"), "wb")
    try:
        timeline("buffering", position)
        while True:
            now = time.monotonic()
            if clock is not None:
                position += now - clock
                clock = now
            elapsed = 0.0 if started is None else now - started
            if elapsed >= args.seconds:
                break
            if seek_after is not None and elapsed >= seek_after:
                seek_after = None
                position, upcoming = float(args.seek_to), args.seek_to
                stream.close()
                stream = open(os.path.join(args.out, "stream-after-seek.ts"), "wb")
                print("seek to %d s after %.1f s" % (args.seek_to, elapsed), flush=True)
            if now - reported >= 1:
                timeline("playing" if started else "buffering", position)
                reported = now
            if upcoming < len(names) and upcoming - position < args.buffer:
                asked = time.monotonic()
                data = plex.get(posixpath.join(posixpath.dirname(variant), names[upcoming]))
                stream.write(data)
                took = time.monotonic() - asked
                # "ahead" is how much the player still had to play when the
                # segment arrived; below zero, playback would have stalled.
                fetched.append({"media_time": upcoming, "bytes": len(data), "wall": time.time(),
                                "elapsed": round(elapsed, 2), "took": round(took, 2),
                                "ahead": round(upcoming - position - (took if clock else 0), 2)})
                upcoming += 1
                if started is None:
                    started = clock = time.monotonic()
            else:
                time.sleep(0.05)
    finally:
        stream.close()
        try:
            timeline("stopped", position)
            try:
                plex.get("/video/:/transcode/universal/stop", {"session": session}, timeout=10)
            except HTTPError:
                pass  # Plex already ended the transcode when playback stopped
        finally:
            if not args.keep_progress:
                library = {"identifier": "com.plexapp.plugins.library", "key": args.rating_key}
                if before == (0, 0):
                    plex.get("/:/unscrobble", library, timeout=10)
                else:
                    plex.get("/:/progress", dict(library, time=before[0], state="stopped"), timeout=10)
    item = ET.fromstring(plex.get(key))[0]
    after = (int(item.get("viewOffset") or 0), int(item.get("viewCount") or 0))
    with open(os.path.join(args.out, "segments.json"), "w") as f:
        json.dump({"session": session, "source": source, "start": args.start, "segments": fetched}, f)
    print("fetched %d segments; watch state (offset ms, views) before %s, after %s"
          % (len(fetched), before, after))
    return 0 if args.keep_progress or after == before else 1


if __name__ == "__main__":
    sys.exit(main())
