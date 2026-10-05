import json
import math
import os
import re

from .captions import align, build_mapper, cc_tokens, tidy

# A cue is a dict: start, end, text, source ("speech" or "captions") and, for speech,
# words: the [start, end, text] entries it was built from, so it can be re-cut later.

DEFAULT_MAX_CHARS = 42
MIN_DUR, MAX_DUR, PAUSE_BREAK, MIN_GAP = 0.9, 5.0, 0.6, 0.08
LINGER = 0.35
MIN_SHOWN = 0.4
FALLBACK_MIN_DUR, FALLBACK_MAX_DUR = 1.5, 6.0
OVERLAP_TOL = 0.3
SOUND_NEAR_SPEECH = 4.0
SENTENCE_END = re.compile(r"[.!?][\"')\]]?$")
CLAUSE_END = re.compile(r"[,;:][\"')\]]?$|-$")
ABBREVIATIONS = {"mr.", "mrs.", "ms.", "dr.", "st.", "vs.", "jr.", "sr.", "prof.", "gen.", "sen.", "rep.", "gov.",
                 "lt.", "col.", "sgt.", "capt.", "mt.", "no.", "u.s."}
MIN_PIECE_SECONDS = 0.7


def asr_words(audio, model, device="cpu", compute="int8", language="en", prompt=None,
              beam_size=1, log=print, **transcribe_kwargs):
    from faster_whisper import WhisperModel
    whisper = WhisperModel(model, device=device, compute_type=compute)
    segments, _ = whisper.transcribe(audio, language=language, word_timestamps=True, vad_filter=True,
                                     beam_size=beam_size, initial_prompt=prompt, **transcribe_kwargs)
    words = []
    for seg in segments:
        for w in seg.words or []:
            if w.word.strip():
                words.append([w.start, w.end, w.word.strip()])
        log(f"[asr] {seg.end:8.1f}s")
    return words


def transcribe_cached(audio, words_json, log=print, **kwargs):
    if os.path.exists(words_json):
        with open(words_json, encoding="utf-8") as f:
            words = json.load(f)
        log(f"[asr] loaded {len(words)} cached words from {words_json}")
        return words
    words = asr_words(audio, log=log, **kwargs)
    with open(words_json, "w", encoding="utf-8") as f:
        json.dump(words, f)
    log(f"[asr] {len(words)} words")
    return words


def render(words):
    """Join words into cue text. Whisper emits "six -three" and "$7 .36" as separate tokens."""
    text = re.sub(r"(\w) -(\w)", r"\1-\2", " ".join(w[2] for w in words))
    return re.sub(r"(\d) \.(\d)", r"\1.\2", text)


def ends_sentence(token):
    """True for "done." and "what?", false for "Mr." and for initials such as the "U." of "U. S."."""
    if not SENTENCE_END.search(token):
        return False
    return token.lower() not in ABBREVIATIONS and not re.fullmatch(r"[A-Z]\.", token)


def speech_cue(words):
    return {"start": words[0][0], "end": words[-1][1], "text": render(words), "source": "speech", "words": list(words)}


def _pieces(words, max_chars):
    """Cut one utterance into the fewest pieces that fit, of even length, preferring to cut after a comma."""
    total = len(render(words))
    count = max(math.ceil(total / max_chars), math.ceil((words[-1][1] - words[0][0]) / MAX_DUR))
    if count <= 1 or len(words) < 2:
        return [words]
    target = total / count
    best, best_cost, last_fit = None, None, 1
    for i in range(1, len(words)):
        left = len(render(words[:i]))
        if left > max_chars:
            break
        last_fit = i
        if len(render(words[i:])) > (count - 1) * max_chars:
            continue
        cost = abs(left - target) - (max_chars * 0.25 if CLAUSE_END.search(words[i - 1][2]) else 0)
        # A piece spoken in a fraction of a second flashes past; avoid cutting one off.
        if min(words[i - 1][1] - words[0][0], words[-1][1] - words[i][0]) < MIN_PIECE_SECONDS:
            cost += max_chars
        if best_cost is None or cost < best_cost:
            best, best_cost = i, cost
    best = best or last_fit
    return [words[:best]] + _pieces(words[best:], max_chars)


def resegment(words, max_chars=DEFAULT_MAX_CHARS):
    """Cut the word list into short cues.

    Sentences and pauses mark where a cue must end. A sentence too long for one cue is cut
    into even pieces. Quick interjections in a row ("Yes! Come on!") share a cue so they
    do not flicker past."""
    utterances, buf = [], []
    for word in words:
        if buf and word[0] - buf[-1][1] >= PAUSE_BREAK:
            utterances.append(buf)
            buf = []
        buf.append(word)
        if ends_sentence(word[2]):
            utterances.append(buf)
            buf = []
    if buf:
        utterances.append(buf)

    joined = []
    for utterance in utterances:
        previous = joined[-1] if joined else None
        if (previous and utterance[0][0] - previous[-1][1] < 0.3 and len(render(previous)) <= 12
                and len(render(previous + utterance)) <= max_chars * 0.67):
            joined[-1] = previous + utterance
        else:
            joined.append(utterance)

    return [speech_cue(piece) for utterance in joined for piece in _pieces(utterance, max_chars)]


def settle(cues):
    """Sort cues and fix their timing: no overlaps, a minimum time on screen, a short linger after the last word.

    Speech cues are timed from their words each time, so calling this again changes nothing."""
    for cue in cues:
        if cue.get("words"):
            cue["start"], cue["end"] = cue["words"][0][0], cue["words"][-1][1]
    cues.sort(key=lambda cue: cue["start"])
    # A caption-sourced cue has only approximate timing; move it rather than let it sit on top of speech.
    for i in range(1, len(cues)):
        previous, cue = cues[i - 1], cues[i]
        if not cue.get("words") and cue["start"] < previous["start"] + MIN_SHOWN:
            length = cue["end"] - cue["start"]
            cue["start"] = previous["start"] + MIN_SHOWN
            cue["end"] = cue["start"] + length
    cues.sort(key=lambda cue: cue["start"])
    for i, cue in enumerate(cues):
        nxt = cues[i + 1]["start"] if i + 1 < len(cues) else float("inf")
        wanted = max(cue["end"] + LINGER, cue["start"] + MIN_DUR) if cue["source"] == "speech" else cue["end"]
        cue["end"] = max(cue["start"] + 0.1, min(wanted, nxt - MIN_GAP))
    # A caption-sourced cue squeezed to a flash by the speech after it is not readable; drop it.
    cues[:] = [cue for cue in cues if cue.get("words") or cue["end"] - cue["start"] >= MIN_SHOWN]
    return cues


def build_subtitles(words, cc_lines, max_chars=DEFAULT_MAX_CHARS, log=print):
    """Merge speech cues with broadcast-caption lines that speech recognition missed.

    Returns (cues, fallback, reference): the merged cues, the caption-sourced ones among them,
    and every caption line as (corrected time, text) for use as a second witness.
    """
    speech = resegment(words, max_chars)
    ctoks = cc_tokens(cc_lines)
    anchors, matched = align(words, ctoks)
    mapper = build_mapper(anchors, log)

    line_tot = [0] * len(cc_lines)
    line_hit = [0] * len(cc_lines)
    for k, (_, n, li) in enumerate(ctoks):
        if n:
            line_tot[li] += 1
            line_hit[li] += k in matched

    spans = [(cue["start"], cue["end"]) for cue in speech]
    fallback, skipped_overlap, skipped_sound = [], 0, 0
    for i, (t, text) in enumerate(cc_lines):
        if line_tot[i] == 0 or line_hit[i] / line_tot[i] >= 0.5:
            continue
        s = mapper(t)
        if s < 0:
            continue
        nxt = cc_lines[i + 1][0] if i + 1 < len(cc_lines) else t + FALLBACK_MIN_DUR
        e = min(max(mapper(nxt), s + FALLBACK_MIN_DUR), s + FALLBACK_MAX_DUR)
        if any(a < e - OVERLAP_TOL and b > s + OVERLAP_TOL for a, b in spans):
            skipped_overlap += 1
            continue
        if text.startswith("[") and not any(b >= s - SOUND_NEAR_SPEECH and a <= e + SOUND_NEAR_SPEECH for a, b in spans):
            skipped_sound += 1
            continue
        fallback.append([s, e, tidy(text)])

    merged = []
    for s, e, text in fallback:
        if (merged and s - merged[-1]["end"] < 1.0 and len(merged[-1]["text"]) + 1 + len(text) <= max_chars
                and e - merged[-1]["start"] <= FALLBACK_MAX_DUR):
            merged[-1]["end"] = max(merged[-1]["end"], e)
            merged[-1]["text"] += " " + text
        else:
            merged.append({"start": s, "end": e, "text": text, "source": "captions", "words": None})

    cues = settle(speech + merged)
    reference = [(mapper(t), tidy(text)) for t, text in cc_lines]
    log(f"[captions] {len(cc_lines)} unique caption lines; {len(merged)} filled into "
        f"{len(speech)} speech cues (skipped {skipped_overlap} overlapping speech, "
        f"{skipped_sound} sound tags away from speech)")
    return cues, merged, reference
