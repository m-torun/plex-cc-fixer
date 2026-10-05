import difflib
import re

import numpy as np

ANCHOR_MIN_RUN = 3
SMOOTH_WINDOW = 7
MAX_LINE = 42


def srt_ts(t):
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms == 1000:
        s += 1
        ms = 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def parse_ts(ts):
    h, m, rest = ts.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def norm(word):
    return re.sub(r"[^a-z0-9]", "", word.lower())


def clean_cc(line):
    line = re.sub(r"<[^>]+>", "", line)
    line = re.sub(r"\{\\[^}]*\}", "", line)
    line = line.replace("\\h", " ").replace("\\N", " ")
    return re.sub(r"\s+", " ", line).strip()


def load_cc(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        raw = f.read().strip()
    cues = []
    for block in re.split(r"\n\s*\n", raw):
        lines = block.splitlines()
        arrow = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if arrow is None:
            continue
        start = parse_ts(lines[arrow].split("-->")[0].strip())
        text = [t for t in (clean_cc(line) for line in lines[arrow + 1:]) if t]
        if text:
            cues.append((start, text))
    return cues


def dedupe_rolling(cues):
    """Rolling captions repeat the previous line(s) in each new cue; keep each line once."""
    lines, prev = [], []
    for start, cur in cues:
        k = 0
        for n in range(min(len(prev), len(cur)), 0, -1):
            if prev[-n:] == cur[:n]:
                k = n
                break
        lines.extend((start, t) for t in cur[k:])
        prev = cur
    return lines


def cc_tokens(lines):
    toks = []
    for i, (t, text) in enumerate(lines):
        nxt = lines[i + 1][0] if i + 1 < len(lines) else t + 2.0
        words = text.split()
        span = min(max(nxt - t, 0.0), max(1.0, 0.4 * len(words)))
        for j, w in enumerate(words):
            toks.append((t + span * j / len(words), norm(w), i))
    return toks


def align(words, ctoks):
    """Match speech words to caption words; returns anchors (caption time, speech time) and matched caption token indices."""
    wi = [i for i, w in enumerate(words) if norm(w[2])]
    ci = [k for k, tok in enumerate(ctoks) if tok[1]]
    sm = difflib.SequenceMatcher(None, [norm(words[i][2]) for i in wi],
                                 [ctoks[k][1] for k in ci], autojunk=False)
    matched, anchors = set(), []
    for a, b, size in sm.get_matching_blocks():
        if size == 0:
            continue
        matched.update(ci[b + x] for x in range(size))
        if size >= ANCHOR_MIN_RUN:
            anchors.append((ctoks[ci[b]][0], words[wi[a]][0]))
    return anchors, matched


def build_mapper(anchors, log=print):
    if not anchors:
        raise SystemExit("no alignment anchors: captions and speech do not overlap")
    ct = np.array([a[0] for a in anchors])
    off = np.array([a[1] for a in anchors]) - ct
    half = SMOOTH_WINDOW // 2
    smooth = np.array([np.median(off[max(0, i - half): i + half + 1]) for i in range(len(off))])
    log(f"[align] {len(anchors)} anchors; caption offset median {np.median(off):+.2f}s, "
        f"p10 {np.percentile(off, 10):+.2f}s, p90 {np.percentile(off, 90):+.2f}s")
    return lambda t: float(t + np.interp(t, ct, smooth))


def tidy(text):
    text = text.replace(">>", "-").strip()
    text = re.sub(r"^-\s*", "- ", text)
    if any(c.isalpha() for c in text) and text == text.upper():
        text = text.lower()
    text = re.sub(r"(^|[.!?]\s+|-\s+|\[\s*)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text)
    return re.sub(r"\bi\b", "I", text)


def wrap(text):
    """Break a long cue into two lines at a space, as evenly as possible. Never inside a word."""
    if len(text) <= MAX_LINE:
        return text
    words = text.split()
    best, best_cost = None, None
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        # Prefer both lines within the limit; among those, the most even pair.
        cost = (max(len(a), len(b)) > MAX_LINE, abs(len(a) - len(b)))
        if best_cost is None or cost < best_cost:
            best_cost, best = cost, (a, b)
    return best[0] + "\n" + best[1] if best else text


def write_srt(path, cues):
    with open(path, "w", encoding="utf-8") as f:
        for i, (s, e, text) in enumerate(cues, 1):
            f.write(f"{i}\n{srt_ts(s)} --> {srt_ts(e)}\n{wrap(text)}\n\n")
