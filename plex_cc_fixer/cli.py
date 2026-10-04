import argparse
import os
import re

from . import captions, media, plex, speech, variants


def episode_code(path):
    m = re.search(r"S\d+E\d+", os.path.basename(path), re.IGNORECASE)
    return m.group(0).upper() if m else os.path.splitext(os.path.basename(path))[0]


def cmd_episode(a):
    ts = os.path.abspath(a.ts)
    code = a.code or episode_code(ts)
    ep = os.path.join(os.path.abspath(a.work), code)
    os.makedirs(ep, exist_ok=True)

    source = os.path.join(ep, "source.ts")
    if not os.path.lexists(source):
        os.symlink(ts, source)

    audio = os.path.join(ep, "audio.flac")
    cc_srt = os.path.join(ep, "captions.srt")
    words_json = os.path.join(ep, "words.json")
    if a.force:
        for path in (audio, cc_srt, words_json):
            if os.path.exists(path):
                os.remove(path)
    if not os.path.exists(audio):
        media.extract_audio(ts, audio, media.audio_stream_index(ts, a.audio_stream))
    if not os.path.exists(cc_srt):
        media.extract_captions(ep)

    words = speech.transcribe_cached(audio, words_json, model=a.model, device=a.device, compute=a.compute,
                                     language=a.language, prompt=a.prompt, beam_size=a.beam_size)
    cc_lines = captions.dedupe_rolling(captions.load_cc(cc_srt))
    cues, fallback = speech.build_subtitles(words, cc_lines)

    out = os.path.join(ep, "subtitle.en.srt")
    captions.write_srt(out, [(s, e, t) for s, e, t, _ in cues])
    with open(os.path.join(ep, "fallback.txt"), "w", encoding="utf-8") as f:
        for s, e, t in fallback:
            f.write(f"{captions.srt_ts(s)} --> {captions.srt_ts(e)} | {t}\n")
    print(f"[srt] {len(cues)} cues -> {out}")

    if not a.no_plex:
        client = plex.PlexClient(a.plex_url, plex.load_token(a.plex_token_file))
        client.attach_srt(ts, out, f"{code}.en.srt")


def cmd_variants(a):
    variants.run(a.audio, a.start, a.duration, a.out, model=a.model, device=a.device, compute=a.compute,
                 prompt=a.prompt, audio_stream=a.audio_stream, cc_srt=a.captions)


def main(argv=None):
    p = argparse.ArgumentParser(prog="plex-cc-fixer", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    ep = sub.add_parser("episode", help="build a subtitle for one recording and attach it to Plex")
    ep.add_argument("ts", help="recording file (.ts) as Plex sees it")
    ep.add_argument("--work", default="work", help="working directory; one subfolder per episode")
    ep.add_argument("--code", help="episode label used for the Plex subtitle title (default: SxxEyy from filename)")
    ep.add_argument("--prompt", default=None, help="Whisper initial prompt: names and terms likely to appear")
    ep.add_argument("--language", default="en")
    ep.add_argument("--audio-stream", type=int, default=None,
                    help="absolute ffmpeg stream index (default: first English track)")
    ep.add_argument("--model", default="small.en")
    ep.add_argument("--device", default="cpu")
    ep.add_argument("--compute", default="int8")
    ep.add_argument("--beam-size", type=int, default=1)
    ep.add_argument("--force", action="store_true", help="redo audio, captions and transcription")
    ep.add_argument("--no-plex", action="store_true", help="write the SRT but do not attach it to Plex")
    ep.add_argument("--plex-url", default=os.environ.get("PLEX_URL", "http://localhost:32400"))
    ep.add_argument("--plex-token-file", default=None, help="file containing the Plex token (or set PLEX_TOKEN)")
    ep.set_defaults(func=cmd_episode)

    vt = sub.add_parser("variants", help="transcribe one clip with several Whisper configurations")
    vt.add_argument("audio", help="audio or recording file")
    vt.add_argument("--start", type=float, required=True, help="clip start, seconds")
    vt.add_argument("--duration", type=float, required=True, help="clip length, seconds")
    vt.add_argument("--out", required=True)
    vt.add_argument("--model", default="small.en")
    vt.add_argument("--device", default="cpu")
    vt.add_argument("--compute", default="int8")
    vt.add_argument("--prompt", default=None)
    vt.add_argument("--audio-stream", type=int, default=None)
    vt.add_argument("--captions", default=None, help="caption SRT to write alongside for comparison")
    vt.set_defaults(func=cmd_variants)

    args = p.parse_args(argv)
    args.func(args)
