import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from plex_audio_offset import server, shim

FILE = "/media/Show (2020)/Show (2020) - S01E01 - A; B.ts"


class FakePlex:
    def __init__(self):
        self.sessions = []
        self.frontiers = {}
        self.fetched = []
        self.on_start = lambda: None
        self.clients = []  # players Plex can command: (id, host, port)

    def get(self, path):
        root = ET.Element("MediaContainer")
        if path == "/status/sessions":
            for session in self.sessions:
                video = ET.SubElement(root, "Video", title="Pilot", grandparentTitle="Show",
                                      ratingKey="77", duration="3600000",
                                      viewOffset=str(session["view"]))
                ET.SubElement(ET.SubElement(video, "Media"), "Part", file=session.get("file", FILE))
                ET.SubElement(video, "Player", machineIdentifier=session["client"],
                              title=session.get("device", "Living Room"), product="Plex for Roku",
                              state=session.get("state", "playing"))
                ET.SubElement(video, "Session", id=session.get("session_id", "s1"))
                if session.get("transcode"):
                    ET.SubElement(video, "TranscodeSession",
                                  key="/transcode/sessions/" + session["transcode"],
                                  audioDecision=session.get("audio", "transcode"))
        elif path == "/clients":
            for client_id, host, port in self.clients:
                ET.SubElement(root, "Server", machineIdentifier=client_id, host=host, port=str(port),
                              protocolCapabilities="timeline,playback,navigation,playqueues")
        else:
            for key, value in self.frontiers.items():
                ET.SubElement(root, "TranscodeSession", key=key, maxOffsetAvailable=str(value))
        return root

    def fetch(self, path, params=None, timeout=5):
        self.fetched.append((path.rsplit("/", 1)[-1], params))
        if path.endswith("start.m3u8"):
            self.on_start()
            return b"#EXTM3U\nsession/x/base/index.m3u8\n"
        return b""


class FakeRemote:
    """A player that answers remote-control requests."""

    def __init__(self, state="playing", time_ms=305500, rating_key="77"):
        self.answer = {"state": state, "time_ms": time_ms, "rating_key": rating_key}
        self.seeks = []

    def timeline(self, address, client_id):
        return dict(self.answer)

    def seek(self, address, client_id, offset_ms):
        self.seeks.append((address, client_id, offset_ms))


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.run_dir = os.path.join(self.tmp, "run")
        self.state_dir = os.path.join(self.tmp, "state")
        os.makedirs(self.run_dir)
        os.makedirs(self.state_dir)
        self.plex = FakePlex()
        self.alive = set()
        self.readers = {}
        patcher = mock.patch.object(server.Proc, "alive", lambda proc: proc.pid in self.alive)
        patcher.start()
        self.addCleanup(patcher.stop)

    def controller(self, clients=None):
        if clients:
            shim.write_json(os.path.join(self.state_dir, "offsets.json"),
                            {"clients": clients, "prefixes": {}})
        self.ctl = server.Controller(self.plex, self.run_dir, self.state_dir)
        return self.ctl

    def spawn(self, pid, session, offset_ms=0, source=FILE, held=False):
        """What the shim leaves behind for a transcoder it started."""
        fifo = os.path.join(self.run_dir, "ctl", "%d.fifo" % pid)
        log = os.path.join(self.run_dir, "log", "%d.log" % pid)
        os.mkfifo(fifo)
        self.readers[pid] = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, self.readers[pid])
        open(log, "w").close()
        shim.write_json(os.path.join(self.run_dir, "sessions", "%d.json" % pid), {
            "pid": pid, "session": session, "fifo": fifo, "log": log, "offset_ms": offset_ms,
            "start": 300.0, "source": source, "format": "ssegment", "cwd": "/",
            "started": time.time(), "held": held})
        self.alive.add(pid)

    def step(self):
        now = time.monotonic()
        with self.ctl.lock:
            self.ctl.scan_procs()
            markers = self.ctl.pending_markers()
            self.ctl.refresh_plex(now)
            self.ctl.reconcile(now, markers)

    def commands(self, pid):
        try:
            return os.read(self.readers[pid], 65536).decode().splitlines()
        except BlockingIOError:
            return []

    def shim_state(self):
        with open(os.path.join(self.run_dir, "state.json")) as f:
            return json.load(f)

    def test_a_known_player_gets_its_saved_offset(self):
        self.controller({"roku": {"offset_ms": 120}})
        self.plex.sessions = [{"client": "roku", "view": 300000, "transcode": "abc-7", "session_id": "abc-7"}]
        self.spawn(11, "abc-7")
        self.step()
        self.assertEqual(self.commands(11), ["casetpts@pao -1 expr PTS+(0.120)/TB",
                                             "carealtime@pao -1 speed 1000"])
        with open(os.path.join(self.run_dir, "sessions", "11.json")) as f:
            self.assertEqual(json.load(f)["offset_ms"], 120)
        state = self.shim_state()
        self.assertEqual(state["sessions"]["abc-7"], 120)
        self.assertEqual(state["prefixes"], {"abc": 120})
        self.assertEqual(state["wait_unknown_ms"], 300)
        self.assertEqual(state["hold_unknown_s"], 4)
        self.step()
        self.assertEqual(self.commands(11), [])

    def test_a_held_transcoder_is_released_once_its_player_is_known(self):
        self.controller({"roku": {"offset_ms": 120}})
        self.spawn(11, "new-1", held=True)
        self.step()
        self.assertEqual(self.commands(11), [])  # still at playback speed, nothing to send
        self.plex.sessions = [{"client": "roku", "view": 300000, "transcode": "new-1"}]
        self.step()
        self.assertEqual(self.commands(11), ["casetpts@pao -1 expr PTS+(0.120)/TB",
                                             "carealtime@pao -1 speed 1000"])

    def test_nothing_to_wait_for_without_saved_offsets(self):
        self.controller()
        self.step()
        self.assertEqual(self.shim_state()["wait_unknown_ms"], 0)

    def test_adjusting_a_session_saves_applies_and_paces(self):
        self.controller()
        session = {"client": "roku", "view": 300000, "transcode": "abc-7"}
        self.plex.sessions = [session]
        self.plex.frontiers = {"abc-7": 360.0}
        self.spawn(11, "abc-7")
        self.step()
        self.assertEqual(self.commands(11), ["carealtime@pao -1 speed 1"])  # a minute ahead: enough
        session["view"] = 300100  # a position report is seen arriving
        self.step()
        self.assertEqual(self.commands(11), [])

        self.assertEqual(self.ctl.set_offset(50, session="abc-7"), 50)
        self.assertEqual(self.commands(11), ["casetpts@pao -1 expr PTS+(0.050)/TB",
                                             "carealtime@pao -1 speed 0.1"])
        with open(os.path.join(self.state_dir, "offsets.json")) as f:
            self.assertEqual(json.load(f)["clients"]["roku"]["offset_ms"], 50)
        (entry,) = self.ctl.snapshot()["sessions"]
        self.assertTrue(entry["adjustable"] and entry["tuning"] and entry["pending"])
        self.assertEqual(entry["offset_ms"], 50)
        self.assertFalse(entry["confirmed"])
        self.assertAlmostEqual(entry["eta_s"], 60.9, delta=1)

        with open(os.path.join(self.run_dir, "log", "11.log"), "w") as f:
            f.write("Command reply for stream 1: ret:0 res:\n")
        session["view"] = 355000  # the player has nearly caught up: hold it there
        self.step()
        self.assertEqual(self.commands(11), ["carealtime@pao -1 speed 1"])
        self.assertTrue(self.ctl.snapshot()["sessions"][0]["confirmed"])
        session["view"] = 358500  # too close: let the transcoder run ahead again
        self.step()
        self.assertEqual(self.commands(11), ["carealtime@pao -1 speed 1000"])
        session["view"] = 362000  # past the change
        self.step()
        self.assertFalse(self.ctl.snapshot()["sessions"][0]["pending"])

        self.ctl.set_tuning("abc-7", False)
        self.assertFalse(self.ctl.snapshot()["sessions"][0]["tuning"])

    def test_outside_tuning_the_transcoder_is_never_slowed_below_playback_speed(self):
        self.controller()
        session = {"client": "roku", "view": 300000, "transcode": "abc-7"}
        self.plex.sessions = [session]
        self.plex.frontiers = {"abc-7": 310.0}
        self.spawn(11, "abc-7")
        self.step()
        session["view"] = 300100
        self.step()
        self.assertEqual(self.commands(11), ["carealtime@pao -1 speed 1000"])  # 10 s ahead: run
        self.plex.frontiers["abc-7"] = 400.0
        self.step()
        self.assertEqual(self.commands(11), ["carealtime@pao -1 speed 1"])     # far ahead: hold
        self.plex.frontiers["abc-7"] = 317.0
        self.step()
        self.assertEqual(self.commands(11), [])                                # still within 5 s of 20
        self.plex.frontiers["abc-7"] = 312.0
        self.step()
        self.assertEqual(self.commands(11), ["carealtime@pao -1 speed 1000"])  # too close: run

    def test_before_a_position_report_the_transcoder_is_capped_but_never_slowed(self):
        self.controller()
        session = {"client": "roku", "view": 300000, "transcode": "abc-7"}
        self.plex.sessions = [session]
        self.plex.frontiers = {"abc-7": 306.0}
        self.spawn(11, "abc-7")  # the transcoder began at 300 s
        self.step()
        self.assertEqual(self.commands(11), ["carealtime@pao -1 speed 1000"])
        self.ctl.set_tuning("abc-7", True)  # the page is open
        self.assertEqual(self.commands(11), [])  # 6 s ahead: playback still has to start
        self.plex.frontiers["abc-7"] = 308.5
        self.step()  # still no new position report
        self.assertEqual(self.commands(11), ["carealtime@pao -1 speed 1"])
        self.plex.frontiers["abc-7"] = 400.0
        self.step()
        self.assertEqual(self.commands(11), [])  # however far ahead: not below playback speed

    def test_a_paused_player_holds_the_transcoder(self):
        self.controller()
        session = {"client": "roku", "view": 300000, "transcode": "abc-7"}
        self.plex.sessions = [session]
        self.plex.frontiers = {"abc-7": 310.0}
        self.spawn(11, "abc-7")
        self.step()
        session.update(view=305000, state="paused")
        self.step()
        self.assertEqual(self.commands(11), ["carealtime@pao -1 speed 1000"])  # 5 s ahead: fill up
        self.plex.frontiers["abc-7"] = 326.0
        self.step()
        self.assertEqual(self.commands(11), ["carealtime@pao -1 speed 0.1"])   # 21 s ahead: hold
        self.ctl.set_offset(-50, session="abc-7")
        (entry,) = self.ctl.snapshot()["sessions"]
        self.assertEqual((entry["state"], entry["pending"]), ("paused", True))
        self.assertAlmostEqual(entry["eta_s"], 22, delta=0.5)  # does not count down while paused
        self.commands(11)
        session.update(view=305000, state="playing")
        self.step()
        self.assertEqual(self.commands(11), [])  # tuning: stays slow until it is 5 s ahead
        session.update(state="buffering")
        self.plex.frontiers["abc-7"] = 307.0
        self.step()
        self.assertEqual(self.commands(11), ["carealtime@pao -1 speed 1000"])  # starved: let it fill

    def reloading(self, **timeline):
        """A session on a player that Plex can command, with the page not open."""
        self.controller()
        self.ctl.apply_delay = 0.01
        self.ctl.remote = FakeRemote(**timeline)
        self.plex.clients = [("roku", "192.0.2.7", 8324)]
        self.session = {"client": "roku", "view": 300000, "transcode": "abc-7"}
        self.plex.sessions = [self.session]
        self.plex.frontiers = {"abc-7": 360.0}
        self.spawn(11, "abc-7")
        self.step()
        self.commands(11)

    def change(self, offset_ms):
        self.ctl.set_offset(offset_ms, session="abc-7")
        timer = self.ctl.timers.get("abc-7")
        if timer is not None:
            timer.join(5)

    def test_a_change_makes_the_player_reload_the_stream_where_it_is(self):
        self.reloading()
        self.ctl.set_offset(20, session="abc-7")
        self.assertTrue(self.ctl.snapshot()["sessions"][0]["applying"])
        self.change(50)  # a second tap soon after: still one reload
        self.assertEqual(self.ctl.remote.seeks, [(("192.0.2.7", "8324"), "roku", 304500)])
        self.assertTrue(self.ctl.snapshot()["sessions"][0]["applying"])

        self.alive.discard(11)        # Plex replaces the transcoder
        self.spawn(12, "abc-7", offset_ms=50)
        self.step()
        (entry,) = self.ctl.snapshot()["sessions"]
        self.assertEqual((entry["applying"], entry["instant"], entry["pending"], entry["offset_ms"]),
                         (False, True, False, 50))
        self.assertEqual(self.shim_state()["sessions"]["abc-7"], 50)
        # Such a player needs no waiting game: the transcoder is not slowed for it.
        self.session["view"] = 300100
        self.step()
        self.assertNotIn("carealtime@pao -1 speed 0.1", self.commands(12))

    def test_a_player_that_is_not_playing_the_item_is_sent_nothing(self):
        for timeline in ({"state": "stopped", "time_ms": None}, {"state": "buffering"},
                         {"rating_key": "999"}):
            with self.subTest(timeline):
                self.reloading(**timeline)
                self.change(50)
                self.assertEqual(self.ctl.remote.seeks, [])
                self.assertFalse(self.ctl.snapshot()["sessions"][0]["applying"])
                self.alive.clear()
                self.readers.clear()
                shutil.rmtree(self.run_dir)
                os.makedirs(self.run_dir)

    def test_a_player_plex_cannot_command_gets_the_change_with_the_stream(self):
        self.reloading()
        self.plex.clients = []
        self.ctl.players = {}
        self.change(50)
        self.assertEqual(self.ctl.remote.seeks, [])
        (entry,) = self.ctl.snapshot()["sessions"]
        self.assertTrue(entry["pending"] and not entry["applying"])

    def test_a_player_that_does_not_reload_is_not_asked_again(self):
        self.reloading()
        self.change(50)
        self.assertEqual(len(self.ctl.remote.seeks), 1)
        with self.ctl.lock:
            self.ctl.reconcile(time.monotonic() + 9)  # no new transcoder showed up
        self.assertIs(self.ctl.instant["roku"], False)
        self.change(80)
        self.assertEqual(len(self.ctl.remote.seeks), 1)
        self.assertFalse(self.ctl.snapshot()["sessions"][0]["applying"])

    def test_offsets_are_clamped(self):
        self.controller({"roku": {"offset_ms": 0}})
        self.assertEqual(self.ctl.set_offset(99999, client_id="roku"), 2000)
        self.assertEqual(self.ctl.set_offset(-99999, client_id="roku"), -2000)
        with self.assertRaises(KeyError):
            self.ctl.set_offset(10, client_id="nobody")
        with self.assertRaises(KeyError):
            self.ctl.set_offset(10, session="nothing")

    def test_a_session_adjusted_before_its_player_is_known(self):
        self.controller()
        self.spawn(11, "web-session")
        self.step()
        self.commands(11)
        self.ctl.set_offset(-30, session="web-session")
        self.assertEqual(self.commands(11), ["casetpts@pao -1 expr PTS+(-0.030)/TB"])
        (entry,) = self.ctl.snapshot()["sessions"]
        self.assertIsNone(entry["client"])
        self.assertEqual(entry["offset_ms"], -30)
        self.assertEqual(self.shim_state()["sessions"]["web-session"], -30)

        # Plex now lists the player, tied to the session only by the file.
        self.plex.sessions = [{"client": "mac", "view": 1000, "session_id": "other"}]
        self.step()
        self.assertEqual(self.commands(11), [])
        self.assertEqual(self.ctl.clients["mac"]["offset_ms"], -30)
        self.assertEqual(self.ctl.snapshot()["sessions"][0]["client"], "mac")

    def test_a_waiting_shim_is_told_the_offset_of_its_player(self):
        self.controller({"roku": {"offset_ms": 200}})
        self.plex.sessions = [{"client": "roku", "view": 0, "session_id": "zzz"}]
        shim.write_json(os.path.join(self.run_dir, "pending", "new-1.json"),
                        {"session": "new-1", "source": FILE})
        self.step()
        self.assertEqual(self.shim_state()["sessions"]["new-1"], 200)

    def test_direct_play_is_listed_but_cannot_be_adjusted(self):
        self.controller()
        self.plex.sessions = [{"client": "tv", "view": 5000}]
        self.step()
        (entry,) = self.ctl.snapshot()["sessions"]
        self.assertFalse(entry["adjustable"])
        self.assertIn("Direct Play", entry["reason"])
        # The player can still be given an offset for later.
        self.ctl.set_offset(40, session=entry["id"])
        self.assertEqual(self.ctl.clients["tv"]["offset_ms"], 40)

    def test_an_exited_transcoder_is_forgotten(self):
        self.controller()
        self.spawn(11, "abc-7")
        self.step()
        self.alive.clear()
        self.step()
        self.assertEqual(self.ctl.procs, {})
        self.assertEqual(os.listdir(os.path.join(self.run_dir, "sessions")), [])
        self.assertEqual(os.listdir(os.path.join(self.run_dir, "ctl")), [])

    def test_a_restarted_transcoder_continues_with_the_session_offset(self):
        self.controller()
        self.spawn(11, "web-session")
        self.step()
        self.ctl.set_offset(70, session="web-session")
        self.alive.clear()  # Plex stops the transcoder for a seek
        self.step()
        self.assertEqual(self.shim_state()["sessions"]["web-session"], 70)

    def test_a_missing_encoder_is_fetched_through_plex_once(self):
        self.controller()
        codecs = os.path.join(self.tmp, "codecs")
        os.makedirs(codecs)
        self.plex.sessions = [{"client": "roku", "view": 1000, "transcode": "abc-7", "audio": "copy"}]
        self.plex.on_start = lambda: open(os.path.join(codecs, "libac3_encoder.so"), "w").close()
        marker = os.path.join(self.run_dir, "wanted", "ac3.json")
        shim.write_json(marker, {"codec": "ac3", "libraries": ["libac3_encoder.so"],
                                 "directory": codecs, "session": "abc-7", "source": FILE})
        now = time.monotonic()
        with self.ctl.lock:
            self.ctl.refresh_plex(now)
            self.ctl.check_codecs(now)
            self.ctl.check_codecs(now + 1)  # no second attempt while the first is recent
        for thread in threading.enumerate():
            if thread is not threading.current_thread() and thread.daemon:
                thread.join(10)
        self.assertEqual([name for name, params in self.plex.fetched],
                         ["decision", "start.m3u8", "index.m3u8", "stop"])
        params = self.plex.fetched[0][1]
        self.assertEqual(params["path"], "/library/metadata/77")
        self.assertEqual(params["directStreamAudio"], 0)
        self.assertIn("audioCodec=ac3", params["X-Plex-Client-Profile-Extra"])
        (entry,) = self.ctl.snapshot()["sessions"]
        self.assertIn("being fetched", entry["reason"])
        with self.ctl.lock:
            self.ctl.check_codecs(now + 2)  # the library is there now: the marker goes
        self.assertFalse(os.path.exists(marker))

    def test_plex_being_down_is_survived(self):
        self.controller()
        self.plex.get = mock.Mock(side_effect=OSError("refused"))
        self.step()
        self.assertFalse(self.ctl.snapshot()["plex_ok"])


class RemoteTest(unittest.TestCase):
    def test_it_speaks_to_a_player_the_way_plex_apps_do(self):
        seen = []

        class Player(server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers["X-Plex-Target-Client-Identifier"],
                             self.headers["X-Plex-Client-Identifier"]))
                body = (b'<MediaContainer><Timeline type="music" state="stopped"/>'
                        b'<Timeline type="video" state="playing" time="82000" ratingKey="2209"/>'
                        b'</MediaContainer>')
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        httpd = server.Server(("127.0.0.1", 0), Player)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        remote = server.Remote()
        address = ("127.0.0.1", httpd.server_address[1])
        self.assertEqual(remote.timeline(address, "roku"),
                         {"state": "playing", "time_ms": 82000, "rating_key": "2209"})
        remote.seek(address, "roku", 81000.4)
        (poll, target, sender), (seek, _, _) = seen
        self.assertRegex(poll, r"^/player/timeline/poll\?wait=0&commandID=\d+$")
        self.assertRegex(seek, r"^/player/playback/seekTo\?type=video&offset=81000&commandID=\d+$")
        self.assertEqual((target, sender), ("roku", "plex-audio-offset"))
        self.assertGreater(int(seek.rsplit("=", 1)[1]), int(poll.rsplit("=", 1)[1]))


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        os.makedirs(os.path.join(self.tmp, "state"))
        shim.write_json(os.path.join(self.tmp, "state", "offsets.json"),
                        {"clients": {"roku": {"offset_ms": 10, "name": "Living Room"}}})
        server.Handler.controller = server.Controller(
            FakePlex(), os.path.join(self.tmp, "run"), os.path.join(self.tmp, "state"))
        server.Handler.web_dir = os.path.join(os.path.dirname(server.__file__), "web")
        self.httpd = server.Server(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def post(self, path, body):
        request = Request(self.base + path, data=body, headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def test_page_state_and_offset(self):
        with urlopen(self.base + "/", timeout=5) as response:
            self.assertIn(b"Audio offset", response.read())
        self.assertEqual(self.post("/api/offset", b'{"client": "roku", "offset_ms": 35}'),
                         {"offset_ms": 35})
        with urlopen(self.base + "/api/state", timeout=5) as response:
            state = json.load(response)
        self.assertEqual(state["clients"][0]["offset_ms"], 35)
        self.assertEqual(state["sessions"], [])

    def test_forget(self):
        self.assertEqual(self.post("/api/forget", b'{"client": "roku"}'), {})
        with urlopen(self.base + "/api/state", timeout=5) as response:
            self.assertEqual(json.load(response)["clients"], [])

    def test_bad_requests(self):
        for path, body, status in (("/api/offset", b'{"session": "x", "offset_ms": 5}', 404),
                                   ("/api/offset", b"not json", 400),
                                   ("/api/offset", b'{"client": "roku"}', 404),
                                   ("/api/forget", b'{"client": "nobody"}', 404),
                                   ("/api/nothing", b"{}", 404)):
            with self.subTest(body=body):
                with self.assertRaises(HTTPError) as caught:
                    self.post(path, body)
                self.assertEqual(caught.exception.code, status)
                caught.exception.close()


if __name__ == "__main__":
    unittest.main()
