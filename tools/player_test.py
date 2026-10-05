#!/usr/bin/env python3
"""Test plex-audio-offset on a real player, without anyone holding the remote.

Plays a library item on a player through Plex's remote-control protocol
(the one Plex's own apps use on each other), then checks what the offset
service does with the stream: the saved offset at the start, pacing, live
changes, pause and resume, and a seek. At the end it stops playback and puts
the item's watch state and the player's offset back.

The player's screen shows the item while this runs. Run it on the Plex
server. Python 3.6+, no packages needed.
"""

import argparse
import itertools
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlencode
from urllib.request import Request, urlopen

NAME = "plex-audio-offset-test"


class Plex:
    def __init__(self, url, token):
        self.url, self.token = url.rstrip("/"), token

    def get(self, path, params=None, method="GET"):
        url = self.url + path + ("?" + urlencode(params) if params else "")
        request = Request(url, method=method, headers={
            "X-Plex-Token": self.token, "X-Plex-Client-Identifier": NAME, "X-Plex-Product": NAME})
        with urlopen(request, timeout=15) as response:
            return ET.fromstring(response.read() or b"<empty/>")


class Player:
    """One player, spoken to the way Plex's apps speak to each other."""

    def __init__(self, host, port, client_id):
        self.address, self.client_id = "http://%s:%s" % (host, port), client_id
        self.ids = itertools.count(int(time.time()) % 1000000)

    def send(self, path, params):
        query = urlencode(dict(params, commandID=next(self.ids)))
        request = Request("%s%s?%s" % (self.address, path, query), headers={
            "X-Plex-Client-Identifier": NAME, "X-Plex-Device-Name": NAME,
            "X-Plex-Target-Client-Identifier": self.client_id})
        with urlopen(request, timeout=15) as response:
            return response.read()

    def video(self):
        """State and position (seconds) of the video the player has open."""
        # The first answer after a gap can be a stale "stopped": ask twice.
        found = ("", None)
        for attempt in range(2):
            root = ET.fromstring(self.send("/player/timeline/poll", {"wait": 0}))
            for element in root.iter("Timeline"):
                if element.get("type") == "video":
                    position = element.get("time", "")
                    found = (element.get("state", ""), int(position) / 1000.0 if position.isdigit() else None)
            if found[0] != "stopped":
                break
            time.sleep(0.5)
        return found

    def command(self, name, **params):
        """Send a playback command, but only to a player that is playing."""
        state, position = self.video()
        if state not in ("playing", "paused") or position is None:
            raise RuntimeError("the player is %s; not sending %s" % (state or "silent", name))
        self.send("/player/playback/" + name, dict(params, type="video"))


class Test:
    def __init__(self, args):
        self.args = args
        with open(args.token_file) as f:
            self.plex = Plex(args.plex_url, f.read().strip())
        self.failures = 0
        self.began = time.time()

    def say(self, text):
        print("%6.1fs  %s" % (time.time() - self.began, text), flush=True)

    def check(self, ok, text):
        self.say(("ok    " if ok else "FAIL  ") + text)
        self.failures += not ok
        return ok

    # ---- the offset service ------------------------------------------

    def service(self, path="/api/state", body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = Request(self.args.service_url.rstrip("/") + path, data=data,
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def session(self):
        """This player's stream as the service sees it, or None."""
        for session in self.service()["sessions"]:
            if session.get("client") == self.player.client_id:
                return session
        return None

    def saved_offset(self):
        for client in self.service()["clients"]:
            if client["id"] == self.player.client_id:
                return client["offset_ms"]
        return 0

    def watch(self, seconds, tuning):
        """Follow steady playback. Returns (times not playing, restarts, leads)."""
        idle, restarts, leads, pid, kept = 0, 0, [], None, 0
        end = time.time() + seconds
        while time.time() < end:
            session = self.session()
            if session:
                if tuning and time.time() - kept > 15:
                    self.service("/api/tuning", {"session": session["id"], "on": True})
                    kept = time.time()
                restarts += pid is not None and session["pid"] != pid
                pid = session["pid"]
                if session["lead_s"] is not None:
                    leads.append(session["lead_s"])
            idle += self.player.video()[0] != "playing"
            time.sleep(0.5)
        return idle, restarts, sorted(leads)

    # ---- the steps ---------------------------------------------------

    def find_player(self):
        wanted = self.args.player.lower()
        for attempt in range(25):
            for element in self.plex.get("/clients"):
                if wanted in element.get("name", "").lower() or wanted == element.get("machineIdentifier"):
                    self.player = Player(element.get("host"), element.get("port"), element.get("machineIdentifier"))
                    self.say("player: %s at %s" % (element.get("name"), self.player.address))
                    return True
            if attempt == 0 and self.args.launch_url:
                self.say("player not listed by Plex; opening its Plex app")
                urlopen(Request(self.args.launch_url, data=b"", method="POST"), timeout=10).read()
            elif not self.args.launch_url:
                break
            time.sleep(1)
        return False

    def start(self):
        key = "/library/metadata/%s" % self.args.rating_key
        server = self.plex.get("/identity").get("machineIdentifier")
        item = self.plex.get(key)[0]
        self.before = (int(item.get("viewOffset") or 0), int(item.get("viewCount") or 0))
        self.duration = int(item.get("duration")) / 1000.0
        queue = self.plex.get("/playQueues", {
            "type": "video", "shuffle": 0, "repeat": 0, "continuous": 0, "own": 1,
            "uri": "server://%s/com.plexapp.plugins.library%s" % (server, key)}, method="POST")
        token = self.plex.get("/security/token", {"type": "delegation", "scope": "all"}).get("token")
        host, port = self.args.plex_address.rsplit(":", 1)
        self.player.send("/player/playback/playMedia", {
            "key": key, "offset": 0, "machineIdentifier": server, "address": host, "port": port,
            "protocol": "http", "type": "video", "token": token,
            "providerIdentifier": "com.plexapp.plugins.library",
            "containerKey": "/playQueues/%s?window=100&own=1" % queue.get("playQueueID")})
        began = time.time()
        while time.time() - began < 25 and self.player.video()[0] != "playing":
            time.sleep(0.5)
        self.check(self.player.video()[0] == "playing",
                   "playback started %.1f s after the command" % (time.time() - began))

    def run(self):
        if not self.find_player():
            self.check(False, "Plex lists no controllable player matching %r" % self.args.player)
            return
        state, _ = self.player.video()
        if state != "stopped":
            self.check(False, "the player is %s; not interrupting it" % state)
            return
        self.offset = self.saved_offset()
        self.say("saved offset for this player: %+d ms" % self.offset)
        self.started = False
        try:
            self.start()
            self.started = True
            self.steps()
        finally:
            self.finish()

    def steps(self):
        args = self.args
        time.sleep(3)
        session = self.session()
        if not self.check(bool(session and session["adjustable"]),
                          "the stream is adjustable" + ("" if session is None else " (%s)" % (session["reason"] or "yes"))):
            return
        sid = session["id"]
        self.check(session["offset_ms"] == self.offset and not session["pending"],
                   "the stream has the saved offset from its start (%+d ms)" % session["offset_ms"])

        if session["tuning"]:
            self.say("skip  pacing with the page closed: a page is open somewhere")
        else:
            idle, restarts, leads = self.watch(args.watch, tuning=False)
            self.check(not idle and not restarts and leads and leads[-1] <= args.normal_lead + 8,
                       "page closed, %d s: never stalled, no restart, ahead by %.1f / %.1f / %.1f s (min/median/max)"
                       % (args.watch, leads[0], leads[len(leads) // 2], leads[-1]))
        if args.closed_only:
            return

        self.watch(args.normal_lead + 6, tuning=True)  # time to come down
        idle, restarts, leads = self.watch(args.watch, tuning=True)
        self.check(not idle and not restarts and leads and leads[-1] <= args.lead + 4,
                   "page open, %d s: never stalled, no restart, ahead by %.1f / %.1f / %.1f s (min/median/max)"
                   % (args.watch, leads[0], leads[len(leads) // 2], leads[-1]))

        for value in (self.offset + 100, self.offset - 100, self.offset):
            self.service("/api/offset", {"session": sid, "offset_ms": value})
            began, predicted = time.time(), None
            while time.time() - began < 30:
                session = self.session()
                if predicted is None and session["pending"]:
                    predicted = session["eta_s"]
                if not session["pending"]:
                    break
                time.sleep(0.25)
            took = time.time() - began
            self.check(session["confirmed"] and not session["pending"] and took <= args.lead + 6
                       and self.player.video()[0] == "playing",
                       "offset %+d ms: accepted by the transcoder, heard after %.1f s (predicted %s s), still playing"
                       % (value, took, predicted))
            time.sleep(2)

        before = self.session()
        self.player.command("pause")
        time.sleep(15)
        paused = self.session()
        self.player.command("play")
        time.sleep(4)
        after = self.session()
        self.check(paused["state"] == "paused" and paused["lead_s"] - before["lead_s"] <= 4
                   and after["pid"] == before["pid"] and self.player.video()[0] == "playing",
                   "paused 15 s: transcoder went from %.1f to %.1f s ahead, same stream after resume"
                   % (before["lead_s"], paused["lead_s"]))

        _, position = self.player.video()
        target = position + 120
        if target < self.duration - 60:
            self.player.command("seekTo", offset=int(target * 1000))
            began = time.time()
            while time.time() - began < 20:
                session = self.session()
                if session and session["pid"] != before["pid"] and self.player.video()[0] == "playing":
                    break
                time.sleep(0.25)
            self.check(bool(session) and session["pid"] != before["pid"] and session["offset_ms"] == self.offset
                       and not session["pending"],
                       "seek ahead: new stream after %.1f s, with the saved offset from its start"
                       % (time.time() - began))
        else:
            self.say("skip  seek: the item is too short")

    def finish(self):
        """Stop playback and put everything back, whatever happened."""
        try:
            if self.started and self.player.video()[0] in ("playing", "paused"):
                self.player.command("stop")
                time.sleep(3)
            self.check(self.player.video()[0] == "stopped", "playback stopped")
        finally:
            if self.started:
                library = {"identifier": "com.plexapp.plugins.library", "key": self.args.rating_key}
                if self.before == (0, 0):
                    self.plex.get("/:/unscrobble", library)
                else:
                    self.plex.get("/:/progress", dict(library, time=self.before[0], state="stopped"))
            if self.saved_offset() != self.offset:
                self.service("/api/offset", {"client": self.player.client_id, "offset_ms": self.offset})
            self.check(self.saved_offset() == self.offset, "saved offset is back at %+d ms" % self.offset)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--player", required=True, help="part of the player's name, or its Plex client id")
    parser.add_argument("--rating-key", required=True, help="library item to play; pick one nobody is watching")
    parser.add_argument("--plex-url", default="http://127.0.0.1:32400")
    parser.add_argument("--plex-address", required=True,
                        help="host:port at which the player reaches Plex, for example 192.0.2.10:32400")
    parser.add_argument("--token-file", default=os.path.expanduser("~/.config/plex-token"))
    parser.add_argument("--service-url", default="http://127.0.0.1:8765")
    parser.add_argument("--launch-url", help="URL to POST to open the player's Plex app when Plex does not "
                        "list the player, for example http://<roku>:8060/launch/13535")
    parser.add_argument("--lead", type=float, default=3.0, help="the service's --lead")
    parser.add_argument("--normal-lead", type=float, default=10.0, help="the service's --normal-lead")
    parser.add_argument("--watch", type=int, default=45, help="seconds of steady playback per check")
    parser.add_argument("--closed-only", action="store_true",
                        help="stop after the check of pacing with the page closed. A page left open "
                             "anywhere keeps streams close to live; to test without that, run the "
                             "service with --tuning-seconds 0 for the duration")
    test = Test(parser.parse_args())
    test.run()
    print("%d check(s) failed" % test.failures if test.failures else "all checks passed")
    return 1 if test.failures else 0


if __name__ == "__main__":
    sys.exit(main())
