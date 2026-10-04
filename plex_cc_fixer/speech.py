import json
import os
import re

from .captions import align, build_mapper, cc_tokens, tidy

MAX_CHARS = 84
MIN_DUR, MAX_DUR, PAUSE_BREAK, MIN_GAP = 1.0, 7.0, 0.8, 0.08
FALLBACK_MIN_DUR, FALLBACK_MAX_DUR = 1.5, 6.0
OVERLAP_TOL = 0.3
SOUND_NEAR_SPEECH = 4.0
SENT_END = re.compile(r"[.!?]$")


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


def _make_cue(buf):
    return [buf[0][0], buf[-1][1], " ".join(w[2] for w in buf)]


def resegment(words):
    cues, buf, buf_chars, last_end = [], [], 0, 0.0
    for ws, we, txt in words:
        pause = ws - last_end if buf else 0.0
        proposed = buf_chars + len(txt) + (1 if buf else 0)
        cur_dur = (we - buf[0][0]) if buf else 0.0
        if buf and (pause >= PAUSE_BREAK or proposed > MAX_CHARS or cur_dur >= MAX_DUR):
            cues.append(_make_cue(buf))
            buf, buf_chars = [], 0
        buf.append((ws, we, txt))
        buf_chars += len(txt) + (1 if buf_chars else 0)
        last_end = we
        if SENT_END.search(txt) and buf_chars >= 25:
            cues.append(_make_cue(buf))
            buf, buf_chars = [], 0
    if buf:
        cues.append(_make_cue(buf))
    for i, cue in enumerate(cues):
        if cue[1] - cue[0] < MIN_DUR:
            cue[1] = cue[0] + MIN_DUR
        if i + 1 < len(cues) and cues[i + 1][0] - cue[1] < MIN_GAP:
            cue[1] = max(cue[0] + 0.5, cues[i + 1][0] - MIN_GAP)
    return cues


def build_subtitles(words, cc_lines, log=print):
    """Merge speech cues with broadcast-caption lines that speech recognition missed.

    Returns (cues, fallback) where cues is a list of [start, end, text, source] and fallback is the
    list of [start, end, text] caption-sourced cues.
    """
    whisper = resegment(words)
    ctoks = cc_tokens(cc_lines)
    anchors, matched = align(words, ctoks)
    mapper = build_mapper(anchors, log)

    line_tot = [0] * len(cc_lines)
    line_hit = [0] * len(cc_lines)
    for k, (_, n, li) in enumerate(ctoks):
        if n:
            line_tot[li] += 1
            line_hit[li] += k in matched

    fallback, skipped_overlap, skipped_sound = [], 0, 0
    for i, (t, text) in enumerate(cc_lines):
        if line_tot[i] == 0 or line_hit[i] / line_tot[i] >= 0.5:
            continue
        s = mapper(t)
        nxt = cc_lines[i + 1][0] if i + 1 < len(cc_lines) else t + FALLBACK_MIN_DUR
        e = min(max(mapper(nxt), s + FALLBACK_MIN_DUR), s + FALLBACK_MAX_DUR)
        if any(c[0] < e - OVERLAP_TOL and c[1] > s + OVERLAP_TOL for c in whisper):
            skipped_overlap += 1
            continue
        if text.startswith("[") and not any(c[1] >= s - SOUND_NEAR_SPEECH and c[0] <= e + SOUND_NEAR_SPEECH
                                             for c in whisper):
            skipped_sound += 1
            continue
        fallback.append([s, e, tidy(text)])

    merged = []
    for s, e, text in fallback:
        if merged and s - merged[-1][1] < 1.0 and len(merged[-1][2]) + 1 + len(text) <= MAX_CHARS:
            merged[-1][1] = max(merged[-1][1], e)
            merged[-1][2] += " " + text
        else:
            merged.append([s, e, text])

    cues = sorted([c + ["speech"] for c in whisper] + [m + ["captions"] for m in merged], key=lambda c: c[0])
    for i in range(len(cues) - 1):
        nxt = cues[i + 1][0]
        if cues[i][1] > nxt - MIN_GAP:
            cues[i][1] = max(cues[i][0] + 0.3, nxt - MIN_GAP)

    log(f"[captions] {len(cc_lines)} unique caption lines; {len(merged)} filled into "
        f"{len(whisper)} speech cues (skipped {skipped_overlap} overlapping speech, "
        f"{skipped_sound} sound tags away from speech)")
    return cues, merged
