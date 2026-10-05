#!/usr/bin/env python3
"""Measure how far the audio of a received stream is shifted against its source.

Compares the audio of a stream saved by sim_client.py with the audio of the
file it was made from, window by window, and prints the shift over time.

Plex's HLS segments carry the source's timestamps plus a fixed 10 s (twice the
5 s "max_delay" it gives the MPEG-TS muxer); --timestamp-shift accounts for it.

Needs ffmpeg, ffprobe and numpy.
"""

import argparse
import subprocess

import numpy as np

RATE = 16000
MARGIN = 3.0  # seconds of source audio to decode either side of the stream


def decode(ffmpeg, path, stream, seek=None, length=None):
    command = [ffmpeg, "-v", "fatal", "-nostdin"]
    if seek is not None:
        command += ["-ss", "%.6f" % seek, "-t", "%.6f" % length]
    command += ["-i", path, "-map", "0:" + stream, "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"]
    raw = subprocess.run(command, check=True, stdout=subprocess.PIPE).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float64)


def start_time(ffprobe, path, stream):
    out = subprocess.run([ffprobe, "-v", "error", "-select_streams", stream, "-show_entries",
                          "stream=start_time", "-of", "csv=p=0", path],
                         check=True, stdout=subprocess.PIPE).stdout
    return float(out.split()[0])


def shifts(reference, reference_start, stream, stream_start, window, max_shift):
    """Yield (media time, shift in ms, correlation) for each window of the stream."""
    size = int(window * RATE)
    reach = int(max_shift * RATE)
    for begin in range(0, len(stream) - size + 1, size):
        piece = stream[begin:begin + size]
        media_time = stream_start + begin / RATE
        centre = int(round((media_time - reference_start) * RATE))
        low, high = max(0, centre - reach), min(len(reference), centre + size + reach)
        area = reference[low:high]
        if len(area) < size or np.abs(piece).max() < 30:
            yield media_time, None, 0.0
            continue
        n = 1 << int(np.ceil(np.log2(len(area) + size)))
        scores = np.fft.irfft(np.fft.rfft(area, n) * np.conj(np.fft.rfft(piece, n)), n)
        scores = scores[:len(area) - size + 1]
        best = int(np.argmax(scores))
        norm = np.linalg.norm(piece) * np.linalg.norm(area[best:best + size])
        # The content of this window sits at low + best in the source.
        yield media_time, (centre - (low + best)) * 1000.0 / RATE, float(scores[best] / (norm + 1e-9))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", required=True, help="the file Plex transcoded")
    parser.add_argument("--stream", required=True, help="stream.ts saved by sim_client.py")
    parser.add_argument("--source-audio", default="a:0", help="audio stream of the source")
    parser.add_argument("--timestamp-shift", type=float, default=10.0,
                        help="seconds the stream's timestamps run ahead of the source's")
    parser.add_argument("--window", type=float, default=1.0, help="seconds per measurement")
    parser.add_argument("--max-shift", type=float, default=2.5, help="largest shift to look for, seconds")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    args = parser.parse_args()

    stream = decode(args.ffmpeg, args.stream, "a:0")
    stream_start = start_time(args.ffprobe, args.stream, "a:0") - args.timestamp_shift
    video_start = start_time(args.ffprobe, args.stream, "v:0") - args.timestamp_shift
    reference_start = max(0.0, stream_start - MARGIN)
    reference = decode(args.ffmpeg, args.source, args.source_audio, reference_start,
                       len(stream) / RATE + 2 * MARGIN)
    print("stream: video starts at %.3f s, audio at %.3f s, %.1f s of audio"
          % (video_start, stream_start, len(stream) / RATE))

    spans = []  # [first media time, last media time, shift]
    for media_time, shift, score in shifts(reference, reference_start, stream, stream_start,
                                           args.window, args.max_shift):
        if shift is None or score < 0.5:
            continue  # silence, or a window that straddles a change
        if spans and abs(spans[-1][2] - shift) <= 2:
            spans[-1][1] = media_time + args.window
        else:
            spans.append([media_time, media_time + args.window, shift])
    for first, last, shift in spans:
        print("%8.1f s to %8.1f s: audio %+7.1f ms" % (first, last, shift))


if __name__ == "__main__":
    main()
