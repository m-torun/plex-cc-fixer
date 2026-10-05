"""Research an episode on the web and write a spoiler-free cheat sheet as a small static site."""
import datetime
import html
import json
import os
import urllib.parse

from .captions import srt_ts
from .sheets import clock, page, relative_page

KINDS = ["cold open", "monologue", "sketch", "pretaped", "news desk", "music", "interview", "goodnights", "other"]

SCHEMA = {
    "type": "object",
    "properties": {
        "episode": {
            "type": "object",
            "properties": {
                "show": {"type": "string"},
                "season_episode": {"type": "string"},
                "air_date": {"type": "string"},
                "host": {"type": "string"},
                "musical_guest": {"type": "string"},
                "cast": {"type": "array", "items": {"type": "string"}},
                "names_to_expect": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["show", "season_episode", "air_date", "host", "musical_guest", "cast", "names_to_expect"],
            "additionalProperties": False,
        },
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "kind": {"type": "string", "enum": KINDS},
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "premise": {"type": "string"},
                    "know_before": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"topic": {"type": "string"}, "explanation": {"type": "string"}},
                            "required": ["topic", "explanation"],
                            "additionalProperties": False,
                        },
                    },
                    "whos_who": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"performer": {"type": "string"}, "plays": {"type": "string"}},
                            "required": ["performer", "plays"],
                            "additionalProperties": False,
                        },
                    },
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "sources": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "kind", "start", "end", "premise", "know_before", "whos_who", "confidence", "sources"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["episode", "segments", "notes"],
    "additionalProperties": False,
}

PROMPT = """I recorded a TV episode off the air. I want two things from you: the list of its segments with their times, and for each segment the background a viewer needs in order to get the jokes.

The viewer is an adult who follows the news loosely and did not grow up in the United States. He misses jokes that lean on things he has not seen: a news story or social-media moment from recent weeks, an American advert, an old TV show or film, a celebrity's public image, a politician's verbal habits, a regional stereotype. He will read your notes on his phone just before watching each segment.

So the notes must give him the background and must not give away the segment itself: no jokes, no twists, no surprise guests, no ending. "This parodies Press Your Luck, a long-running game show where contestants shout 'no whammies' while a board flashes prizes" is the right kind of note. "Her character keeps hitting whammies until she has lost everything" is a spoiler. The same goes for titles: if a segment's usual title gives away its turn, give it a plain neutral title instead. In "who's who", an impression of a real person is fine to name; leave out an entry when the identity is itself the surprise.

The recording: {identity}. The file starts a little before the programme and includes the commercial breaks. Leave out the commercials and anything from other programmes before or after.

Work like this:
1. Search the web for this episode: the sketch list, recaps and reviews. Use them to name the segments and learn what each is about.
2. Match them to the transcript below and give each segment's start and end time, in the transcript's own clock.
3. For each segment, work out what it leans on, and search for anything you are not sure of (the advert being parodied, the news story, the person being impersonated). Explain each in a sentence or two of plain language. A segment that needs no background gets an empty list; do not pad.

This is best effort. Where you cannot find out what a segment refers to, say so and mark it low confidence. An honest gap is more useful than an invented explanation.

Also collect the names likely to be spoken (cast, host, guests, recurring characters, people impersonated), spelled correctly. They are used to fix the transcript, which was made by speech recognition and often misspells names.

What to return:
- episode: the show, season and episode, air date, host, musical guest, the cast appearing, and the names to expect.
- segments, in running order. For each: title; kind; start and end as H:MM:SS; premise (one spoiler-free line saying what kind of segment it is); know_before (topic and explanation pairs); whos_who (performer and who they play); confidence; sources (the pages you relied on).
- notes: anything the viewer should know about gaps in this sheet, in a sentence or two. Empty if none.

The transcript, as time | text:

{transcript}"""


def seconds(clock):
    try:
        parts = [float(p) for p in clock.strip().split(":")]
    except ValueError:
        return 0.0
    while len(parts) < 3:
        parts.insert(0, 0.0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def research(cues, identity, claude, log=print):
    """Ask Claude, with web search, for the episode's facts and segment notes."""
    transcript = "\n".join(f"{srt_ts(cue['start'])[:8]} | {cue['text']}" for cue in cues)
    facts = claude.ask(PROMPT.format(identity=identity, transcript=transcript), SCHEMA, effort="high",
                       tools=("WebSearch", "WebFetch"), timeout=2400)
    for segment in facts.get("segments", []):
        segment["start_seconds"] = seconds(segment.get("start", ""))
        segment["end_seconds"] = seconds(segment.get("end", ""))
    log(f"[research] {len(facts.get('segments', []))} segments, "
        f"{len(facts.get('episode', {}).get('names_to_expect', []))} names")
    return facts


def render_brief(code, facts, changes_page=None):
    e = html.escape
    episode = facts.get("episode", {})
    heading = " · ".join(x for x in (episode.get("show"), code) if x)
    people = " / ".join(x for x in (episode.get("host"), episode.get("musical_guest")) if x)
    body = [f"<h1>{e(heading)}</h1>", f"<p class=\"dim\">{e(people)}{' · aired ' + e(episode['air_date']) if episode.get('air_date') else ''}</p>",
            "<p class=\"dim\">Background only, no spoilers. Times are positions in the recording, commercials included.</p>"]
    segments = facts.get("segments", [])
    body.append("<nav>" + "".join(
        f"<a href=\"#s{n}\"><span class=\"when\">{clock(segment.get('start_seconds', 0))}</span> {e(segment.get('title', ''))}</a>"
        for n, segment in enumerate(segments, 1)) + "</nav>")
    if facts.get("notes"):
        body.append(f"<details><summary>What this sheet is missing</summary><p class=\"dim\">{e(facts['notes'])}</p></details>")
    for n, segment in enumerate(segments, 1):
        part = [f"<section class=\"seg\" id=\"s{n}\"><div><span class=\"when\">{clock(segment.get('start_seconds', 0))}</span> "
                f"<span class=\"kind\">{e(segment.get('kind', ''))}</span></div><h2>{e(segment.get('title', ''))}</h2>"]
        if segment.get("premise"):
            part.append(f"<p>{e(segment['premise'])}</p>")
        if segment.get("know_before"):
            part.append("<dl>" + "".join(f"<dt>{e(item.get('topic', ''))}</dt><dd>{e(item.get('explanation', ''))}</dd>"
                                         for item in segment["know_before"]) + "</dl>")
        elif segment.get("confidence") != "low":
            part.append("<p class=\"dim\">Nothing you need to know first.</p>")
        if segment.get("confidence") == "low":
            part.append("<p class=\"dim\">Not much could be found about this one.</p>")
        if segment.get("whos_who"):
            part.append("<details><summary>Who's who</summary><ul>" + "".join(
                f"<li>{e(p.get('performer', ''))} as {e(p.get('plays', ''))}</li>" for p in segment["whos_who"]) + "</ul></details>")
        links = [s for s in segment.get("sources", []) if s.startswith("http")]
        if links:
            part.append("<details><summary>Sources</summary><ul>" + "".join(
                f"<li><a href=\"{e(s, quote=True)}\">{e(s.split('/')[2])}</a></li>" for s in links) + "</ul></details>")
        part.append("</section>")
        body.append("".join(part))
    footer = f"<p class=\"dim\">Made {datetime.date.today().isoformat()} by plex-cc-fixer. Best effort; it can be wrong."
    if changes_page:
        footer += f" <a href=\"{e(changes_page, quote=True)}\">Subtitle corrections</a>."
    body.append(footer + " <a href=\"/\">Now playing and all sheets</a></p>")
    return page(f"{heading}: before you watch", "".join(body))


def render_changes(code, changes, sheet_page):
    e = html.escape
    rows = []
    for change in changes:
        if change["kind"] == "word":
            what = f"<del>{e(change['old'])}</del> → <strong>{e(change['new'])}</strong><br><span class=\"dim\">{e(change['now'])}</span>"
        else:
            what = f"split for timing<br><span class=\"dim\">{e(change['new'])}</span>"
        rows.append(f"<tr><td class=\"t\">{clock(change['time'])}</td><td>{what}</td></tr>")
    words = sum(1 for change in changes if change["kind"] == "word")
    body = (f"<h1>{e(code)}: subtitle corrections</h1>"
            f"<p class=\"dim\">{words} words corrected and {len(changes) - words} cues split by the editing pass. "
            "This page quotes the dialogue, so it can spoil jokes.</p>"
            f"<table>{''.join(rows)}</table><p class=\"dim\"><a href=\"{e(sheet_page, quote=True)}\">Back to the cheat sheet</a></p>")
    return page(f"{code}: subtitle corrections", body)


def write_site(site, media_path, code, facts, changes=None):
    """Write the episode's pages into `site`, at the place that mirrors the media file's place in the library."""
    base = os.path.join(site, relative_page(media_path))
    os.makedirs(os.path.dirname(base), exist_ok=True)
    name = urllib.parse.quote(os.path.basename(base))
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(dict(facts, code=code, media=os.path.basename(media_path)), f, indent=1)
    with open(base + ".html", "w", encoding="utf-8") as f:
        f.write(render_brief(code, facts, name + ".changes.html" if changes else None))
    if changes:
        with open(base + ".changes.html", "w", encoding="utf-8") as f:
            f.write(render_changes(code, changes, name + ".html"))
    return base + ".html"
