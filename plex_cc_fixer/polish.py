"""Have Claude proofread the cues: words, punctuation and capitals, then cut them again at the sentences it found."""
import difflib
from concurrent.futures import ThreadPoolExecutor

from .captions import norm, srt_ts
from .llm import LLMError
from .speech import DEFAULT_MAX_CHARS, render, resegment, settle

CHUNK_CUES = 90
WORKERS = 3
REVEAL = "|"  # in Claude's text, right before the first word of a punchline
UNRELATED = 0.25  # a long cue sharing less than this with Claude's text was answered for another cue
MAX_FAILED = 0.25  # the run fails if more than this share of the stretches gets no answer

SCHEMA = {
    "type": "object",
    "properties": {
        "cues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "text": {"type": "string"}},
                "required": ["id", "text"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["cues"],
    "additionalProperties": False,
}

PROMPT = """You are the last editor of machine-made subtitles for one stretch of a TV recording, before someone watches it.

How they were made: a speech recogniser transcribed the audio and the words were cut into short lines. It hears most things right, but it mishears names, brands, slang and words spoken through laughter, and writes something that sounds similar (for example "jay" for the cast member "Che"). It also often leaves out punctuation and capitals, for long stretches at a time, and the lines were cut without knowing where its sentences end. The broadcaster's own closed captions for the same stretch are included as a second witness. A person typed those live, so their wording is usually right where the recogniser slipped, but they skip and paraphrase, contain typos of their own, and their times are only approximate.

About the programme:
{facts}

Read the lines one after another as continuous speech. A line can stop in the middle of a sentence, and a new sentence can start in the middle of a line; that is expected, and the lines are cut again from your punctuation afterwards. There are three jobs.

1. Punctuation and capitals. Make the text read the way a professional subtitler would write it: a capital at the start of every sentence and on names, a full stop, question mark or exclamation mark at the end of every sentence, commas, apostrophes, numbers as usual. Do this everywhere it is wrong, even if that is nearly every line. The end of a sentence matters most: when a new sentence starts inside a line, end the previous one there and capitalise the first word of the new one. Every sentence must end, so that nothing runs on into the next thing said. Do not add a capital to a word just because a line starts with it.

2. Wrong words. Where the recogniser clearly wrote the wrong word or name, correct it. Your evidence can be the captions, the facts above, or a sentence that makes no sense as written when a like-sounding word would. Change only what was misheard. Keep the speaker's own wording, grammar, slang and repetitions; do not add words that were not spoken. If you are not confident about a word, leave it.

3. Punchline timing. A line appears on screen all at once, at the moment its first word is spoken. When one line holds both the end of a setup and the reveal (the punchline, the surprise word, the answer), the viewer reads the reveal before the performer says it, and the joke is spoiled. Put {reveal} right before the first word of the reveal, so that the reveal is shown only when it is spoken. Mark only places where this matters; most lines need nothing, and never mark a line's first word.

The lines, as id | time | source | text. Source S came from speech recognition. Source C was copied from the captions: give those the first job only, and leave their words alone.

{cues}

The broadcaster's captions for this stretch, as time | text:

{reference}

Answer with `cues`: for each line that needs any change, its id and its full new text. Leave out lines that are already right. Keep every word on the line it is on, in the same order, even when a sentence crosses to the next line."""


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
    # Sung lines placed from published lyrics are final; there is nothing to check in them.
    cue_lines = "\n".join(
        f"{i} | {clock(cues[i]['start'])} | {'S' if cues[i]['source'] == 'speech' else 'C'} | {cues[i]['text']}"
        for i in range(first, last) if cues[i]["source"] != "lyrics")
    ref_lines = "\n".join(f"{clock(t)} | {text}" for t, text in reference if start - 12 <= t <= end + 12)
    return PROMPT.format(facts=facts_text(facts, start, end), cues=cue_lines, reveal=REVEAL,
                         reference=ref_lines or "(none for this stretch)")


def read_text(text):
    """Claude's text for a cue, as its words and the index of the word marked as the reveal (None if none)."""
    tokens, reveal, pending = [], None, False
    for token in text.split():
        if token.startswith(REVEAL):
            pending, token = True, token[len(REVEAL):]
            if not token:
                continue
        if pending:
            reveal, pending = len(tokens), False
        tokens.append(token)
    return tokens, reveal


def apply_text(cue, text):
    """Put Claude's text on a speech cue, keeping the timing of each word it kept.

    Returns the corrected spans as (old words, new words) and whether a reveal was marked, or None for an
    answer that cannot be for this cue."""
    tokens, reveal = read_text(text)
    old = [norm(word[2]) for word in cue["words"]]
    new = [norm(token) for token in tokens]
    if not tokens:
        return None
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    if len(old) >= 6 and matcher.ratio() < UNRELATED:
        return None
    words, spans = [], []
    for op, a, b, c, d in matcher.get_opcodes():
        if op == "equal":
            words.extend([word[0], word[1], token] for word, token in zip(cue["words"][a:b], tokens[c:d]))
            continue
        if a < b:
            t0, t1 = cue["words"][a][0], cue["words"][b - 1][1]
        else:  # a word with no time of its own: right after the one before it, or at the start
            t0 = t1 = words[-1][1] if words else cue["words"][0][0]
        step = (t1 - t0) / max(1, d - c)
        words.extend([t0 + k * step, t0 + (k + 1) * step, tokens[c + k]] for k in range(d - c))
        spans.append((render(cue["words"][a:b]), " ".join(tokens[c:d])))
    if not words:
        return None
    marked = False
    if reveal is not None and 0 < reveal < len(words):
        words[reveal] = words[reveal] + [True]
        marked = True
    cue["words"], cue["text"] = words, render(words)
    return spans, marked


def apply_answer(cues, first, last, answer):
    """Apply Claude's answer for one stretch. Returns the changes to list, the cues retouched and the answers refused."""
    changes, retouched, rejected = [], 0, 0
    for item in answer.get("cues", []):
        i, text = item.get("id", -1), (item.get("text") or "").strip()
        if not first <= i < last or not text or cues[i]["source"] == "lyrics":
            rejected += 1
            continue
        cue, was = cues[i], cues[i]["text"]
        if cue["source"] != "speech":
            cue["text"] = " ".join(read_text(text)[0])  # no word times to keep, and nothing to split
            retouched += cue["text"] != was
            continue
        applied = apply_text(cue, text)
        if applied is None:
            rejected += 1
            continue
        spans, marked = applied
        retouched += cue["text"] != was
        for old, new in spans:
            changes.append({"time": cue["start"], "kind": "word", "old": old, "new": new, "was": was, "now": cue["text"]})
        if marked:
            at = next(k for k, word in enumerate(cue["words"]) if len(word) > 3)
            left, right = render(cue["words"][:at]), render(cue["words"][at:])
            changes.append({"time": cue["words"][at][0], "kind": "split", "old": cue["text"], "new": f"{left} / {right}",
                            "was": was, "now": right})
    return changes, retouched, rejected


def polish(cues, reference, facts, claude, max_chars=DEFAULT_MAX_CHARS, log=print):
    """Ask Claude about each stretch, apply what it returns, cut the speech into cues again at the sentences it
    found, and report every change of words.

    Returns (cues, changes) where changes is a list of dicts for review.
    """
    segments = facts.get("segments", [])
    # A recording runs on either side of the programme, into local news and advertising nobody will read.
    ranges = [(first, last) for first, last in chunk_ranges(cues) if not segments or any(
        s.get("start_seconds", 0) < cues[last - 1]["end"] and s.get("end_seconds", 0) > cues[first]["start"] for s in segments)]
    failures = []

    def ask(bounds):
        try:
            return claude.ask(build_prompt(cues, bounds[0], bounds[1], reference, facts), SCHEMA, effort="high")
        except LLMError as error:
            log(f"[polish] stretch {clock(cues[bounds[0]]['start'])} skipped: {error}")
            failures.append(str(error))
            return {"cues": []}

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        answers = list(pool.map(ask, ranges))

    # A few stretches can go unedited; a subtitle that was hardly edited at all should not pass for an edited one.
    if len(failures) > len(ranges) * MAX_FAILED:
        raise LLMError(f"{len(failures)} of {len(ranges)} stretches got no answer: {failures[0]}")

    changes, retouched, rejected = [], 0, 0
    for (first, last), answer in zip(ranges, answers):
        found, touched, refused = apply_answer(cues, first, last, answer)
        changes += found
        retouched += touched
        rejected += refused

    words = [word for cue in cues if cue["source"] == "speech" for word in cue["words"]]
    result = resegment(words, max_chars) + [cue for cue in cues if cue["source"] != "speech"]
    corrected = sum(1 for change in changes if change["kind"] == "word")
    log(f"[polish] {len(ranges)} stretches: {retouched} lines retouched, {corrected} words corrected, "
        f"{len(changes) - corrected} punchlines held back, {rejected} answers not applicable")
    changes.sort(key=lambda change: change["time"])
    return settle(result), changes
