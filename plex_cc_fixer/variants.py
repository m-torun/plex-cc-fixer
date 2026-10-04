"""Compare Whisper configurations on one clip; writes one SRT per configuration for side-by-side rating."""
import os

from . import captions, media, speech

VARIANTS = [
    ("01_baseline", {}),
    ("02_beam5", {"beam_size": 5}),
    ("03_no_vad", {"vad_filter": False}),
    ("04_vad_threshold_0.3", {"vad_parameters": {"threshold": 0.3, "min_silence_duration_ms": 1000}}),
    ("05_no_speech_0.8", {"no_speech_threshold": 0.8}),
    ("06_no_vad_no_speech_0.8", {"vad_filter": False, "no_speech_threshold": 0.8}),
    ("07_no_previous_context", {"condition_on_previous_text": False}),
]


def run(audio, start, duration, out_dir, model, device="cpu", compute="int8", prompt=None,
        audio_stream=None, cc_srt=None, log=print):
    os.makedirs(out_dir, exist_ok=True)
    clip = os.path.join(out_dir, "clip.flac")
    media.extract_audio(audio, clip, audio_stream, start=start, duration=duration)

    rows = []
    for name, extra in VARIANTS:
        words = speech.asr_words(clip, model, device=device, compute=compute, prompt=prompt, log=lambda _m: None,
                                 **extra)
        cues = speech.resegment(words)
        captions.write_srt(os.path.join(out_dir, name + ".srt"), [(s, e, t) for s, e, t in cues])
        covered = sum(e - s for s, e, _ in cues) / duration
        rows.append((name, str(len(words)), str(len(cues)), f"{covered:.0%}"))
        log(f"[done] {name}: {len(words)} words, {len(cues)} cues, {covered:.0%} covered")

    with open(os.path.join(out_dir, "summary.tsv"), "w", encoding="utf-8") as f:
        f.write("variant\twords\tcues\tcovered\n")
        for row in rows:
            f.write("\t".join(row) + "\n")

    if cc_srt:
        lines = captions.dedupe_rolling(captions.load_cc(cc_srt))
        window = [(t - start, x) for t, x in lines if start <= t < start + duration]
        cues = []
        for i, (t, x) in enumerate(window):
            end = window[i + 1][0] if i + 1 < len(window) else duration
            cues.append((t, min(max(end, t + 1.0), t + 6.0), captions.tidy(x)))
        captions.write_srt(os.path.join(out_dir, "reference_captions.srt"), cues)
        log(f"[ref] {len(cues)} caption lines -> reference_captions.srt (delayed, for comparison)")
