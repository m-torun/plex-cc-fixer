"""Show the published lyrics during a song, placed on the performance by Claude.

The transcript of a recording has next to nothing during a song, because its voice filter takes singing for noise.
With the filter off Whisper mishears most of a song but gets enough runs of words, with good times, to pin it down;
without them Claude's placing moved by seconds from one try to the next. Claude gets the lyrics, that and the
captions, and answers with line numbers and times, not with the words themselves, so the text on screen is the
published text apart from lines sung differently.
"""
import json
import os
import re
import tempfile
import urllib.parse
import urllib.request

from . import media, speech
from .captions import norm, srt_ts
from .llm import LLMError

SEARCH = "https://lrclib.net/api/search"  # free, no key: published lyrics, many with a time for each line
AROUND = 12        # seconds of evidence either side of the segment
MIN_LINES = 4      # fewer placed lines than this is not a song placed
LINE_SECONDS = 6.0  # how long a line stays up when nothing is sung right after it
MIN_LINE_SECONDS = 1.0
HEARD_LINE_WORDS = 10

SCHEMA = {
    "type": "object",
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line": {"type": "integer"},
                    "text": {"type": "string"},
                    "start": {"type": "string"},
                },
                "required": ["line", "text", "start"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["lines"],
    "additionalProperties": False,
}

PROMPT = """A song is performed live in a TV recording. Its subtitles should show the published lyrics, each line on screen when it is sung. Give the running order of this performance and when each line starts.

The published lyrics, as number | time in the studio recording, if known | line:

{lyrics}

Two imperfect records of the performance, in the recording's clock. The performance is somewhere from {start} to {end}, which may include an introduction and applause.

A speech recogniser, as time | words. It mishears most singing and can repeat itself or invent words over the instruments, but where a run of its words matches the lyrics, its time is good to within a second. Those runs are your best evidence for when a line is sung:

{heard}

The broadcast captions, as time | text. A person typed these live, so the words are usually right, but they skip lines. Their times are corrected by the delay measured on the speech around the song, but during singing they can still be off by a few seconds, usually late, so trust a matching run above over them:

{captions}

Work out what was actually sung. A live performance can skip or repeat a section, change words, add lines and run at its own pace, so go by the evidence and do not simply copy the studio order. Fix the moments you can pin down from the records, then use the studio times, or the length of the lines if there are none, to estimate the lines between them. This is your best estimate; give every line that was sung even where you are unsure of its exact time.

Answer with `lines`, in the order sung, with an entry for each time a line is sung:
- line: its number in the published lyrics, or 0 if it is not in them.
- text: empty when it was sung as published; otherwise the words as sung, from the captions. Use this only for a line that is not published or clearly differs.
- start: when its first word is sung, as H:MM:SS.s in the recording's clock."""


def clock(seconds):
    return srt_ts(seconds)[:8]


def seconds(text):
    """H:MM:SS.s as seconds, or None for anything else."""
    match = re.fullmatch(r"(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)", text.strip())
    if not match:
        return None
    return int(match.group(1) or 0) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


def parse_lyrics(found):
    """A search result's lines as (studio seconds or None, text)."""
    lines = []
    for line in (found.get("syncedLyrics") or "").splitlines():
        match = re.match(r"\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)", line)
        if match and match.group(3).strip():
            lines.append((int(match.group(1)) * 60 + float(match.group(2)), match.group(3).strip()))
    if lines:
        return lines
    return [(None, line.strip()) for line in (found.get("plainLyrics") or "").splitlines() if line.strip()]


def choose(results, artist, title):
    """The best search result for this song: the right names first, then one with a time for each line."""
    def right(result):
        return norm(title) in norm(result.get("trackName") or "") and norm(artist) in norm(result.get("artistName") or "")

    usable = [r for r in results if right(r) and not r.get("instrumental") and parse_lyrics(r)]
    usable.sort(key=lambda r: (not r.get("syncedLyrics"), len(norm(r.get("trackName") or ""))))
    return usable[0] if usable else None


def fetch(artist, title, timeout=20):
    """The published lyrics of a song as (studio seconds or None, text) lines; empty when none are found."""
    url = SEARCH + "?" + urllib.parse.urlencode({"artist_name": artist, "track_name": title})
    request = urllib.request.Request(url, headers={"User-Agent": "plex-cc-fixer (https://github.com/m-torun/plex-cc-fixer)"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        found = choose(json.load(response), artist, title)
    return parse_lyrics(found) if found else []


def listen(audio, start, end, path, artist="", model="small.en", threads=0):
    """What Whisper hears from `start` to `end` of the audio with its voice filter off, kept in the file `path`."""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    with tempfile.TemporaryDirectory() as folder:
        clip = os.path.join(folder, "song.flac")
        media.extract_audio(audio, clip, start=start, duration=end - start)
        words = speech.asr_words(clip, model, threads=threads, prompt=f"Song lyrics, sung by {artist}.", beam_size=5,
                                 log=lambda _line: None, vad_filter=False, no_speech_threshold=0.9)
    words = [[word[0] + start, word[1] + start, word[2]] for word in words]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(words, f)
    return words


def transcribed(cues, start, end):
    """The words the recording's transcript has during the segment."""
    return [word for cue in cues if cue["source"] == "speech" and cue["end"] >= start and cue["start"] <= end
            for word in cue["words"]]


def heard_lines(words):
    """Timed words as lines, a new one at each pause and every few words, each with the time of its first word."""
    lines, run = [], []
    for word in words:
        if run and (word[0] - run[-1][1] > 1.0 or len(run) >= HEARD_LINE_WORDS):
            lines.append(run)
            run = []
        run.append(word)
    if run:
        lines.append(run)
    return "\n".join(f"{srt_ts(run[0][0])[:10].replace(',', '.')} | {' '.join(word[2] for word in run)}"
                     for run in lines) or "(nothing)"


def build_prompt(lyrics, heard, reference, start, end):
    numbered = "\n".join(f"{n} | {clock(at) if at is not None else '-'} | {text}" for n, (at, text) in enumerate(lyrics, 1))
    captions = "\n".join(f"{clock(t)} | {text}" for t, text in reference if start - AROUND <= t <= end + AROUND)
    return PROMPT.format(lyrics=numbered, start=clock(start), end=clock(end), heard=heard_lines(heard),
                         captions=captions or "(none)")


def lyric_cues(answer, lyrics, start, end):
    """Claude's answer as cues in time order, keeping only what makes sense: known lines, times in the segment."""
    placed = []
    for item in answer.get("lines", []):
        at, number = seconds(item.get("start") or ""), item.get("line", 0)
        text = (item.get("text") or "").strip() or (lyrics[number - 1][1] if 1 <= number <= len(lyrics) else "")
        if at is not None and text and start - AROUND <= at <= end + AROUND:
            placed.append((at, text))
    placed.sort(key=lambda line: line[0])
    cues = []
    for i, (at, text) in enumerate(placed):
        next_at = placed[i + 1][0] if i + 1 < len(placed) else float("inf")
        if next_at - at < MIN_LINE_SECONDS:
            continue  # two lines given the same moment: the later one takes it
        cues.append({"start": at, "end": min(at + LINE_SECONDS, next_at), "text": f"♪ {text} ♪", "source": "lyrics", "words": None})
    return cues


def place(cues, facts, reference, claude, hear=None, find=fetch, log=print):
    """Replace what was heard during each song in `facts` with its published lyrics. Returns the new cue list.

    `hear(start, end, artist)` gives timed words for a stretch of the audio; without it, or if it fails, the
    transcript's words are the evidence. A song whose lyrics or placing cannot be had keeps the cues it has."""
    for segment in facts.get("segments", []):
        if segment.get("kind") != "music" or not segment.get("songs"):
            continue
        name = segment.get("title", "")
        lyrics = []
        for song in segment["songs"]:
            try:
                lyrics += find(song.get("artist", ""), song.get("title", ""))
            except (OSError, ValueError) as error:
                log(f"[lyrics] {song.get('title', '')}: could not look it up ({error})")
        if not lyrics:
            log(f"[lyrics] {name}: no published lyrics found; keeping what was heard")
            continue
        start, end = segment.get("start_seconds", 0), segment.get("end_seconds", 0)
        heard = None
        if hear:
            try:
                heard = hear(start, end, segment["songs"][0].get("artist", ""))
            except Exception as error:  # best effort: the transcript's words are still evidence
                log(f"[lyrics] {name}: could not listen to it ({error})")
        if heard is None:
            heard = transcribed(cues, start, end)
        try:
            answer = claude.ask(build_prompt(lyrics, heard, reference, start, end), SCHEMA, effort="high")
        except LLMError as error:
            log(f"[lyrics] {name}: not placed ({error}); keeping what was heard")
            continue
        sung = lyric_cues(answer, lyrics, start, end)
        if len(sung) < MIN_LINES:
            log(f"[lyrics] {name}: only {len(sung)} lines placed; keeping what was heard")
            continue
        first, last = sung[0]["start"], sung[-1]["end"]
        kept = [cue for cue in cues if not first - 1 <= cue["start"] <= last]
        log(f"[lyrics] {name}: {len(sung)} lines from {clock(first)} to {clock(last)}, "
            f"in place of {len(cues) - len(kept)} cues")
        cues = sorted(kept + sung, key=lambda cue: cue["start"])
    return cues
