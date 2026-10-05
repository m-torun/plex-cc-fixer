"""Where cheat sheet pages live and how they look. Standard library only, so the web server can import it.

The site folder mirrors the media library: the page for
    .../Show (Year)/Season 52/Show (Year) - S52E02 - Title.ts
is
    <site>/Show (Year)/Season 52/Show (Year) - S52E02 - Title.html
so the page for whatever is playing can be found from its file path alone.
"""
import html
import json
import os
import urllib.parse

STYLE = """
:root { color-scheme: light dark; --bg: #fbfaf7; --fg: #1d1c1a; --dim: #6b6862; --line: #e2dfd8; --card: #ffffff; --accent: #9a3412; --on-accent: #ffffff; }
@media (prefers-color-scheme: dark) { :root { --bg: #141312; --fg: #ece9e3; --dim: #a09c94; --line: #2e2c29; --card: #1d1c1a; --accent: #fdba74; --on-accent: #1d1c1a; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); font: 17px/1.5 -apple-system, system-ui, sans-serif; -webkit-text-size-adjust: 100%; }
main { max-width: 42rem; margin: 0 auto; padding: 1.25rem 1rem 4rem; }
h1 { font-size: 1.45rem; line-height: 1.25; margin: 0 0 .25rem; }
h2 { font-size: 1.15rem; line-height: 1.3; margin: 0; }
h3 { font-size: .8rem; text-transform: uppercase; letter-spacing: .05em; color: var(--dim); margin: 1.6rem 0 .2rem; font-weight: 600; }
p { margin: .5rem 0; }
a { color: var(--accent); }
.dim { color: var(--dim); font-size: .9rem; }
nav { margin: 1rem 0 .5rem; border-top: 1px solid var(--line); }
nav a { display: flex; gap: .6rem; padding: .5rem 0; border-bottom: 1px solid var(--line); color: var(--fg); text-decoration: none; }
nav .when { flex: 0 0 4.1rem; }
.seg { scroll-margin-top: .5rem; background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: .9rem 1rem; margin: 1rem 0; }
.when { color: var(--accent); font-variant-numeric: tabular-nums; font-weight: 600; font-size: .95rem; }
.kind { color: var(--dim); font-size: .8rem; text-transform: uppercase; letter-spacing: .05em; }
.button { display: block; margin-top: .8rem; padding: .7rem 1rem; border-radius: 10px; background: var(--accent); color: var(--on-accent); text-align: center; text-decoration: none; font-weight: 600; }
dl { margin: .6rem 0 0; }
dt { font-weight: 600; margin-top: .6rem; }
dd { margin: .1rem 0 0; }
ul { padding-left: 1.2rem; margin: .4rem 0; }
details { margin-top: .6rem; }
summary { color: var(--dim); font-size: .9rem; cursor: pointer; }
table { border-collapse: collapse; width: 100%; font-size: .95rem; }
td { border-top: 1px solid var(--line); padding: .45rem .3rem; vertical-align: top; }
td.t { white-space: nowrap; color: var(--accent); font-variant-numeric: tabular-nums; }
del { color: var(--dim); }
"""


def page(title, body, script=""):
    return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{html.escape(title)}</title><style>{STYLE}</style></head>"
            f"<body><main>{body}</main>{script}</body></html>\n")


def clock(value):
    """Show 0:12:40 as 12:40 and 1:02:03 unchanged."""
    total = int(value)
    hours, minutes, secs = total // 3600, total % 3600 // 60, total % 60
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def relative_page(media_path):
    """The page for a media file, relative to the site folder and without an extension:
    the file's two enclosing folders and its name."""
    folder, name = os.path.split(os.path.normpath(media_path))
    parents = [part for part in folder.split(os.sep) if part][-2:]
    return os.path.join(*(parents + [os.path.splitext(name)[0]]))


def url(relative):
    return "/" + urllib.parse.quote(relative.replace(os.sep, "/"), safe="/")


def sheet_for(site, media_path):
    """URL of the cheat sheet for a media file, or None when there is none."""
    relative = relative_page(media_path) + ".html"
    return url(relative) if os.path.isfile(os.path.join(site, relative)) else None


def list_sheets(site):
    """Every cheat sheet in the site folder, newest first."""
    found = []
    for folder, _, files in os.walk(site):
        for name in files:
            if not name.endswith(".json") or not os.path.isfile(os.path.join(folder, name[:-5] + ".html")):
                continue
            path = os.path.join(folder, name)
            try:
                with open(path, encoding="utf-8") as f:
                    facts = json.load(f)
            except (OSError, ValueError):
                continue
            episode = facts.get("episode", {})
            found.append({
                "label": " · ".join(x for x in (episode.get("show"), facts.get("code")) if x) or name[:-5],
                "people": " / ".join(x for x in (episode.get("host"), episode.get("musical_guest")) if x),
                "url": url(os.path.relpath(path, site)[:-5] + ".html"),
                "modified": os.path.getmtime(path),
            })
    return sorted(found, key=lambda item: -item["modified"])
