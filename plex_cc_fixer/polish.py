"""Have Claude proofread the cues: fix misheard words and split cues so a punchline is not shown early."""
import re
from concurrent.futures import ThreadPoolExecutor

from .captions import norm, srt_ts
from .llm import LLMError
from .speech import render, settle, speech_cue

CHUNK_CUES = 90
WORKERS = 3

SCHEMA = {
    "type": "object",
    "properties": {
        "fixes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                },
                "required": ["id", "old", "new"],
                "additionalProperties": False,
            },
        },
        "breaks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "before": {"type": "string"}},
                "required": ["id", "before"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["fixes", "breaks"],
    "additionalProperties": False,
}

PROMPT = """You are checking machine-made subtitles for one stretch of a TV recording before someone watches it.

How they were made: a speech recogniser transcribed the audio and the words were cut into short cues. It hears most things right, but it mishears names, brands, slang and words spoken through laughter, and writes something that sounds similar (for example "jay" for the cast member "Che"). The broadcaster's own closed captions for the same stretch are included as a second witness. A person typed those live, so their wording is usually right where the recogniser slipped, but they skip and paraphrase, contain typos of their own, and their times are only approximate.

About the programme:
{facts}

There are two jobs.

1. Wrong words. Where the recogniser clearly wrote the wrong word or name, give the correction. Your evidence can be the captions, the facts above, or a sentence that makes no sense as written when a like-sounding word would. Change only what was misheard. Keep the speaker's own wording, grammar, slang and repetitions; do not tidy style or punctuation; do not add words that were not spoken. If you are not confident, leave it alone.

2. Punchline timing. A cue appears on screen all at once, at the moment its first word is spoken. When one cue holds both the end of a setup and the reveal (the punchline, the surprise word, the answer), the viewer reads the reveal before the performer says it, and the joke is spoiled. For such a cue, name the first word of the reveal so the cue can be split there and the reveal shown only when it is spoken. Mark only cues where this matters; most need nothing.

The cues, as id | time | source | text. Source S came from speech recognition and can be corrected and split. Source C was copied from the captions; leave those as they are.

{cues}

The broadcaster's captions for this stretch, as time | text:

{reference}

Answer with two lists, either of which may be empty:
- fixes: for each correction, the cue id, `old` (the exact words in that cue to replace, as few as possible) and `new` (what they should be).
- breaks: for each split, the cue id and `before` (the exact word in that cue which starts the reveal; never the cue's first word)."""


def clock(seconds):
    return srt_ts(seconds)[:8]


def chunk_ranges(cues, size=CHUNK_CUES):
    """Cut the cue list into runs of about `size`, preferring to cut at the longest silence nearby."""
    ranges, start = [], 0
    while start < len(cues):
        end = min(start + size, len(cues))
        if end < len(cues):
            window = range(max(start + size // 2, end - 15), min(len(cues) - 1, end + 15))
            end = max(window, key=lambda i: cues[i]["start"] - cues[i - 1]["end"])
        ranges.append((start, end))
        start = end
    return ranges


def facts_text(facts, start, end):
    episode = facts.get("episode", {})
    lines = []
    for label, key in (("Show", "show"), ("Episode", "season_episode"), ("Aired", "air_date"), ("Host", "host"),
                       ("Musical guest", "musical_guest")):
        if episode.get(key):
            lines.append(f"{label}: {episode[key]}")
    for label, key in (("Cast", "cast"), ("Names likely to be spoken", "names_to_expect")):
        if episode.get(key):
            lines.append(f"{label}: {', '.join(episode[key])}")
    for segment in facts.get("segments", []):
        if segment.get("start_seconds", 0) < end and segment.get("end_seconds", 0) > start:
            line = f"This stretch includes the segment \"{segment.get('title', '')}\" ({segment.get('kind', '')})"
            if segment.get("premise"):
                line += f": {segment['premise']}"
            players = [f"{p.get('performer', '')} as {p.get('plays', '')}" for p in segment.get("whos_who", [])]
            if players:
                line += " With " + "; ".join(players) + "."
            lines.append(line)
    return "\n".join(lines) if lines else "Nothing is known beyond the transcript."


def build_prompt(cues, first, last, reference, facts):
    start, end = cues[first]["start"], cues[last - 1]["end"]
    cue_lines = "\n".join(
        f"{i} | {clock(cues[i]['start'])} | {'S' if cues[i]['source'] == 'speech' else 'C'} | {cues[i]['text']}"
        for i in range(first, last))
    ref_lines = "\n".join(f"{clock(t)} | {text}" for t, text in reference if start - 12 <= t <= end + 12)
    return PROMPT.format(facts=facts_text(facts, start, end), cues=cue_lines,
                         reference=ref_lines or "(none for this stretch)")


def _strip(token):
    """Split a word into leading punctuation, core and trailing punctuation."""
    match = re.match(r"^(\W*)(.*?)(\W*)$", token, re.S)
    return match.group(1), match.group(2), match.group(3)


def apply_fix(cue, old, new):
    """Replace the words `old` in a speech cue with `new`, keeping the timing of the span. Returns True if applied."""
    words, target = cue["words"], [norm(t) for t in old.split() if norm(t)]
    fresh = new.split()
    if not target or not fresh or old.strip() == new.strip():
        return False
    for i in range(len(words) - len(target) + 1):
        if [norm(w[2]) for w in words[i:i + len(target)]] != target:
            continue
        lead = _strip(words[i][2])[0]
        trail = _strip(words[i + len(target) - 1][2])[2]
        if not _strip(fresh[0])[0]:
            fresh[0] = lead + fresh[0]
        if not _strip(fresh[-1])[2]:
            fresh[-1] = fresh[-1] + trail
        t0, t1 = words[i][0], words[i + len(target) - 1][1]
        step = (t1 - t0) / len(fresh)
        cue["words"][i:i + len(target)] = [[t0 + k * step, t0 + (k + 1) * step, token] for k, token in enumerate(fresh)]
        cue["text"] = render(cue["words"])
        return True
    return False


def apply_break(cue, before):
    """Split a speech cue at the word `before`. Returns the two cues, or None if the word is not there."""
    target = norm(before.split()[0]) if before.split() else ""
    for k in range(1, len(cue["words"])):
        if target and norm(cue["words"][k][2]) == target:
            return speech_cue(cue["words"][:k]), speech_cue(cue["words"][k:])
    return None


def polish(cues, reference, facts, claude, log=print):
    """Ask Claude about each stretch, apply what it returns, and report every change.

    Returns (cues, changes) where changes is a list of dicts for review.
    """
    ranges = chunk_ranges(cues)

    def ask(bounds):
        try:
            return claude.ask(build_prompt(cues, bounds[0], bounds[1], reference, facts), SCHEMA, effort="high")
        except LLMError as error:
            log(f"[polish] stretch {clock(cues[bounds[0]]['start'])} skipped: {error}")
            return {"fixes": [], "breaks": []}

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        answers = list(pool.map(ask, ranges))

    changes, splits, rejected = [], {}, 0
    for (first, last), answer in zip(ranges, answers):
        for fix in answer.get("fixes", []):
            i = fix.get("id", -1)
            if not first <= i < last or cues[i]["source"] != "speech":
                rejected += 1
                continue
            before_text = cues[i]["text"]
            if apply_fix(cues[i], fix.get("old", ""), fix.get("new", "")):
                changes.append({"time": cues[i]["start"], "kind": "word", "old": fix["old"], "new": fix["new"],
                                "was": before_text, "now": cues[i]["text"]})
            else:
                rejected += 1
        for brk in answer.get("breaks", []):
            i = brk.get("id", -1)
            if not first <= i < last or cues[i]["source"] != "speech" or i in splits:
                rejected += 1
                continue
            parts = apply_break(cues[i], brk.get("before", ""))
            if parts:
                splits[i] = parts
                changes.append({"time": parts[1]["start"], "kind": "split", "old": cues[i]["text"],
                                "new": parts[0]["text"] + " / " + parts[1]["text"], "was": cues[i]["text"],
                                "now": parts[1]["text"]})
            else:
                rejected += 1

    result = []
    for i, cue in enumerate(cues):
        result.extend(splits[i] if i in splits else [cue])
    words = sum(1 for change in changes if change["kind"] == "word")
    log(f"[polish] {len(ranges)} stretches: {words} words corrected, {len(splits)} cues split for timing, "
        f"{rejected} suggestions not applicable")
    changes.sort(key=lambda change: change["time"])
    return settle(result), changes
