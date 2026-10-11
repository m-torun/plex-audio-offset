"""plex-audio-offset service.

Remembers an audio offset per player, drives the running Plex transcoders that
the shim prepared, and answers an HTTP API. It has no page of its own: an
interface such as plex-buddy talks to the API.

  GET  /api/state    streams, saved players, limits
  POST /api/offset   {"session": id | "client": id, "offset_ms": n}
  POST /api/forget   {"client": id}
  POST /api/tuning   {"session": id, "on": true|false}

Run with:  python3 -m plex_audio_offset.server
"""

import argparse
import json
import logging
import os
import re
import signal
import socketserver
import threading
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .shim import TAG, UNTHROTTLED, offset_expr, write_json

log = logging.getLogger("plex-audio-offset")

REALTIME = 1
SLOW = 0.1
PLEX_PREFS = ("/var/lib/plexmediaserver/Library/Application Support/"
              "Plex Media Server/Preferences.xml")
# How long a session's settings outlive its transcoder. Plex stops and starts
# the transcoder on every seek, so a session is briefly without one.
GRACE_SECONDS = 120
# A new transcoder gets ahead of its player within seconds, so it is watched
# closely for this long after it starts.
YOUNG_SECONDS = 15
# While tuning, a transcoder whose player has not reported yet may get this
# much further ahead than the tuning lead, so that playback can start.
START_MARGIN = 3.0
# A transcoder for a player that is not known yet starts at playback speed.
# This is how much of the stream it holds that pace for, at most.
HOLD_SECONDS = 6
# How long to keep saying a change is on its way when its arrival cannot be
# worked out (Plex normally keeps the transcoder about a minute ahead).
PENDING_SECONDS = 90
MAX_PREFIXES = 50
# Sessions this service starts itself to make Plex download a codec.
CODEC_SESSION = "plex-audio-offset-codec-"
CODEC_RETRY_SECONDS = 3600


class Plex:
    def __init__(self, url, token):
        self.url = url.rstrip("/")
        self.token = token

    def fetch(self, path, params=None, timeout=5):
        if params:
            path += "?" + urlencode(params)
        request = Request(self.url + path, headers={
            "X-Plex-Token": self.token, "Accept": "application/xml"})
        with urlopen(request, timeout=timeout) as response:
            return response.read()

    def get(self, path):
        return ET.fromstring(self.fetch(path))


class Playback:
    """One entry of Plex's /status/sessions."""

    def __init__(self, element, now):
        player = element.find("Player")
        session = element.find("Session")
        transcode = element.find("TranscodeSession")
        self.session_id = session.get("id", "") if session is not None else ""
        self.transcode_key = ""
        self.audio_decision = ""
        if transcode is not None:
            self.transcode_key = transcode.get("key", "").rsplit("/", 1)[-1]
            self.audio_decision = transcode.get("audioDecision", "")
        self.client_id = player.get("machineIdentifier", "")
        self.key = self.transcode_key or self.session_id or self.client_id
        self.rating_key = element.get("ratingKey", "")
        self.device = (player.get("title") or player.get("device")
                       or player.get("product") or "Unknown player")
        self.product = player.get("product", "")
        self.state = player.get("state", "")
        title = element.get("title", "")
        if element.get("grandparentTitle"):
            title = "%s: %s" % (element.get("grandparentTitle"), title)
        self.title = title
        self.duration_ms = int(element.get("duration") or 0)
        self.view_offset_ms = int(element.get("viewOffset") or 0)
        self.view_changed = now
        self.fresh = False  # set once a new position report has been seen arriving
        self.files = [part.get("file") for part in element.iter("Part") if part.get("file")]

    def playhead_ms(self, now):
        """Position now, estimated from the last report of the player.

        While playing, the estimate runs on from the last report. It can then
        only be too far along (the player may have stopped without saying so),
        never too far back, as long as the report's arrival was seen (fresh).
        """
        if self.state != "playing":
            return self.view_offset_ms
        return self.view_offset_ms + (now - self.view_changed) * 1000


class Proc:
    """A running transcoder that the shim prepared."""

    def __init__(self, path, record):
        self.path = path
        self.record = record
        self.pid = int(record["pid"])
        self.session = record["session"]
        self.fifo = record["fifo"]
        self.log = record["log"]
        self.source = record.get("source", "")
        self.started = float(record.get("started", 0))
        self.start = float(record.get("start", 0))  # media position it began at
        self.offset_ms = int(record.get("offset_ms", 0))
        self.held = bool(record.get("held"))
        self.speed = REALTIME if self.held else None  # None: not known yet
        self.log_pos = 0
        self.confirmed = True
        self.pending = None  # (media position of the last change or None, time sent)
        self.identified = False
        self.broken = False

    def alive(self):
        try:
            with open("/proc/%d/cmdline" % self.pid, "rb") as f:
                return self.session.encode() in f.read()
        except OSError:
            return False

    def command(self, target, name, value):
        """Send a filter command through the transcoder's stdin."""
        line = "c%s@%s -1 %s %s\n" % (target, TAG, name, value)
        fd = os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)

    def read_replies(self):
        try:
            with open(self.log, "rb") as f:
                f.seek(self.log_pos)
                data = f.read()
                self.log_pos = f.tell()
        except OSError:
            return
        if b"ret:0" in data:
            self.confirmed = True

    def set_offset(self, offset_ms):
        self.command("asetpts", "expr", offset_expr(offset_ms))
        self.offset_ms = offset_ms
        self.confirmed = False
        # Keep the record true, so a restarted service knows what is applied.
        self.record["offset_ms"] = offset_ms
        write_json(self.path, self.record)

    def remove_files(self):
        for path in (self.path, self.fifo, self.log):
            try:
                os.unlink(path)
            except OSError:
                pass


class Controller:
    def __init__(self, plex, run_dir, state_dir, max_offset_ms=2000, lead=3.0,
                 tuning_seconds=90, wait_ms=300, normal_lead=10.0, reencode_copied=True):
        self.plex = plex
        self.run_dir = run_dir
        self.offsets_path = os.path.join(state_dir, "offsets.json")
        self.max_offset_ms = max_offset_ms
        self.lead = lead
        self.normal_lead = normal_lead
        self.reencode_copied = reencode_copied
        self.tuning_seconds = tuning_seconds
        self.wait_ms = wait_ms
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.clients = {}    # player id -> {name, product, offset_ms, last_seen}
        self.prefixes = {}   # session id without its counter -> player id
        self.procs = {}      # pid -> Proc
        self.playbacks = {}  # session key -> Playback
        self.frontiers = {}  # transcode session -> seconds of media transcoded
        self.overrides = {}  # session -> offset, for sessions without a known player
        self.tuning = {}     # session -> time until which it is paced for tuning
        self.last_active = {}  # session -> time a transcoder was last seen
        self.shim_state = None
        self.plex_ok = True
        self.dirty = False
        self.wanted = []          # codec libraries the shim found missing
        self.codec_attempts = {}  # (directory, codec) -> time of the last fetch
        self.load()
        for name in ("sessions", "ctl", "log", "pending", "wanted"):
            os.makedirs(os.path.join(run_dir, name), exist_ok=True)

    # ---- persistence -------------------------------------------------

    def load(self):
        try:
            with open(self.offsets_path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        self.clients = data.get("clients", {})
        self.prefixes = data.get("prefixes", {})

    def save(self):
        if self.dirty:
            write_json(self.offsets_path, {"clients": self.clients, "prefixes": self.prefixes})
            self.dirty = False

    # ---- observing ---------------------------------------------------

    def scan_procs(self):
        """Pick up new shim records and drop transcoders that have exited."""
        changed = False
        directory = os.path.join(self.run_dir, "sessions")
        for name in os.listdir(directory):
            if not name.endswith(".json") or not name[:-5].isdigit():
                continue
            if int(name[:-5]) in self.procs:
                continue
            path = os.path.join(directory, name)
            try:
                with open(path) as f:
                    proc = Proc(path, json.load(f))
            except (OSError, ValueError, KeyError):
                continue
            self.procs[proc.pid] = proc
            changed = True
            log.info("session %s: transcoder %d started at %+d ms",
                     proc.session, proc.pid, proc.offset_ms)
        for pid, proc in list(self.procs.items()):
            if not proc.alive():
                del self.procs[pid]
                proc.remove_files()
                changed = True
                log.info("session %s: transcoder %d exited", proc.session, pid)
        return changed

    def markers(self, kind):
        """What the shim left in one of its marker directories, with paths."""
        markers = []
        directory = os.path.join(self.run_dir, kind)
        for name in os.listdir(directory):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(directory, name)) as f:
                    markers.append(dict(json.load(f), path=f.name))
            except (OSError, ValueError):
                continue
        return markers

    def pending_markers(self):
        """Sessions whose shim is waiting to learn which offset to start with."""
        return self.markers("pending")

    def check_codecs(self, now):
        """Get Plex to download codec libraries the shim found missing.

        Audio that Plex copies is decoded and encoded again so that it can be
        shifted. Plex downloads an encoder only when one of its own
        transcodes needs it, and into a folder that is new after every Plex
        upgrade, so the shim can find it absent. Starting a short transcode
        that needs it makes Plex fetch it.
        """
        self.wanted = self.markers("wanted")
        for marker in self.wanted:
            try:
                present = all(os.path.isfile(os.path.join(marker["directory"], name))
                              for name in marker["libraries"])
                if present:
                    os.unlink(marker["path"])
                    continue
                attempt = (marker["directory"], marker["codec"])
                playback = self.find_playback(marker["session"], marker["source"])
                if (playback is None or not playback.rating_key
                        or now - self.codec_attempts.get(attempt, -CODEC_RETRY_SECONDS) < CODEC_RETRY_SECONDS):
                    continue
                self.codec_attempts[attempt] = now
                threading.Thread(target=self.fetch_codec, args=(marker, playback.rating_key),
                                 daemon=True).start()
            except (KeyError, OSError):
                continue

    def fetch_codec(self, marker, rating_key):
        """Start and stop a transcode that makes Plex download marker's codec."""
        session = "%s%d" % (CODEC_SESSION, int(time.time()))
        name = "plex-audio-offset"
        # Plex picks the streaming profile from the platform and device; this
        # pair is one for which it encodes to the codec asked for below.
        params = {
            "path": "/library/metadata/%s" % rating_key, "mediaIndex": 0, "partIndex": 0,
            "protocol": "hls", "directPlay": 0, "directStream": 1, "directStreamAudio": 0,
            "location": "lan", "subtitles": "none", "session": session,
            "X-Plex-Session-Identifier": session, "X-Plex-Client-Identifier": name,
            "X-Plex-Product": name, "X-Plex-Device-Name": name,
            "X-Plex-Platform": "Roku", "X-Plex-Device": "Roku Ultra",
            "X-Plex-Client-Profile-Extra":
                "add-transcode-target-audio-codec(type=videoProfile&context=streaming"
                "&protocol=hls&audioCodec=%s)" % marker["codec"],
        }
        base = "/video/:/transcode/universal/"
        log.info("asking Plex to download its %s encoder", marker["codec"])
        try:
            self.plex.fetch(base + "decision", params, timeout=30)
            master = self.plex.fetch(base + "start.m3u8", params, timeout=30).decode()
            variant = [line for line in master.splitlines() if line and not line.startswith("#")][0]
            self.plex.fetch(base + variant, timeout=30)  # returns once the transcoder runs
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not all(
                    os.path.isfile(os.path.join(marker["directory"], library))
                    for library in marker["libraries"]):
                time.sleep(0.5)
        except (OSError, IndexError, ValueError) as error:
            log.warning("could not get Plex to download its %s encoder: %s", marker["codec"], error)
        finally:
            try:
                self.plex.fetch(base + "stop", {"session": session}, timeout=10)
            except OSError:
                pass

    def refresh_plex(self, now):
        try:
            root = self.plex.get("/status/sessions")
            frontiers = {}
            if self.procs:
                for element in self.plex.get("/transcode/sessions").iter("TranscodeSession"):
                    if element.get("maxOffsetAvailable"):
                        frontiers[element.get("key", "")] = float(element.get("maxOffsetAvailable"))
        except (OSError, ET.ParseError, ValueError) as error:
            if self.plex_ok:
                log.warning("cannot read sessions from Plex: %s", error)
            self.plex_ok = False
            return
        if not self.plex_ok:
            log.info("Plex is reachable again")
        self.plex_ok = True
        playbacks = {}
        for element in root:
            if element.find("Player") is None:
                continue
            playback = Playback(element, now)
            previous = self.playbacks.get(playback.key)
            if previous is not None:
                playback.fresh = True
                if (previous.view_offset_ms == playback.view_offset_ms
                        and previous.state == playback.state):
                    playback.view_changed = previous.view_changed
                    playback.fresh = previous.fresh
            playbacks[playback.key] = playback
        self.playbacks = playbacks
        self.frontiers = frontiers

    def find_playback(self, session, source):
        """The playback that a transcode session belongs to, if Plex shows it."""
        if session in self.playbacks:
            return self.playbacks[session]
        for playback in self.playbacks.values():
            if playback.session_id == session:
                return playback
        # Early in a session Plex lists the player before it lists the
        # transcode. The file being played still ties the two together.
        taken = set(proc.session for proc in self.procs.values() if proc.session != session)
        candidates = [playback for playback in self.playbacks.values()
                      if source and source in playback.files and playback.key not in taken]
        return candidates[0] if len(candidates) == 1 else None

    def client_for(self, playback):
        """The saved entry for a playback's player, created on first sight."""
        if playback is None or not playback.client_id:
            return None
        client = self.clients.get(playback.client_id)
        today = time.strftime("%Y-%m-%d")
        if client is None:
            client = {"offset_ms": 0}
            self.clients[playback.client_id] = client
        if (client.get("name"), client.get("product"), client.get("last_seen")) != (
                playback.device, playback.product, today):
            client.update(name=playback.device, product=playback.product, last_seen=today)
            self.dirty = True
        client["seen"] = int(time.time())  # saved along with the next real change
        return client

    def default_offset(self):
        """Offset for a session whose player is not known yet: that of the
        player seen last, which in a home is most often the one playing."""
        clients = sorted(self.clients.values(), key=lambda client: client.get("seen", 0))
        return clients[-1]["offset_ms"] if clients else 0

    # ---- acting ------------------------------------------------------

    def reconcile(self, now, markers=()):
        """Bring every running transcoder to the offset and pace it should have."""
        for playback in self.playbacks.values():
            self.client_for(playback)
        for proc in list(self.procs.values()):
            self.last_active[proc.session] = now
            proc.read_replies()
            playback = self.find_playback(proc.session, proc.source)
            client = self.client_for(playback)
            if proc.broken:
                continue
            desired = self.overrides.get(proc.session, proc.offset_ms)
            if client is not None:
                if not proc.identified:
                    proc.identified = True
                    self.learn_prefix(proc.session, playback.client_id)
                    log.info("session %s: player is %s, known %.0f ms after the transcoder started",
                             proc.session, playback.device, (time.time() - proc.started) * 1000)
                if proc.session in self.overrides:
                    client["offset_ms"] = self.overrides.pop(proc.session)
                    self.dirty = True
                desired = client["offset_ms"]
            try:
                if desired != proc.offset_ms:
                    proc.set_offset(desired)
                    frontier = self.frontiers.get(proc.session)
                    if frontier is None and time.time() - proc.started < 5:
                        frontier = proc.start  # nothing has been transcoded yet
                    proc.pending = (frontier, now)
                    log.info("session %s: offset %+d ms", proc.session, desired)
                speed = self.desired_speed(proc, playback, now)
                if speed != proc.speed:
                    proc.command("arealtime", "speed", "%g" % speed)
                    proc.speed = speed
            except OSError as error:
                proc.broken = True
                log.warning("session %s: cannot reach transcoder %d: %s",
                            proc.session, proc.pid, error)
            if proc.pending is not None and self.eta(proc, playback, now) == 0:
                proc.pending = None
        for table in (self.overrides, self.tuning, self.last_active):
            for session in list(table):
                if now - self.last_active.get(session, 0) > GRACE_SECONDS:
                    del table[session]
        self.write_shim_state(markers)
        self.save()

    def learn_prefix(self, session, client_id):
        """Remember which player a family of session ids belongs to.

        Some players number their sessions "<id>-1", "<id>-2", ... The shared
        part lets the shim pick the right offset before Plex lists the session.
        """
        prefix = re.sub(r"-\d+$", "", session)
        if prefix == session or self.prefixes.get(prefix) == client_id:
            return
        self.prefixes.pop(prefix, None)
        self.prefixes[prefix] = client_id
        while len(self.prefixes) > MAX_PREFIXES:
            del self.prefixes[next(iter(self.prefixes))]
        self.dirty = True

    def desired_speed(self, proc, playback, now):
        """Pace of the transcoder.

        Left alone it runs flat out and is soon minutes ahead of the player,
        and a change would take that long to be heard. So it is kept at
        playback speed once it is normal_lead ahead, and while someone is
        tuning it is slowed until it is only lead ahead.

        It is slowed below playback speed only when that cannot starve the
        player: while tuning with a position whose report was seen arriving,
        or while the player is paused.
        """
        if proc.held and not proc.identified and time.time() - proc.started < HOLD_SECONDS:
            return REALTIME
        frontier = self.frontiers.get(proc.session)
        tuning = self.tuning.get(proc.session, 0) > now
        target = self.lead if tuning else self.normal_lead
        if frontier is None or target <= 0:
            return UNTHROTTLED
        if playback is None or (playback.state == "playing" and not playback.fresh):
            # No usable position yet, as at the start of a session. The player
            # cannot be before where the transcoder began.
            position = max(proc.start, playback.view_offset_ms / 1000.0 if playback else 0)
            margin = START_MARGIN if tuning else 0
            return REALTIME if frontier - position >= target + margin else UNTHROTTLED
        lead = frontier - playback.playhead_ms(now) / 1000.0
        if playback.state == "paused":
            # The position is exact and nothing is being played: hold still.
            return SLOW if lead >= target else UNTHROTTLED
        if playback.state != "playing":
            return REALTIME if lead >= target else UNTHROTTLED  # buffering: let it fill
        if tuning:
            if lead > target + 1.5 or (proc.speed == SLOW and lead > target):
                return SLOW
            if lead < max(target - 2.0, 1.5):
                return UNTHROTTLED
            return REALTIME
        if lead >= target or (proc.speed == REALTIME and lead >= target - 5):
            return REALTIME
        return UNTHROTTLED

    def eta(self, proc, playback, now):
        """Seconds until the last offset change reaches the screen, or None."""
        if proc.pending is None:
            return 0
        frontier, sent = proc.pending
        if playback is None or frontier is None:
            return None if now - sent < PENDING_SECONDS else 0
        # The change lands in the segment being written, just past the frontier.
        return max(0.0, frontier + 1.0 - playback.playhead_ms(now) / 1000.0)

    def write_shim_state(self, markers=()):
        sessions = {}
        for proc in self.procs.values():
            sessions[proc.session] = proc.offset_ms
        for playback in self.playbacks.values():
            client = self.clients.get(playback.client_id)
            if client is not None:
                for key in (playback.transcode_key, playback.session_id):
                    if key:
                        sessions[key] = client["offset_ms"]
        for marker in markers:
            playback = self.find_playback(marker.get("session", ""), marker.get("source", ""))
            client = self.client_for(playback)
            if client is not None:
                sessions[marker["session"]] = client["offset_ms"]
        sessions.update(self.overrides)
        prefixes = {prefix: self.clients[client_id]["offset_ms"]
                    for prefix, client_id in self.prefixes.items() if client_id in self.clients}
        default = self.default_offset()
        # Only when players differ is there anything to wait for.
        wanted = any(client["offset_ms"] != default for client in self.clients.values())
        state = {"sessions": sessions, "prefixes": prefixes, "default_offset_ms": default,
                 "wait_unknown_ms": self.wait_ms if wanted else 0,
                 "hold_unknown_s": HOLD_SECONDS if wanted else 0,
                 "reencode_copied": self.reencode_copied}
        if state != self.shim_state:
            write_json(os.path.join(self.run_dir, "state.json"), state)
            self.shim_state = state

    def run(self):
        with open(os.path.join(self.run_dir, "enabled"), "w"):
            pass
        next_poll = 0
        while not self.stop.is_set():
            now = time.monotonic()
            with self.lock:
                changed = self.scan_procs()
                markers = self.pending_markers()
                if changed or markers or now >= next_poll:
                    self.refresh_plex(now)
                    if any(time.time() - proc.started < YOUNG_SECONDS
                           for proc in self.procs.values()):
                        next_poll = now + 0.15  # a new transcoder: find its player, catch it early
                    elif any(expiry > now for expiry in self.tuning.values()):
                        next_poll = now + 0.5
                    elif self.procs or self.playbacks:
                        next_poll = now + 1.0
                    else:
                        next_poll = now + 3.0
                    self.check_codecs(now)
                    self.reconcile(now, markers)
            self.stop.wait(0.1)
        self.shutdown()

    def shutdown(self):
        """Stop taking new sessions and let the running ones go at full speed."""
        with self.lock:
            try:
                os.unlink(os.path.join(self.run_dir, "enabled"))
            except OSError:
                pass
            for proc in self.procs.values():
                if proc.speed != UNTHROTTLED:
                    try:
                        proc.command("arealtime", "speed", "%g" % UNTHROTTLED)
                    except OSError:
                        pass

    # ---- API ---------------------------------------------------------

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            sessions = []
            shown = set()
            procs = {}
            for proc in sorted(self.procs.values(), key=lambda proc: proc.started):
                procs[proc.session] = proc  # the newest transcoder of a session wins
            for session, proc in procs.items():
                if session.startswith(CODEC_SESSION):
                    continue
                playback = self.find_playback(session, proc.source)
                client = self.clients.get(playback.client_id) if playback else None
                eta = self.eta(proc, playback, now)
                frontier = self.frontiers.get(session)
                entry = {
                    "id": session, "adjustable": not proc.broken,
                    "reason": "The transcoder for this stream stopped listening. Seek or "
                              "restart playback." if proc.broken else "",
                    "offset_ms": client["offset_ms"] if client else self.overrides.get(session, proc.offset_ms),
                    "tuning": self.tuning.get(session, 0) > now,
                    "eta_s": None if eta is None else round(eta, 1),
                    "pending": proc.pending is not None,
                    "confirmed": proc.confirmed,
                    "lead_s": None,
                    "pid": proc.pid,
                    "stream_start_s": proc.start,
                }
                if playback is not None:
                    shown.add(playback.key)
                    entry.update(self.describe(playback, now))
                    if frontier is not None:
                        entry["lead_s"] = round(frontier - playback.playhead_ms(now) / 1000.0, 1)
                else:
                    name = os.path.splitext(os.path.basename(proc.source))[0]
                    entry.update(client=None, device="Player not identified yet", product="",
                                 title=name, state="", position_ms=None, duration_ms=None)
                sessions.append(entry)
            for key, playback in self.playbacks.items():
                if key in shown:
                    continue
                client = self.clients.get(playback.client_id)
                if not playback.transcode_key:
                    reason = ("Direct Play: this player reads the file as it is, so there is no "
                              "audio to shift. To change that, turn off Allow Direct Play in "
                              "the Plex app's settings on this player, then start playback again.")
                elif any(marker.get("session") == key for marker in self.wanted):
                    reason = ("Plex did not have the encoder needed to shift this audio. It is "
                              "being fetched; seek or restart playback in a moment to pick it up.")
                elif playback.audio_decision == "copy":
                    reason = ("The audio is passed through unchanged, so it cannot be shifted. "
                              "If this stream began before the offset service did, seek or "
                              "restart playback to pick it up.")
                else:
                    reason = ("This stream is not under control. Seek or restart "
                              "playback to pick it up.")
                entry = {"id": key, "adjustable": False, "reason": reason,
                         "offset_ms": client["offset_ms"] if client else 0, "tuning": False,
                         "eta_s": 0, "pending": False, "confirmed": True, "lead_s": None,
                         "pid": None, "stream_start_s": None}
                entry.update(self.describe(playback, now))
                sessions.append(entry)
            active = set(playback.client_id for playback in self.playbacks.values())
            clients = [{"id": client_id, "name": client.get("name", client_id),
                        "product": client.get("product", ""), "offset_ms": client["offset_ms"],
                        "last_seen": client.get("last_seen", ""), "active": client_id in active}
                       for client_id, client in self.clients.items()]
            clients.sort(key=lambda client: client["last_seen"], reverse=True)
            return {"sessions": sessions, "clients": clients, "plex_ok": self.plex_ok,
                    "max_offset_ms": self.max_offset_ms}

    @staticmethod
    def describe(playback, now):
        return {"client": playback.client_id, "device": playback.device,
                "product": playback.product, "title": playback.title, "state": playback.state,
                "position_ms": int(playback.playhead_ms(now)), "duration_ms": playback.duration_ms}

    def set_offset(self, offset_ms, session=None, client_id=None):
        """Set the offset of a session's player, or of a saved player."""
        offset_ms = max(-self.max_offset_ms, min(self.max_offset_ms, int(round(offset_ms))))
        with self.lock:
            now = time.monotonic()
            if session is not None:
                procs = [proc for proc in self.procs.values() if proc.session == session]
                playback = self.find_playback(session, procs[0].source if procs else "")
                if self.client_for(playback) is not None:
                    client_id = playback.client_id
                elif procs:
                    self.overrides[session] = offset_ms
                else:
                    raise KeyError("unknown session")
                if procs:
                    self.tuning[session] = now + self.tuning_seconds
            if client_id is not None:
                if client_id not in self.clients:
                    raise KeyError("unknown player")
                self.clients[client_id]["offset_ms"] = offset_ms
                self.dirty = True
            self.reconcile(now)
        return offset_ms

    def forget(self, client_id):
        """Drop a saved player. One that is playing comes straight back at 0."""
        with self.lock:
            if client_id not in self.clients:
                raise KeyError("unknown player")
            del self.clients[client_id]
            self.prefixes = {prefix: owner for prefix, owner in self.prefixes.items()
                             if owner != client_id}
            self.dirty = True
            self.reconcile(time.monotonic())

    def set_tuning(self, session, on):
        with self.lock:
            now = time.monotonic()
            if on and any(proc.session == session for proc in self.procs.values()):
                self.tuning[session] = now + self.tuning_seconds
            else:
                self.tuning.pop(session, None)
            self.reconcile(now)


class Handler(BaseHTTPRequestHandler):
    controller = None

    def do_GET(self):
        if self.path == "/":
            self.reply_json(200, {"service": "plex-audio-offset", "interface": "plex-buddy"})
        elif self.path == "/api/state":
            self.reply_json(200, self.controller.snapshot())
        else:
            self.reply_json(404, {"error": "not found"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 4096:
                raise ValueError("request too large")
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/api/offset":
                offset = self.controller.set_offset(
                    float(body["offset_ms"]), session=body.get("session"),
                    client_id=body.get("client"))
                self.reply_json(200, {"offset_ms": offset})
            elif self.path == "/api/forget":
                self.controller.forget(str(body["client"]))
                self.reply_json(200, {})
            elif self.path == "/api/tuning":
                self.controller.set_tuning(str(body["session"]), bool(body.get("on")))
                self.reply_json(200, {})
            else:
                self.reply_json(404, {"error": "not found"})
        except KeyError as error:
            self.reply_json(404, {"error": str(error.args[0]) if error.args else "not found"})
        except (ValueError, TypeError) as error:
            self.reply_json(400, {"error": str(error)})

    def reply_json(self, status, data):
        self.reply(status, json.dumps(data).encode(), "application/json")

    def reply(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # HTTPServer looks up its own host name here, which can take seconds.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def read_token(args):
    if args.token_file:
        with open(args.token_file) as f:
            return f.read().strip()
    with open(args.plex_prefs) as f:
        match = re.search(r'PlexOnlineToken="([^"]+)"', f.read())
    if not match:
        raise SystemExit("no PlexOnlineToken in %s; pass --token-file" % args.plex_prefs)
    return match.group(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--listen", default="127.0.0.1",
                        help="address the API listens on; it has no login, so the default is this machine only")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--plex-url", default="http://127.0.0.1:32400")
    parser.add_argument("--plex-prefs", default=PLEX_PREFS,
                        help="Plex Preferences.xml to read the server token from")
    parser.add_argument("--token-file", help="file holding a Plex token, instead of --plex-prefs")
    parser.add_argument("--run-dir", default="/run/plex-audio-offset",
                        help="runtime directory shared with the transcoder shim")
    parser.add_argument("--state-dir", default=os.environ.get("STATE_DIRECTORY", "/var/lib/plex-audio-offset"))
    parser.add_argument("--max-offset-ms", type=int, default=2000)
    parser.add_argument("--lead", type=float, default=3.0,
                        help="seconds the transcoder stays ahead of the player while tuning")
    parser.add_argument("--normal-lead", type=float, default=10.0,
                        help="seconds the transcoder may get ahead of the player otherwise; "
                             "0 leaves that to Plex")
    parser.add_argument("--tuning-seconds", type=int, default=90,
                        help="how long a session stays paced for tuning after the last change "
                             "or the last sign that the page is open")
    parser.add_argument("--leave-copied-audio", action="store_true",
                        help="do not touch audio that Plex passes through unchanged; by default "
                             "it is decoded and encoded again in the same format (AC-3 only) "
                             "so that it can be shifted")
    parser.add_argument("--wait-ms", type=int, default=300,
                        help="longest a new transcoder waits to learn which player it serves")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    controller = Controller(Plex(args.plex_url, read_token(args)), args.run_dir, args.state_dir,
                            args.max_offset_ms, args.lead, args.tuning_seconds, args.wait_ms,
                            args.normal_lead, not args.leave_copied_audio)
    Handler.controller = controller
    server = Server((args.listen, args.port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: controller.stop.set())
    signal.signal(signal.SIGINT, lambda *_: controller.stop.set())
    log.info("API on http://%s:%d", args.listen, args.port)
    controller.run()


if __name__ == "__main__":
    main()
