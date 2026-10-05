# plex-cc-fixer

Generates subtitles for broadcast recordings that Plex has already captured, then attaches them to the matching episode in Plex.

It combines two sources:

- **Speech recognition** ([faster-whisper](https://github.com/SYSTRAN/faster-whisper)) gives accurate timing for the speech it can hear.
- **Broadcast closed captions** (CEA-608, embedded in the recording) have the text for everything the broadcaster captioned, including laughter, applause and singing, but their timing lags the audio by several seconds.

The captions are time-corrected against the speech, and only the caption lines that speech recognition missed are added. Those lines usually cover laughter, crowd reactions, and sung lyrics.

Two optional steps use Claude through the [Claude Code](https://claude.com/claude-code) command line:

- **An editing pass** fixes words the recogniser misheard (names above all) and splits cues so a punchline isn't on screen before it's spoken.
- **A cheat sheet** for the episode: what each segment refers to, researched on the web and written without spoilers, as a small web page for a phone.

## Requirements

- Python 3.8+
- `ffmpeg` and `ffprobe` on `PATH`. Built-in EIA-608 decoding (`cc_dec`) is required.
- A recording that contains embedded EIA-608 captions. Check with `ffprobe`, or look for a `Closed Captions` stream in Plex.
- Plex Media Server, if you want automatic attachment.
- For the two optional steps: the `claude` CLI, installed and logged in on the machine that runs this. No API key is needed.

```bash
python3 -m pip install -r requirements.txt
```

## Usage

Build a subtitle for one episode and attach it to Plex:

```bash
python3 -m plex_cc_fixer episode "/path/to/Show - S01E02 - Title.ts" \
    --prompt "Show Name, Guest Name, Musical Guest"
```

This writes the working files to `work/S01E02/` and attaches `S01E02.en.srt` to the Plex item whose media file is that `.ts` path. Re-running the same command reuses the audio, caption extraction, and transcription, and it won't attach a subtitle that's already there.

Useful options:

| Option | Purpose |
| --- | --- |
| `--prompt` | Names and terms Whisper should expect. Helps with spelling. |
| `--model` | Whisper model. `small.en` is the default, `medium.en` and `large-v3` are more accurate but slower. |
| `--device`, `--compute` | For example `--device cuda --compute float16` on an NVIDIA GPU. |
| `--audio-stream` | Absolute ffmpeg stream index. By default the first English track is used. |
| `--max-chars` | Longest cue in characters. The default, 42, gives one short line that appears close to when it's spoken. 84 gives two-line cues. |
| `--no-plex` | Write the SRT only. |
| `--force` | Redo audio, captions, and transcription. |

### Editing pass and cheat sheet

```bash
python3 -m plex_cc_fixer episode "/path/to/Show - S01E02 - Title.ts" \
    --polish --variant v2 --site /path/to/site
```

- `--polish` first researches the episode on the web (segments, cast, names), then sends the cues to Claude in stretches of about 90, together with the broadcast captions for the same stretch as a second witness. Claude returns word corrections and the cues to split; nothing else in the subtitle changes. Every change is listed in `work/<episode>/changes.<variant>.en.txt`.
- `--variant v2` names the result `S01E02.v2.en` in Plex and `subtitle.v2.en.srt` on disk, next to the plain version, so the two can be compared on the same episode.
- `--site DIR` also writes the cheat sheet (`S01E02.html`), a page listing the corrections, and an `index.html` covering every episode in that folder. Serve the folder with any static web server; `deploy/plex-cc-fixer-site.service` is a systemd unit that does it with Python's built-in one on port 8766. The pages have no login, so keep the port on your local network.
- `python3 -m plex_cc_fixer brief "<recording>" --site DIR` writes the cheat sheet without touching subtitles.

Answers from Claude are cached in `work/<episode>/llm/`, and the research in `research.json`, so a re-run costs nothing. `--refresh` redoes the research. The cheat sheet is best effort: it depends on recaps being published, and it can be wrong.

Compare Whisper settings on a short clip, then rate the outputs by ear:

```bash
python3 -m plex_cc_fixer variants "/path/to/Show - S01E02 - Title.ts" \
    --start 3450 --duration 240 --out work/compare --captions work/S01E02/captions.srt
```

This writes one SRT per configuration plus `summary.tsv`, and `reference_captions.srt` for the same window (the delayed broadcast captions).

## Plex configuration

The subtitle is attached through Plex's subtitle upload endpoint (`POST /library/metadata/<key>/subtitles`). This endpoint isn't in Plex's public API documentation, so it could change between server versions.

- Provide the token with `PLEX_TOKEN` or `--plex-token-file` (default `~/.config/plex-token`).
- Set the server with `PLEX_URL` or `--plex-url` (default `http://localhost:32400`).
- The tool matches the episode by its media file path, so it has to run on a machine where Plex sees the recording at that same path.

The tool uploads the subtitle and doesn't write a sidecar `.srt` next to the recording:

- An uploaded subtitle shows up in Plex immediately and needs no write access to the library folder.
- Plex does read sidecar files, but it was slow to register one placed next to an existing DVR recording: about ten minutes in one test, and not within eight hours in another.
- Using both gives two identical tracks on the episode.

An uploaded subtitle lives inside Plex and other media servers can't see it. If another server needs the subtitle, copy `work/<episode>/subtitle.en.srt` next to the recording as `<recording name>.en.srt`.

## How it works

1. **Audio** is extracted from the recording as mono 16 kHz FLAC.
2. **Captions** are decoded from the video stream with ffmpeg's `movie=...[out0+subcc]` filter and written as SRT.
3. **Transcription** runs Whisper with word timestamps, cached to `words.json`.
4. **Cue cutting**: each sentence becomes a cue that starts when its first word is spoken. A sentence longer than `--max-chars` is cut into the fewest pieces that fit, of even length, preferably after a comma. Quick interjections in a row share a cue.
5. **Caption cleanup**: the rolling captions repeat earlier lines in each cue. Each line is kept once, at the time it first appeared.
6. **Alignment**: speech words and caption words are matched with `difflib`. Each run of three or more matching words becomes an anchor that measures the caption delay at that point. The offsets are median-smoothed and interpolated, so the captions follow a delay that changes over the recording.
7. **Fallback selection**: a caption line is used only if fewer than half of its words matched speech, and it doesn't overlap a speech cue. Sound tags such as `[APPLAUSE]` are only used within 4 seconds of speech.
8. **Editing pass** (with `--polish`): Claude's word corrections are applied to the words they replace, keeping their timing, and the cues it marks are split at the word that starts the reveal.
9. **Output**: speech cues and fallback cues are merged in time order and overlaps are trimmed. A cue longer than 42 characters (only with a larger `--max-chars`) is wrapped onto two lines at a space.

`work/<episode>/fallback.txt` lists every caption-sourced cue, so you can check what was added.

## Limitations

- Caption text is not always accurate. Misspellings and mishearings from the captioner end up in the output.
- Repeated caption lines in a row ("Yes! Yes!") can be merged by the rolling-caption cleanup.
- Caption lines during commercial breaks can be added if the captions contain text and speech recognition found nothing there.
- Only English is supported for alignment and output.

## Layout

```
plex_cc_fixer/
  cli.py        command-line entry point (episode, brief, variants)
  media.py      ffmpeg/ffprobe calls: audio, captions, track selection
  captions.py   caption parsing, alignment, time correction, SRT writing
  speech.py     Whisper transcription, cue cutting, caption merge
  llm.py        runs the `claude` CLI and caches its answers
  polish.py     the editing pass: prompt, word corrections, cue splits
  brief.py      web research and the cheat sheet pages
  plex.py       Plex search and subtitle upload
  variants.py   configuration comparison on a clip
deploy/
  plex-cc-fixer-site.service   systemd unit serving the cheat sheet folder
```

## License

Not yet chosen.
