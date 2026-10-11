import argparse
import datetime
import json
import os
import re

from . import brief, captions, llm, media, plex, polish, speech, variants


def episode_code(path):
    m = re.search(r"S\d+E\d+", os.path.basename(path), re.IGNORECASE)
    return m.group(0).upper() if m else os.path.splitext(os.path.basename(path))[0]


def identity(ts, code):
    """Describe the recording for the research prompt, from its file name and date."""
    name = os.path.splitext(os.path.basename(ts))[0]
    try:
        recorded = datetime.date.fromtimestamp(os.path.getmtime(ts)).isoformat()
    except OSError:
        recorded = "unknown date"
    return f"\"{name}\" (episode code {code}), file written {recorded}"


def recording(a):
    """The recording's path, its episode code and its work folder."""
    ts = os.path.abspath(a.ts)
    code = a.code or episode_code(ts)
    ep = os.path.join(os.path.abspath(a.work), code)
    os.makedirs(ep, exist_ok=True)
    return ts, code, ep


def prepare(a):
    """Extract what is missing, transcribe, and build the merged cues for one recording."""
    ts, code, ep = recording(a)

    source = os.path.join(ep, "source.ts")
    if not os.path.lexists(source):
        os.symlink(ts, source)

    audio = os.path.join(ep, "audio.flac")
    cc_srt = os.path.join(ep, "captions.srt")
    words_json = os.path.join(ep, "words.json")
    if getattr(a, "force", False):
        for path in (audio, cc_srt, words_json):
            if os.path.exists(path):
                os.remove(path)
    if not os.path.exists(audio) and not os.path.exists(words_json):
        media.extract_audio(ts, audio, media.audio_stream_index(ts, a.audio_stream))
    if not os.path.exists(cc_srt):
        media.extract_captions(ep)

    words = speech.transcribe_cached(audio, words_json, model=a.model, device=a.device, compute=a.compute,
                                     language=a.language, prompt=a.prompt, beam_size=a.beam_size,
                                     threads=a.threads)
    cc_lines = captions.dedupe_rolling(captions.load_cc(cc_srt))
    cues, fallback, reference = speech.build_subtitles(words, cc_lines, max_chars=a.max_chars)
    return ts, code, ep, cues, fallback, reference


def researched(a, ts, code, ep, cues, claude):
    """Return the episode's researched facts, from the episode folder if already done."""
    path = os.path.join(ep, "research.json")
    if os.path.exists(path) and not getattr(a, "refresh", False):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    facts = brief.research(cues, identity(ts, code), claude)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(facts, f, indent=1)
    return facts


def cmd_episode(a):
    ts, code, ep, cues, fallback, reference = prepare(a)
    label = f"{a.variant}.en" if a.variant else "en"
    changes = None

    if a.polish:
        claude = llm.Claude(os.path.join(ep, "llm"), model=a.llm_model)
        try:
            facts = researched(a, ts, code, ep, cues, claude)
        except llm.LLMError as error:
            print(f"[research] failed, continuing without it: {error}")
            facts = {}
        cues, changes = polish.polish(cues, reference, facts, claude)
        with open(os.path.join(ep, f"changes.{label}.txt"), "w", encoding="utf-8") as f:
            for change in changes:
                f.write(f"{captions.srt_ts(change['time'])} | {change['kind']} | {change['old']} -> {change['new']}\n")
        if a.site and facts:
            print(f"[site] {brief.write_site(a.site, ts, code, facts, changes)}")
        print(f"[llm] reported cost of this run: ${claude.cost:.2f}")

    out = os.path.join(ep, f"subtitle.{label}.srt")
    captions.write_srt(out, [(cue["start"], cue["end"], cue["text"]) for cue in cues])
    with open(os.path.join(ep, "fallback.txt"), "w", encoding="utf-8") as f:
        for cue in fallback:
            f.write(f"{captions.srt_ts(cue['start'])} --> {captions.srt_ts(cue['end'])} | {cue['text']}\n")
    print(f"[srt] {len(cues)} cues -> {out}")

    if not a.no_plex:
        client = plex.PlexClient(a.plex_url, plex.load_token(a.plex_token_file))
        client.attach_srt(ts, out, f"{code}.{label}.srt", key=a.rating_key)


def cues_from_plex(a, ts, ep):
    """The cues of a subtitle already attached to the recording in Plex, or None. The cheat sheet only
    needs to know what is said when, so an existing subtitle saves transcribing the recording again."""
    if a.no_plex:
        return None
    try:
        client = plex.PlexClient(a.plex_url, plex.load_token(a.plex_token_file))
        key = a.rating_key or client.find_rating_key(ts)
        attached = client.uploaded_subtitles(key) if key else []
        if not attached:
            return None
        text = client.fetch(attached[0]["key"]).decode("utf-8", errors="replace")
    except Exception as error:  # best effort: any trouble with Plex means transcribing instead
        print(f"[subtitle] could not read one from Plex ({error}); transcribing instead")
        return None
    cues = captions.parse_srt(text)
    if not cues:
        return None
    with open(os.path.join(ep, "plex-subtitle.srt"), "w", encoding="utf-8") as f:
        f.write(text)
    print(f"[subtitle] using {attached[0]['title'] or 'the subtitle'} from Plex: {len(cues)} cues")
    return cues


def cmd_brief(a):
    ts, code, ep = recording(a)
    cues = None if os.path.exists(os.path.join(ep, "words.json")) else cues_from_plex(a, ts, ep)
    if cues is None:
        ts, code, ep, cues, _, _ = prepare(a)
    claude = llm.Claude(os.path.join(ep, "llm"), model=a.llm_model)
    facts = researched(a, ts, code, ep, cues, claude)
    print(f"[site] {brief.write_site(a.site, ts, code, facts)}")
    print(f"[llm] reported cost of this run: ${claude.cost:.2f}")


def cmd_serve(a):
    from . import server
    server.serve(a.site, a.work, a.port, a.listen, a.plex_url, a.plex_token_file, a.threads)


def cmd_variants(a):
    variants.run(a.audio, a.start, a.duration, a.out, model=a.model, device=a.device, compute=a.compute,
                 prompt=a.prompt, audio_stream=a.audio_stream, cc_srt=a.captions)


def add_recording_arguments(p):
    p.add_argument("ts", help="recording file (.ts) as Plex sees it")
    p.add_argument("--work", default="work", help="working directory; one subfolder per episode")
    p.add_argument("--code", help="episode label (default: SxxEyy from the file name)")
    p.add_argument("--prompt", default=None, help="Whisper initial prompt: names and terms likely to appear")
    p.add_argument("--language", default="en")
    p.add_argument("--audio-stream", type=int, default=None,
                   help="absolute ffmpeg stream index (default: first English track)")
    p.add_argument("--model", default="small.en", help="Whisper model")
    p.add_argument("--device", default="cpu")
    p.add_argument("--compute", default="int8")
    p.add_argument("--beam-size", type=int, default=1)
    p.add_argument("--threads", type=int, default=0,
                   help="CPU threads for Whisper (default: all cores); fewer keeps the machine responsive")
    p.add_argument("--max-chars", type=int, default=speech.DEFAULT_MAX_CHARS,
                   help="longest cue, in characters (default 42: one short line; 84 gives two-line cues)")
    p.add_argument("--llm-model", default=llm.DEFAULT_MODEL, help="Claude model for the editing and research passes")
    p.add_argument("--refresh", action="store_true", help="redo the web research even if it was done before")
    p.add_argument("--no-plex", action="store_true",
                   help="leave Plex alone: do not attach the subtitle, do not read one that is attached")
    p.add_argument("--plex-url", default=os.environ.get("PLEX_URL", "http://localhost:32400"))
    p.add_argument("--plex-token-file", default=None, help="file containing the Plex token (or set PLEX_TOKEN)")
    p.add_argument("--rating-key", default=None, help="the recording's Plex ratingKey, when known; saves looking it up")


def main(argv=None):
    p = argparse.ArgumentParser(prog="plex-cc-fixer", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    ep = sub.add_parser("episode", help="build a subtitle for one recording and attach it to Plex")
    add_recording_arguments(ep)
    ep.add_argument("--polish", action="store_true",
                    help="have Claude (the `claude` CLI) fix misheard words and split cues that would show a punchline early")
    ep.add_argument("--variant", default=None,
                    help="name for an alternate version, e.g. v2: files and the Plex subtitle are named <code>.<variant>.en")
    ep.add_argument("--site", default=None, help="with --polish: also write the cheat sheet pages into this folder")
    ep.add_argument("--force", action="store_true", help="redo audio, captions and transcription")
    ep.set_defaults(func=cmd_episode)

    br = sub.add_parser("brief", help="research an episode on the web and write a spoiler-free cheat sheet page; "
                                      "uses a subtitle already attached in Plex and transcribes only when there is none")
    add_recording_arguments(br)
    br.add_argument("--site", required=True, help="folder to write the pages into; serve it with any static web server")
    br.set_defaults(func=cmd_brief)

    sv = sub.add_parser("serve", help="the daemon: an API for interfaces, the cheat sheet pages, generation jobs")
    sv.add_argument("--site", required=True, help="folder holding the cheat sheet pages")
    sv.add_argument("--work", required=True, help="folder for episode work files; one subfolder per show")
    sv.add_argument("--port", type=int, default=8766)
    sv.add_argument("--listen", default="127.0.0.1")
    sv.add_argument("--plex-url", default=os.environ.get("PLEX_URL", "http://localhost:32400"))
    sv.add_argument("--plex-token-file", default=None, help="file containing the Plex token (or set PLEX_TOKEN)")
    sv.add_argument("--threads", type=int, default=3, help="CPU threads Whisper may use in a generation job")
    sv.set_defaults(func=cmd_serve)

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
