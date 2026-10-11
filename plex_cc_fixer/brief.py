"""Research an episode on the web and write a spoiler-free cheat sheet as a small static site."""
import datetime
import html
import json
import os
import urllib.parse

from .captions import srt_ts
from .sheets import clock, page, relative_page

KINDS = ["cold open", "monologue", "sketch", "pretaped", "news desk", "music", "interview", "goodnights", "other"]

LEAD_IN = 3  # seconds before a segment's start that its Play button jumps to

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
                    "label": {"type": "string"},
                    "kind": {"type": "string", "enum": KINDS},
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "know_before": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"topic": {"type": "string"}, "explanation": {"type": "string"}},
                            "required": ["topic", "explanation"],
                            "additionalProperties": False,
                        },
                    },
                    "title": {"type": "string"},
                    "premise": {"type": "string"},
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
                "required": ["label", "kind", "start", "end", "know_before", "title", "premise", "whos_who",
                             "confidence", "sources"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["episode", "segments", "notes"],
    "additionalProperties": False,
}

PROMPT = """I recorded a TV episode off the air. I want two things from you: the list of its segments with their times, and for each segment a short glossary of the background a viewer needs in order to get the jokes.

The viewer is an adult who follows the news loosely and did not grow up in the United States. He misses jokes that lean on things he has not seen: a news story or social-media moment from recent weeks, an American advert, an old TV show or film, a celebrity's public image, a politician's verbal habits, a regional stereotype. He reads your notes on his phone just before each segment, and uses the list to jump the playback to a segment.

He found an earlier version of these notes spoiled the show: it told him what each segment was about before he watched it. So nothing he sees may say what happens in a segment, what it is about, or how it uses the things you explain. Two fields are what he sees:

- label: the segment's name on his screen. Use the type of segment, and add a name only when that name is a fixture of the show or a performer and gives nothing away: "Cold open", "Monologue", "Sketch", "Filmed piece", the news segment under its own name, "Music: " and the artist, "Goodnights". Never a sketch's title, which usually gives away its idea.
- know_before: a glossary. Each entry names one real thing the segment assumes he knows (a person, a news story, an advert, a programme, a phrase, a stereotype) and says what it is and what it is known for, in one sentence, two at most, the way a reference book would. Write nothing about the segment: not that it parodies or mentions the thing, not who plays whom, not what the joke is, and no "the sketch", "here" or "in this segment". Right: "Press Your Luck: a long-running American game show where contestants shout 'no whammies' while a board flashes prizes." Wrong: "The sketch parodies Press Your Luck." Wrong: "Her character keeps hitting whammies." Give only what he would be lost without, usually one to four entries, and none for a segment that needs none; do not pad. Leave out an entry whose mere presence would give away a surprise, such as an unannounced guest.

Three more fields are for the subtitle editor and are never shown to him, so they should say plainly what the segment is: title (its usual title), premise (one line on what it is about) and whos_who (performer and who they play).

The recording: {identity}. The file starts a little before the programme and includes the commercial breaks. Leave out the commercials and anything from other programmes before or after.

Work like this:
1. Search the web for this episode: the sketch list, recaps and reviews. Use them to name the segments and learn what each is about.
2. Match them to the transcript below and give each segment's start and end time, in the transcript's own clock. He jumps to the start time, so put it at the segment's first words, not in the commercial before it.
3. For each segment, work out what it leans on, and search for anything you are not sure of (the advert being parodied, the news story, the person being impersonated).

Always look for the programme's sign-off when it has one: the host's thank-yous and goodbyes at the very end, after the last commercial break (kind goodnights). His player tends to skip it as if it were the credits, and this list is how he finds it, so give its start exactly. If the transcript ends before it, say in notes that the recording stops before the goodbyes.

This is best effort. Where you cannot find out what a segment refers to, leave its glossary empty and mark it low confidence. An honest gap is more useful than an invented explanation.

Also collect the names likely to be spoken (cast, host, guests, recurring characters, people impersonated), spelled correctly. They are used to fix the transcript, which often misspells names.

What to return:
- episode: the show, season and episode, air date, host, musical guest, the cast appearing, and the names to expect.
- segments, in running order. For each: label; kind; start and end as H:MM:SS; know_before (topic and explanation pairs); title, premise and whos_who for the editor; confidence; sources (the pages you relied on).
- notes: one or two sentences for the viewer about gaps in this sheet (a segment you could not place, a time you are unsure of, the recording stopping early), giving away nothing. Empty if none. Nothing else belongs there.

The transcript, as time | text:

{transcript}"""

# What the page does while it is open: asks the daemon whether this recording is playing, and sends a jump.
SCRIPT = """<script>
(function () {
  const status = document.getElementById("now");
  const buttons = Array.from(document.querySelectorAll("button.jump"));
  const rows = Array.from(document.querySelectorAll("nav .row"));
  const here = decodeURIComponent(location.pathname);
  let playing = null, said = "", saidUntil = 0;
  const clock = s => {
    s = Math.max(0, Math.floor(s));
    const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = String(s % 60).padStart(2, "0");
    return h ? h + ":" + String(m).padStart(2, "0") + ":" + x : m + ":" + x;
  };
  function show() {
    buttons.forEach(button => { button.disabled = !playing; });
    let current = null;
    if (playing) rows.forEach(row => { if (Number(row.dataset.at) <= playing.position + 1) current = row; });
    rows.forEach(row => row.classList.toggle("on", row === current));
    if (Date.now() < saidUntil) { status.textContent = said; return; }
    status.textContent = playing
      ? "Playing on " + playing.player + ", at " + clock(playing.position) + (playing.state === "paused" ? " (paused)" : "") + ". Play jumps to a part."
      : "Not playing in Plex right now. Start it there, and Play jumps to a part.";
  }
  function say(text) { said = text; saidUntil = Date.now() + 6000; status.textContent = text; }
  async function look() {
    try {
      const data = await (await fetch("/api/now", {cache: "no-store"})).json();
      playing = (data.playing || []).find(p => p.sheet && here.endsWith(decodeURIComponent(p.sheet))) || null;
    } catch (error) { playing = null; }
    show();
  }
  buttons.forEach(button => button.addEventListener("click", async () => {
    if (!playing) return;
    const at = Number(button.dataset.at), player = playing.player;
    say("Jumping to " + clock(at) + " on " + player + "…");
    try {
      const response = await fetch("/api/seek", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({key: playing.key, client: playing.client, position: at})});
      const answer = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(answer.error || response.statusText);
      say("Jumped to " + clock(at) + " on " + player + ".");
    } catch (error) { say("Could not jump: " + error.message); }
  }));
  look();
  setInterval(() => { if (document.visibilityState === "visible") look(); }, 3000);
})();
</script>"""


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


# A sketch's title says what it is about, so these kinds are shown by their type alone.
PLAIN = {"cold open": "Cold open", "monologue": "Monologue", "sketch": "Sketch", "pretaped": "Filmed piece",
         "news desk": "News desk", "interview": "Interview"}


def shown_name(segment):
    """What the viewer sees as a segment's name: nothing that gives away what happens in it."""
    return segment.get("label") or PLAIN.get(segment.get("kind"), segment.get("title") or segment.get("kind", ""))


def render_brief(code, facts, changes_page=None):
    e = html.escape
    episode = facts.get("episode", {})
    heading = " · ".join(x for x in (episode.get("show"), code) if x)
    people = " / ".join(x for x in (episode.get("host"), episode.get("musical_guest")) if x)
    body = [f"<h1>{e(heading)}</h1>", f"<p class=\"dim\">{e(people)}{' · aired ' + e(episode['air_date']) if episode.get('air_date') else ''}</p>",
            "<p class=\"dim\">Background only, no spoilers. Times are positions in the recording, commercials included.</p>",
            "<p id=\"now\" class=\"now\">Checking whether this is playing…</p>"]
    segments = facts.get("segments", [])

    def jump(segment, text):
        at = max(0, segment.get("start_seconds", 0) - LEAD_IN)
        return f"<button class=\"jump\" data-at=\"{at:.0f}\" disabled>{text}</button>"

    body.append("<nav>" + "".join(
        f"<div class=\"row\" data-at=\"{segment.get('start_seconds', 0):.0f}\"><a href=\"#s{n}\">"
        f"<span class=\"when\">{clock(segment.get('start_seconds', 0))}</span> {e(shown_name(segment))}</a>"
        f"{jump(segment, 'Play')}</div>"
        for n, segment in enumerate(segments, 1)) + "</nav>")
    if facts.get("notes"):
        body.append(f"<details><summary>What this sheet is missing</summary><p class=\"dim\">{e(facts['notes'])}</p></details>")
    for n, segment in enumerate(segments, 1):
        # The premise and the cast list stay in the facts for the subtitle editor; shown here they gave the segment away.
        part = [f"<section class=\"seg\" id=\"s{n}\"><div class=\"head\"><span class=\"when\">{clock(segment.get('start_seconds', 0))}</span>"
                f"{jump(segment, 'Play from here')}</div><h2>{e(shown_name(segment))}</h2>"]
        if segment.get("know_before"):
            part.append("<dl>" + "".join(f"<dt>{e(item.get('topic', ''))}</dt><dd>{e(item.get('explanation', ''))}</dd>"
                                         for item in segment["know_before"]) + "</dl>")
        elif segment.get("confidence") != "low":
            part.append("<p class=\"dim\">Nothing you need to know first.</p>")
        if segment.get("confidence") == "low":
            part.append("<p class=\"dim\">Not much could be found about this one.</p>")
        links = [s for s in segment.get("sources", []) if s.startswith("http")]
        if links:
            part.append("<details><summary>Sources</summary><ul>" + "".join(
                f"<li><a href=\"{e(s, quote=True)}\">{e(s.split('/')[2])}</a></li>" for s in links) + "</ul></details>")
        part.append("</section>")
        body.append("".join(part))
    footer = f"<p class=\"dim\">Made {datetime.date.today().isoformat()} by plex-cc-fixer. Best effort; it can be wrong."
    if changes_page:
        footer += f" <a href=\"{e(changes_page, quote=True)}\">Subtitle corrections</a>."
    body.append(footer + "</p>")
    return page(f"{heading}: before you watch", "".join(body), SCRIPT)


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
    if changes:
        with open(base + ".changes.html", "w", encoding="utf-8") as f:
            f.write(render_changes(code, changes, name + ".html"))
    # A run that makes the sheet alone keeps the link to the corrections page an earlier subtitle run wrote.
    linked = name + ".changes.html" if os.path.exists(base + ".changes.html") else None
    with open(base + ".html", "w", encoding="utf-8") as f:
        f.write(render_brief(code, facts, linked))
    return base + ".html"
