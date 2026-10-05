#!/usr/bin/python3 -I
"""Transcoder shim for plex-audio-offset.

The wrapper installed in place of "Plex Transcoder" runs this for playback
transcodes:

    shim.py <real transcoder> <original arguments...>

It appends a small filter group to the audio filter chain, gives the
transcoder a FIFO as stdin so the service can send filter commands while it
runs, records the session for the service, and then execs the real transcoder.
Audio that Plex meant to copy untouched is decoded and encoded again in the
same format, because only decoded audio can be shifted.

Anything it does not recognise, and any failure at all, ends in an exec of the
real transcoder with the original arguments.
"""

import json
import os
import re
import subprocess
import sys
import time

RUN_DIR = os.environ.get("PAO_RUN_DIR", "/run/plex-audio-offset")
TAG = "pao"
# Output formats Plex uses for streaming sessions. Conversions to a single
# file (optimized versions, downloads) must never get an offset baked in.
STREAM_FORMATS = ("ssegment", "dash")
SESSION_RE = re.compile(r"/transcode/session/([^/?]+)/[^/?]+/progress")
# One linear audio chain: "[0:<stream>] ...aresample=...[label]".
AUDIO_CHAIN_RE = re.compile(
    r"^(\[0:[^\]]+\][^;\[\]]*\baresample=[^;\[\]]*)(\[\w+\])\s*$")
# One audio stream as the transcoder lists it for an input file, for example
#   Stream #0:1[0x101](eng): Audio: ac3 (AC-3 / 0x332D4341), 48000 Hz, 5.1(side), fltp, 384 kb/s
STREAM_RE = re.compile(
    r"^\s*Stream #0:(\d+)(?:\[0x([0-9a-fA-F]+)\])?(?:\([^)]*\))?: Audio: (\w+)[^,]*, \d+ Hz, ([^,\n]+)",
    re.M)
# Copied audio that can be encoded again as what it was: codec -> (encoder,
# codec libraries Plex must have downloaded, bitrate for stereo, for more).
# The bitrates are the ones Plex itself uses when it encodes that codec.
REENCODE = {"ac3": ("ac3", ("libac3_decoder.so", "libac3_encoder.so"), "256k", "640k")}
# arealtime speed that never sleeps, i.e. the transcoder runs flat out.
UNTHROTTLED = 1000
POLL_SECONDS = 0.05


class Skip(Exception):
    """This invocation should run unmodified."""


class Missing(Skip):
    """Copied audio stays a copy because Plex lacks a codec library."""

    def __init__(self, codec, libraries):
        Skip.__init__(self, "copied %s audio: Plex has not downloaded %s" % (codec, libraries[-1]))
        self.codec = codec
        self.libraries = libraries


def option_values(args, name):
    return [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == name]


def offset_expr(offset_ms):
    """asetpts expression that moves audio by offset_ms (positive = later)."""
    return "PTS+(%.3f)/TB" % (offset_ms / 1000.0)


def inspect(args, isfile=os.path.isfile):
    """Describe a playback transcode, or raise Skip."""
    inputs = [i for i, arg in enumerate(args[:-1]) if arg == "-i"]
    if len(inputs) != 1:
        raise Skip("expected one input, found %d" % len(inputs))
    source = args[inputs[0] + 1]
    if not (os.path.isabs(source) and isfile(source)):
        raise Skip("input is not a local file")
    progress = option_values(args, "-progressurl")
    match = SESSION_RE.search(progress[-1]) if progress else None
    if not match:
        raise Skip("no session in -progressurl")
    formats = option_values(args[inputs[0]:], "-f")
    if not formats or formats[-1] not in STREAM_FORMATS:
        raise Skip("not a streaming output")
    if any("@" + TAG in arg for arg in args):
        raise Skip("already injected")
    chains = [i + 1 for i, arg in enumerate(args[:-1])
              if arg == "-filter_complex" and AUDIO_CHAIN_RE.match(args[i + 1])]
    if len(chains) > 1:
        raise Skip("expected one audio filter chain, found %d" % len(chains))
    copies = [] if chains else copied_streams(args, inputs[0])
    if not chains and not copies:
        raise Skip("no audio filter chain and no copied stream")
    start = 0.0
    seeks = option_values(args[:inputs[0]], "-ss")
    if seeks and "-copyts" in args:
        try:
            start = float(seeks[-1])
        except ValueError:
            raise Skip("unparsable seek position %r" % seeks[-1])
    return {"session": match.group(1), "chain": chains[0] if chains else None,
            "copies": copies, "start": start, "source": source, "format": formats[-1]}


def copied_streams(args, input_index):
    """Streams that are mapped straight from the input and copied.

    Returns (index of the map spec, index of the "copy" value, output number)
    for each. Which of them is audio is only known after probing the file.
    """
    result = []
    maps = [i for i, arg in enumerate(args[:-1]) if arg == "-map" and i > input_index]
    for number, i in enumerate(maps):
        option = "-codec:%d" % number
        values = [j + 1 for j, arg in enumerate(args[:-1]) if arg == option and j > i]
        if values and args[values[0]] == "copy" and re.match(r"^0:(#0x[0-9a-fA-F]+|\d+)$", args[i + 1]):
            result.append((i + 1, values[0], number))
    return result


def probe_audio(real, source):
    """Audio streams of a file as the transcoder sees them."""
    listing = subprocess.run([real, "-hide_banner", "-i", source], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=5).stderr
    return [{"index": int(index), "id": int(ident, 16) if ident else None,
             "codec": codec, "layout": layout.strip()}
            for index, ident, codec, layout in STREAM_RE.findall(listing.decode("utf-8", "replace"))]


def codec_directory():
    """Where Plex keeps the codec libraries it downloads for this transcoder."""
    # Plex writes the directory with its spaces escaped by backslashes.
    return os.environ.get("FFMPEG_EXTERNAL_LIBS", "").replace("\\ ", " ")


def have_libraries(names):
    """Whether Plex has downloaded the given codec libraries."""
    directory = codec_directory()
    return bool(directory) and all(os.path.isfile(os.path.join(directory, name)) for name in names)


def reencode(args, info, streams, available=have_libraries):
    """Turn the one copied audio stream into a decode, filter and encode.

    Returns the new arguments with a plain audio chain in place and updates
    info to point at it, or raises Skip when the audio has to stay a copy.
    """
    audio = []
    for spec_index, copy_index, number in info["copies"]:
        spec = args[spec_index][2:]
        for stream in streams:
            if spec in ("#0x%x" % (stream["id"] or -1), "#0x%X" % (stream["id"] or -1),
                        str(stream["index"])):
                audio.append((spec_index, copy_index, number, stream))
    if len(audio) != 1:
        raise Skip("expected one copied audio stream, found %d" % len(audio))
    spec_index, copy_index, number, stream = audio[0]
    if stream["codec"] not in REENCODE:
        raise Skip("copied %s audio cannot be encoded again" % stream["codec"])
    encoder, libraries, stereo_rate, surround_rate = REENCODE[stream["codec"]]
    if not available(libraries):
        raise Missing(stream["codec"], libraries)
    label = "[%s]" % TAG
    new_args = list(args)
    new_args[copy_index] = encoder
    rate = stereo_rate if stream["layout"] in ("mono", "stereo") else surround_rate
    new_args[copy_index + 1:copy_index + 1] = ["-b:%d" % number, rate]
    chain = "[%s] aresample=async=1%s" % (args[spec_index], label)
    new_args[spec_index] = label
    new_args[spec_index - 1:spec_index - 1] = ["-filter_complex", chain]
    option = "-copypriorss:%d" % number  # only means something for a copy
    if option in new_args:
        del new_args[new_args.index(option):new_args.index(option) + 2]
    info["chain"] = spec_index
    info["reencoded"] = stream["codec"]
    return new_args


def inject(chain, offset_ms, start, hold_seconds=0):
    """Append the offset filters to an audio chain.

    asetpts moves the timestamps and can be changed while running; the
    aresample after it turns a timestamp jump into inserted silence or dropped
    samples, so the output stays continuous. atrim keeps audio from starting
    before the video when the offset is negative. arealtime lets the service
    slow the transcoder down so a change is heard soon after it is made.

    With hold_seconds the transcoder starts at playback speed and lets go by
    itself after that much of the stream. It is used while the player is not
    known yet, so that little has been transcoded when its offset arrives.
    """
    match = AUDIO_CHAIN_RE.match(chain)
    filters = [
        "asetpts@%s=%s" % (TAG, offset_expr(offset_ms)),
        "aresample@%s=async=1:min_hard_comp=0.001" % TAG,
        "atrim=start=%.6f" % start,
        "arealtime@%s=speed=%d" % (TAG, UNTHROTTLED),
    ]
    if hold_seconds > 0:
        filters[3] = "arealtime@%s=speed=1" % TAG
        filters.insert(3, "asendcmd=c='%.3f arealtime@%s speed %d'"
                       % (start + hold_seconds, TAG, UNTHROTTLED))
    return match.group(1) + "," + ",".join(filters) + match.group(2)


def safe_name(session):
    return re.sub(r"[^A-Za-z0-9._-]", "_", session)


def read_state(run_dir):
    try:
        with open(os.path.join(run_dir, "state.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def lookup(state, session):
    """Offset the service has for this session, or None if it is unknown."""
    sessions = state.get("sessions", {})
    if session in sessions:
        return int(sessions[session])
    prefix = re.sub(r"-\d+$", "", session)
    prefixes = state.get("prefixes", {})
    if prefix != session and prefix in prefixes:
        return int(prefixes[prefix])
    return None


def write_json(path, data):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.rename(tmp, path)


def initial_offset(info, run_dir):
    """Offset to start with.

    The service knows offsets per player, but a brand new session is not tied
    to a player yet. When it matters, wait briefly for the service to work out
    who is playing so the stream is right from its first sample.
    """
    state = read_state(run_dir)
    found = lookup(state, info["session"])
    wait_ms = int(state.get("wait_unknown_ms", 0))
    began = time.monotonic()
    if found is None and wait_ms > 0:
        marker = os.path.join(run_dir, "pending", safe_name(info["session"]) + ".json")
        write_json(marker, {"session": info["session"], "source": info["source"],
                            "time": time.time()})
        deadline = time.monotonic() + wait_ms / 1000.0
        while found is None and time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)
            found = lookup(read_state(run_dir), info["session"])
        try:
            os.unlink(marker)
        except OSError:
            pass
    info["waited_ms"] = int((time.monotonic() - began) * 1000)
    info["known"] = found is not None
    info["hold_seconds"] = 0 if found is not None else float(state.get("hold_unknown_s", 0))
    if found is None:
        found = int(state.get("default_offset_ms", 0))
    return found


def prepare(real, args, run_dir):
    """Return (new args, stdin fd, stderr fd) for a playback transcode."""
    info = inspect(args)
    if info["chain"] is None:
        if not read_state(run_dir).get("reencode_copied"):
            raise Skip("audio is copied and is to be left alone")
        try:
            args = reencode(args, info, probe_audio(real, info["source"]))
        except Missing as missing:
            # Tell the service, which can get Plex to download the library.
            if codec_directory():
                write_json(os.path.join(run_dir, "wanted", missing.codec + ".json"), {
                    "codec": missing.codec, "libraries": list(missing.libraries),
                    "directory": codec_directory(), "session": info["session"],
                    "source": info["source"], "time": time.time()})
            raise
    offset_ms = initial_offset(info, run_dir)
    pid = os.getpid()
    fifo = os.path.join(run_dir, "ctl", "%d.fifo" % pid)
    log = os.path.join(run_dir, "log", "%d.log" % pid)
    for stale in (fifo, log):
        try:
            os.unlink(stale)
        except OSError:
            pass
    os.mkfifo(fifo, 0o600)
    # Read-write, so the transcoder never sees end-of-file on its stdin.
    stdin_fd = os.open(fifo, os.O_RDWR)
    stderr_fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    new_args = list(args)
    new_args[info["chain"]] = inject(args[info["chain"]], offset_ms, info["start"],
                                     info["hold_seconds"])
    write_json(os.path.join(run_dir, "sessions", "%d.json" % pid), {
        "pid": pid, "session": info["session"], "fifo": fifo, "log": log,
        "offset_ms": offset_ms, "start": info["start"], "source": info["source"],
        "format": info["format"], "cwd": os.getcwd(), "started": time.time(),
        "held": info["hold_seconds"] > 0, "reencoded": info.get("reencoded"),
    })
    note(run_dir, "inject: session %s at %+d ms (%s after %d ms)%s" % (
        info["session"], offset_ms, "known" if info["known"] else "not known", info["waited_ms"],
        ", copied %s audio encoded again" % info["reencoded"] if info.get("reencoded") else ""))
    return new_args, stdin_fd, stderr_fd


def note(run_dir, message):
    """Best-effort line in the shim log."""
    try:
        path = os.path.join(run_dir, "shim.log")
        if os.path.exists(path) and os.path.getsize(path) > 1 << 20:
            os.truncate(path, 0)
        with open(path, "a") as f:
            f.write("%s pid=%d %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid(), message))
    except OSError:
        pass


def self_test():
    args = [
        "-codec:#0x101", "ac3", "-ss", "300", "-i", "/media/Show (2020) - S01E01 - A; B.ts",
        "-start_at_zero", "-copyts", "-progressurl",
        "http://127.0.0.1:32400/video/:/transcode/session/abc-12/0f/progress",
        "-filter_complex", "[0:#0x100]hwupload[0];[0]scale_vaapi=w=1920:h=1080[1]",
        "-map", "[1]", "-codec:0", "h264_vaapi",
        "-filter_complex", "[0:#0x101] aresample=async=1:ochl='stereo':osr=48000[2]",
        "-map", "[2]", "-codec:1", "aac", "-f", "dash", "dash",
    ]
    info = inspect(args, isfile=lambda path: True)
    assert info["session"] == "abc-12" and info["start"] == 300.0, info
    chain = inject(args[info["chain"]], -125, info["start"])
    assert chain == ("[0:#0x101] aresample=async=1:ochl='stereo':osr=48000,"
                     "asetpts@pao=PTS+(-0.125)/TB,aresample@pao=async=1:min_hard_comp=0.001,"
                     "atrim=start=300.000000,arealtime@pao=speed=1000[2]"), chain
    assert "asendcmd=c='304.000 arealtime@pao speed 1000',arealtime@pao=speed=1[2]" in inject(
        args[info["chain"]], 0, info["start"], 4), "hold"
    assert lookup({"prefixes": {"abc": 40}}, "abc-12") == 40
    copied = args[:args.index("-filter_complex", 12)] + [
        "-map", "0:#0x101", "-codec:1", "copy", "-copypriorss:1", "0", "-f", "ssegment", "out"]
    info = inspect(copied, isfile=lambda path: True)
    streams = [{"index": 1, "id": 0x101, "codec": "ac3", "layout": "5.1(side)"}]
    result = reencode(copied, info, streams, available=lambda names: True)
    assert result[-11:] == ["-filter_complex", "[0:#0x101] aresample=async=1[pao]", "-map", "[pao]",
                            "-codec:1", "ac3", "-b:1", "640k", "-f", "ssegment", "out"], result[-11:]
    assert result[info["chain"]] == "[0:#0x101] aresample=async=1[pao]"
    print("shim self-test ok")
    return 0


def main(argv):
    if len(argv) == 2 and argv[1] == "--self-test":
        return self_test()
    if len(argv) < 2:
        sys.stderr.write("usage: shim.py <real transcoder> [arguments...]\n")
        return 2
    real, args = argv[1], argv[2:]
    try:
        new_args, stdin_fd, stderr_fd = prepare(real, args, RUN_DIR)
    except Skip as reason:
        note(RUN_DIR, "pass-through: %s" % reason)
    except BaseException as error:  # noqa: B902 - nothing may stop playback
        note(RUN_DIR, "pass-through after error: %r" % (error,))
    else:
        try:
            os.dup2(stdin_fd, 0)
            os.dup2(stderr_fd, 2)
            os.execv(real, [real] + new_args)
        except BaseException as error:  # noqa: B902
            note(RUN_DIR, "exec with injected arguments failed: %r" % (error,))
    os.execv(real, [real] + args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
