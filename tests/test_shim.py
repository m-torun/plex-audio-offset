import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from plex_audio_offset import shim
from tests import fixtures

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INJECTED = (fixtures.AUDIO_CHAIN[:-3] +
            ",asetpts@pao=PTS+(0.120)/TB,aresample@pao=async=1:min_hard_comp=0.001,"
            "atrim=start=300.000000,arealtime@pao=speed=1000[4]")

REENCODED = ("[0:#0x101] aresample=async=1,asetpts@pao=PTS+(0.000)/TB,"
             "aresample@pao=async=1:min_hard_comp=0.001,atrim=start=300.000000,"
             "arealtime@pao=speed=1000[pao]")

# Stands in for the real transcoder: lists a file's streams when asked to,
# and otherwise reports how it was started.
FAKE_REAL = """#!%s
import json, os, stat, sys
if sys.argv[1:3] == ["-hide_banner", "-i"]:
    sys.stderr.write(%r)
    sys.exit(1)
with open(os.environ["FAKE_REAL_OUT"], "w") as f:
    json.dump({"args": sys.argv[1:], "stdin_is_fifo": stat.S_ISFIFO(os.fstat(0).st_mode)}, f)
""" % (sys.executable, fixtures.PROBE_OUTPUT)


def always(path):
    return True


class InspectTest(unittest.TestCase):
    def test_hls_and_dash_playback_are_recognised(self):
        for build in (fixtures.hls, fixtures.dash):
            args = build("/media/Show (2020) - S01E01 - A; B.ts")
            info = shim.inspect(args, isfile=always)
            self.assertEqual(info["session"], fixtures.SESSION)
            self.assertEqual(info["start"], 300.0)
            self.assertEqual(args[info["chain"]], fixtures.AUDIO_CHAIN)

    def test_inject_appends_to_the_audio_chain_only(self):
        self.assertEqual(shim.inject(fixtures.AUDIO_CHAIN, 120, 300.0), INJECTED)

    def test_copied_audio_is_found_and_encoded_again(self):
        args = fixtures.hls_copied_audio("/media/show.ts")
        info = shim.inspect(args, isfile=always)
        self.assertIsNone(info["chain"])
        new = shim.reencode(args, info, [fixtures.AC3_STREAM, dict(fixtures.AC3_STREAM, index=2, id=0x102)],
                            available=lambda names: True)
        at = new.index("-filter_complex", new.index("-filter_complex") + 1)
        self.assertEqual(new[at:at + 10], [
            "-filter_complex", "[0:#0x101] aresample=async=1[pao]", "-map", "[pao]",
            "-metadata:s:1", "language=eng", "-codec:1", "ac3", "-b:1", "640k"])
        self.assertEqual(new[info["chain"]], "[0:#0x101] aresample=async=1[pao]")
        self.assertNotIn("-copypriorss:1", new)
        self.assertEqual(len(new), len(args) + 4 - 2)
        self.assertEqual(args, fixtures.hls_copied_audio("/media/show.ts"))  # input untouched

    def test_copied_audio_that_cannot_be_encoded_again_is_left(self):
        args = fixtures.hls_copied_audio("/media/show.ts")
        cases = {
            "encoder not downloaded": ([fixtures.AC3_STREAM], lambda names: False),
            "another codec": ([dict(fixtures.AC3_STREAM, codec="eac3")], lambda names: True),
            "stream not found": ([dict(fixtures.AC3_STREAM, id=0x1ff, index=7)], lambda names: True),
        }
        for name, (streams, available) in cases.items():
            with self.subTest(name):
                with self.assertRaises(shim.Skip):
                    shim.reencode(args, shim.inspect(args, isfile=always), streams, available)

    def test_stereo_gets_the_stereo_bitrate(self):
        args = fixtures.hls_copied_audio("/media/show.ts")
        new = shim.reencode(args, shim.inspect(args, isfile=always),
                            [dict(fixtures.AC3_STREAM, layout="stereo")], lambda names: True)
        self.assertEqual(new[new.index("-b:1") + 1], "256k")

    def test_stream_listing_is_parsed(self):
        with mock.patch.object(shim.subprocess, "run") as run:
            run.return_value.stderr = fixtures.PROBE_OUTPUT.encode()
            streams = shim.probe_audio("/real", "/media/show.ts")
        self.assertEqual(streams, [fixtures.AC3_STREAM,
                                   {"index": 2, "id": 0x102, "codec": "ac3", "layout": "stereo"}])

    def test_everything_else_is_skipped(self):
        source = "/media/show.ts"
        cases = {
            "DVR grab": fixtures.DVR_GRAB,
            "DVR write": fixtures.DVR_WRITE,
            "conversion to a file": fixtures.convert(source),
            "second input": ["-i", "/media/other.ts"] + fixtures.hls(source),
            "copied audio": [arg for arg in fixtures.hls(source) if arg != fixtures.AUDIO_CHAIN],
            "already injected": [INJECTED if arg == fixtures.AUDIO_CHAIN else arg
                                 for arg in fixtures.hls(source)],
            "branching audio graph": [
                "[0:#0x101] aresample=async=1,asplit[a][b]" if arg == fixtures.AUDIO_CHAIN else arg
                for arg in fixtures.hls(source)],
        }
        for name, args in cases.items():
            with self.subTest(name):
                with self.assertRaises(shim.Skip):
                    shim.inspect(args, isfile=always)
        with self.assertRaises(shim.Skip):
            shim.inspect(fixtures.hls(source), isfile=lambda path: False)

    def test_inject_can_start_held_at_playback_speed(self):
        chain = shim.inject(fixtures.AUDIO_CHAIN, 0, 300.0, hold_seconds=4)
        self.assertTrue(chain.endswith(
            ",atrim=start=300.000000,asendcmd=c='304.000 arealtime@pao speed 1000',"
            "arealtime@pao=speed=1[4]"), chain)

    def test_start_is_zero_without_copyts(self):
        args = [arg for arg in fixtures.hls("/media/show.ts") if arg != "-copyts"]
        self.assertEqual(shim.inspect(args, isfile=always)["start"], 0.0)

    def test_lookup_prefers_the_session_then_its_family(self):
        state = {"sessions": {"abc-3": -40}, "prefixes": {"abc": 150}}
        self.assertEqual(shim.lookup(state, "abc-3"), -40)
        self.assertEqual(shim.lookup(state, "abc-4"), 150)
        self.assertIsNone(shim.lookup(state, "other"))
        self.assertIsNone(shim.lookup({"prefixes": {"plain": 5}}, "plain"))


class WrapperTest(unittest.TestCase):
    """The installed wrapper and the shim together, with a stand-in transcoder."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.run_dir = os.path.join(self.tmp, "run")
        for name in ("sessions", "ctl", "log", "pending", "wanted"):
            os.makedirs(os.path.join(self.run_dir, name))
        open(os.path.join(self.run_dir, "enabled"), "w").close()
        self.codecs = os.path.join(self.tmp, "Codecs dir")
        os.makedirs(self.codecs)
        for name in ("libac3_decoder.so", "libac3_encoder.so"):
            open(os.path.join(self.codecs, name), "w").close()
        self.source = os.path.join(self.tmp, "Some Show (1975) - S52E02 - A Guest; A Band.ts")
        open(self.source, "w").close()
        self.real = os.path.join(self.tmp, "Plex Transcoder.real")
        with open(self.real, "w") as f:
            f.write(FAKE_REAL)
        os.chmod(self.real, 0o755)
        with open(os.path.join(ROOT, "packaging", "transcoder-wrapper.sh.in")) as f:
            text = f.read()
        for key, value in (("@REAL@", self.real), ("@RUN_DIR@", self.run_dir),
                           ("@PYTHON@", sys.executable),
                           ("@SHIM@", os.path.join(ROOT, "plex_audio_offset", "shim.py"))):
            text = text.replace(key, value)
        self.wrapper = os.path.join(self.tmp, "Plex Transcoder")
        with open(self.wrapper, "w") as f:
            f.write(text)
        os.chmod(self.wrapper, 0o755)

    def run_wrapper(self, args):
        out = os.path.join(self.tmp, "out.json")
        if os.path.exists(out):
            os.unlink(out)
        # Plex hands the codec directory over with its spaces escaped.
        env = dict(os.environ, PAO_RUN_DIR=self.run_dir, FAKE_REAL_OUT=out,
                   FFMPEG_EXTERNAL_LIBS=self.codecs.replace(" ", "\\ ") + "/")
        subprocess.run([self.wrapper] + args, env=env, check=True, stdin=subprocess.DEVNULL,
                       timeout=20)
        with open(out) as f:
            return json.load(f)

    def records(self):
        directory = os.path.join(self.run_dir, "sessions")
        result = []
        for name in os.listdir(directory):
            with open(os.path.join(directory, name)) as f:
                result.append(json.load(f))
        return result

    def test_dvr_and_odd_arguments_pass_through_exactly(self):
        odd = ["-metadata", "title=it's; $HOME `x` \"q\" *", "", "a\nb", "-filter_complex", "[0:0]null[v]"]
        shim_log = os.path.join(self.run_dir, "shim.log")
        for args in (fixtures.DVR_GRAB, fixtures.DVR_WRITE, odd, ["-version"]):
            result = self.run_wrapper(args)
            self.assertEqual(result["args"], args)
            self.assertFalse(result["stdin_is_fifo"])
        self.assertFalse(os.path.exists(shim_log))  # none of these even started the shim
        # A conversion gets as far as the shim, which leaves it alone.
        args = fixtures.convert(self.source)
        self.assertEqual(self.run_wrapper(args)["args"], args)
        self.assertTrue(os.path.exists(shim_log))
        self.assertEqual(self.records(), [])

    def test_playback_is_injected_and_recorded(self):
        shim.write_json(os.path.join(self.run_dir, "state.json"),
                        {"sessions": {fixtures.SESSION: 120}})
        args = fixtures.hls(self.source)
        result = self.run_wrapper(args)
        expected = [INJECTED if arg == fixtures.AUDIO_CHAIN else arg for arg in args]
        self.assertEqual(result["args"], expected)
        self.assertTrue(result["stdin_is_fifo"])
        (record,) = self.records()
        self.assertEqual(record["session"], fixtures.SESSION)
        self.assertEqual(record["offset_ms"], 120)
        self.assertEqual(record["source"], self.source)
        self.assertFalse(record["held"])
        self.assertTrue(stat.S_ISFIFO(os.stat(record["fifo"]).st_mode))

    def test_copied_audio_is_encoded_again_when_the_service_allows_it(self):
        args = fixtures.hls_copied_audio(self.source)
        state = os.path.join(self.run_dir, "state.json")
        shim.write_json(state, {})
        self.assertEqual(self.run_wrapper(args)["args"], args)  # not allowed: left as it is
        self.assertEqual(self.records(), [])

        shim.write_json(state, {"reencode_copied": True})
        result = self.run_wrapper(args)
        self.assertTrue(result["stdin_is_fifo"])
        at = result["args"].index(REENCODED)
        self.assertEqual(result["args"][at - 1:at + 9], [
            "-filter_complex", REENCODED, "-map", "[pao]", "-metadata:s:1", "language=eng",
            "-codec:1", "ac3", "-b:1", "640k"])
        self.assertEqual(self.records()[0]["reencoded"], "ac3")

        os.unlink(os.path.join(self.codecs, "libac3_encoder.so"))
        self.assertEqual(self.run_wrapper(args)["args"], args)  # no encoder: left as it is
        with open(os.path.join(self.run_dir, "wanted", "ac3.json")) as f:
            wanted = json.load(f)
        self.assertEqual(wanted["directory"].rstrip("/"), self.codecs)
        self.assertEqual(wanted["libraries"], ["libac3_decoder.so", "libac3_encoder.so"])
        self.assertEqual(wanted["session"], fixtures.SESSION)

    def test_playback_passes_through_when_the_service_is_off(self):
        os.unlink(os.path.join(self.run_dir, "enabled"))
        args = fixtures.hls(self.source)
        self.assertEqual(self.run_wrapper(args)["args"], args)

    def test_a_failing_shim_still_starts_the_transcoder(self):
        shutil.rmtree(os.path.join(self.run_dir, "ctl"))
        args = fixtures.dash(self.source)
        result = self.run_wrapper(args)
        self.assertEqual(result["args"], args)
        self.assertFalse(result["stdin_is_fifo"])

    def test_an_unknown_session_waits_for_the_service(self):
        state = os.path.join(self.run_dir, "state.json")
        shim.write_json(state, {"wait_unknown_ms": 5000})
        pending = os.path.join(self.run_dir, "pending")

        def answer():
            deadline = time.time() + 10
            while not os.listdir(pending) and time.time() < deadline:
                time.sleep(0.01)
            shim.write_json(state, {"wait_unknown_ms": 5000, "sessions": {fixtures.SESSION: -80}})

        thread = threading.Thread(target=answer)
        thread.start()
        started = time.time()
        self.run_wrapper(fixtures.hls(self.source))
        thread.join()
        self.assertLess(time.time() - started, 4)
        self.assertEqual(self.records()[0]["offset_ms"], -80)
        self.assertEqual(os.listdir(pending), [])

    def test_an_unknown_session_starts_at_zero_after_the_wait(self):
        shim.write_json(os.path.join(self.run_dir, "state.json"),
                        {"wait_unknown_ms": 200, "hold_unknown_s": 4})
        result = self.run_wrapper(fixtures.hls(self.source))
        (record,) = self.records()
        self.assertEqual(record["offset_ms"], 0)
        self.assertTrue(record["held"])
        self.assertIn("arealtime@pao=speed=1[4]", " ".join(result["args"]))


if __name__ == "__main__":
    unittest.main()
