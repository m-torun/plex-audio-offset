"""Transcoder command lines as Plex Media Server 1.43 builds them."""

SESSION = "1b2c3d4e-0000-4000-8000-123456789abc-57"
PROGRESS = "http://127.0.0.1:32400/video/:/transcode/session/%s/44efc43f-edcb/progress" % SESSION
MANIFEST = ("http://127.0.0.1:32400/video/:/transcode/session/%s/44efc43f-edcb/"
            "manifest?X-Plex-Http-Pipeline=infinite" % SESSION)
VIDEO_CHAIN = ("[0:#0x100]hwupload[0];[0]deinterlace_vaapi[1];"
               "[1]scale_vaapi=w=1920:h=1080:format=nv12[2];[2]hwupload[3]")
AUDIO_CHAIN = "[0:#0x101] aresample=async=1:ochl='stereo':rematrix_maxval=0.000000dB:osr=48000[4]"


def _playback(source, output):
    return [
        "-codec:#0x100", "mpeg2video", "-hwaccel:#0x100", "vaapi",
        "-hwaccel_output_format:#0x100", "vaapi", "-hwaccel_device:#0x100", "vaapi",
        "-codec:#0x101", "ac3", "-ss", "300", "-analyzeduration", "20000000",
        "-probesize", "20000000", "-i", source, "-start_at_zero", "-copyts",
        "-init_hw_device", "vaapi=vaapi:/dev/dri/renderD128,driver=iHD",
        "-filter_hw_device", "vaapi", "-y", "-nostats", "-loglevel", "quiet",
        "-loglevel_plex", "error", "-progressurl", PROGRESS,
        "-filter_complex", VIDEO_CHAIN, "-map", "[3]", "-codec:0", "h264_vaapi",
        "-b:0", "18303k", "-maxrate:0", "18303k", "-bufsize:0", "36606k",
        "-r:0", "29.969999999999999", "-sei:0", "-a53_cc",
        "-force_key_frames:0", "expr:gte(t,n_forced*1)",
        "-filter_complex", AUDIO_CHAIN, "-map", "[4]",
        "-metadata:s:1", "language=eng", "-codec:1", "aac", "-b:1", "256k",
    ] + output + ["-avoid_negative_ts", "disabled", "-map_metadata", "-1", "-map_chapters", "-1"]


def hls(source):
    """Playback for a player that takes HLS, such as Plex for Roku."""
    return _playback(source, [
        "-segment_format", "mpegts", "-f", "ssegment", "-individual_header_trailer", "0",
        "-segment_time", "1", "-segment_start_number", "300", "-segment_time_delta", "0.0625",
        "-segment_list", MANIFEST, "-segment_list_type", "csv", "-segment_list_size", "5",
        "-segment_list_separate_stream_times", "1", "-segment_list_unfinished", "1",
        "-max_delay", "5000000",
    ]) + ["media-%05d.ts"]


def dash(source):
    """Playback for Plex Web."""
    return _playback(source, [
        "-f", "dash", "-seg_duration", "1", "-dash_segment_type", "mp4",
        "-init_seg_name", "init-stream$RepresentationID$.m4s",
        "-media_seg_name", "chunk-stream$RepresentationID$-$Number%05d$.m4s",
        "-window_size", "5", "-delete_removed", "false", "-skip_to_segment", "301",
        "-manifest_name", MANIFEST,
    ]) + ["dash"]


def hls_copied_audio(source):
    """HLS playback for a player that takes the AC-3 audio as it is."""
    args = hls(source)
    start = args.index("-filter_complex", args.index("-filter_complex") + 1)
    end = args.index("256k") + 1
    args[start:end] = ["-map", "0:#0x101", "-metadata:s:1", "language=eng",
                       "-codec:1", "copy", "-copypriorss:1", "0"]
    args.remove("-codec:#0x101")
    args.remove("ac3")
    return args


AC3_STREAM = {"index": 1, "id": 0x101, "codec": "ac3", "layout": "5.1(side)"}
PROBE_OUTPUT = """Input #0, mpegts, from 'x.ts':
  Duration: 00:16:49.50, start: 59160.513122, bitrate: 13182 kb/s
  Program 3
  Stream #0:0[0x100]: Video: mpeg2video (Main) ([2][0][0][0] / 0x0002), yuv420p(tv, bt709, top first), 1920x1080 [SAR 1:1 DAR 16:9], Closed Captions, 29.97 fps, 29.97 tbr, 90k tbn
  Stream #0:1[0x101](eng): Audio: ac3 (AC-3 / 0x332D4341), 48000 Hz, 5.1(side), fltp, 384 kb/s
  Stream #0:2[0x102](spa): Audio: ac3 (AC-3 / 0x332D4341), 48000 Hz, stereo, fltp, 128 kb/s
At least one output file must be specified
"""


def convert(source):
    """A conversion to one file, as for an optimized version."""
    return _playback(source, ["-f", "mp4", "-movflags", "+faststart"]) + ["/tmp/out.mp4"]


# The two processes of a DVR recording: tuner to segments, segments to file.
DVR_GRAB = [
    "-noaccurate_seek", "-ignore_unknown", "-scan_all_pmts", "-1", "-rw_timeout", "30000000",
    "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "30",
    "-fflags", "+discardcorruptts+fillwallclockdts", "-probesize", "20000000",
    "-i", "http://192.0.2.10:5004/auto/v7.1", "-y", "-nostats", "-loglevel", "quiet",
    "-loglevel_plex", "error", "-xioerror", "-progressurl", PROGRESS,
    "-map", "0:V?", "-codec:V", "copy", "-map", "0:a?", "-codec:a", "copy",
    "-map", "0:s?", "-codec:s", "copy", "-break_non_keyframes", "1",
    "-segment_format", "mpegts", "-f", "ssegment", "-individual_header_trailer", "0",
    "-segment_time", "1", "-segment_start_number", "0", "-segment_time_delta", "0.25",
    "-segment_list", MANIFEST, "-segment_list_type", "csv", "-segment_list_size", "5",
    "-segment_list_separate_stream_times", "1", "-segment_list_unfinished", "1",
    "-max_delay", "5000000", "-map_metadata", "-1", "-map_chapters", "-1", "media-%05d.ts",
]
DVR_WRITE = [
    "-codec:0", "mpeg2video", "-noaccurate_seek", "-live_start_index", "0",
    "-probesize", "20000000",
    "-i", "http://127.0.0.1:32400/livetv/sessions/aa/bb/index.m3u8?offset=-1.000000&X-Plex-Token=x",
    "-y", "-nostats", "-loglevel", "quiet", "-loglevel_plex", "error", "-xioerror",
    "-progressurl", PROGRESS, "-map", "0:0", "-codec:0", "copy",
    "-map", "0:1", "-metadata:s:1", "language=eng", "-codec:1", "copy",
    "-map", "0:2", "-metadata:s:2", "language=spa", "-codec:2", "copy",
    "-f", "mpegts", "-map_metadata", "-1", "-map_chapters", "-1",
    "/media/.grab/abc/Some Game (2002) - 2026-10-04 12 00 00 - Home at Away.ts",
]
