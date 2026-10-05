"""Web server for the cheat sheets: shows what Plex is playing now and offers that recording's sheet.

  python3 -m plex_cc_fixer.server --site DIR [--port 8766]

Standard library only. It has no login: keep the port on your local network.
"""
import argparse
import functools
import json
import threading
import time
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from . import plex, sheets

HOME_BODY = """
<h1>Before you watch</h1>
<p class="dim">Spoiler-free background for what you are about to watch.</p>
<h3>Now playing in Plex</h3>
<div id="now"><p class="dim">Checking…</p></div>
<h3>All cheat sheets</h3>
<div id="all"><p class="dim">Loading…</p></div>
"""

HOME_SCRIPT = """<script>
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const clock = (s) => { s = Math.floor(s); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), r = s % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(r).padStart(2, "0"); };

async function now() {
  const box = document.getElementById("now");
  try {
    const data = await (await fetch("/api/now", {cache: "no-store"})).json();
    if (data.error) { box.innerHTML = '<p class="dim">Plex cannot be reached right now.</p>'; return; }
    if (!data.playing.length) { box.innerHTML = '<p class="dim">Nothing is playing.</p>'; return; }
    box.innerHTML = data.playing.map((p) => {
      const where = [p.player, p.state, p.duration ? clock(p.position) + " of " + clock(p.duration) : ""].filter(Boolean).join(" · ");
      const sheet = p.sheet
        ? '<a class="button" href="' + esc(p.sheet) + '">Open the cheat sheet</a>'
        : '<p class="dim">No cheat sheet has been made for this one.</p>';
      return '<section class="seg"><h2>' + esc(p.label) + '</h2><p class="dim">' + esc(where) + '</p>' + sheet + '</section>';
    }).join("");
  } catch (error) {
    box.innerHTML = '<p class="dim">This page lost its connection to the server.</p>';
  }
}

async function all() {
  const box = document.getElementById("all");
  try {
    const list = await (await fetch("/api/sheets", {cache: "no-store"})).json();
    box.innerHTML = list.length ? "<nav>" + list.map((s) =>
      '<a href="' + esc(s.url) + '"><span>' + esc(s.label) + (s.people ? '<br><span class="dim">' + esc(s.people) + '</span>' : '') + '</span></a>').join("") + "</nav>"
      : '<p class="dim">None yet.</p>';
  } catch (error) {}
}

now(); all();
setInterval(now, 5000);
setInterval(all, 60000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) { now(); all(); } });
</script>"""


def describe(session):
    """A one-line name for what a session is playing."""
    if session.get("show"):
        number = ""
        if session.get("season") and session.get("episode"):
            number = "S%02dE%02d" % (int(session["season"]), int(session["episode"]))
        return " · ".join(x for x in (session["show"], number, session.get("title")) if x)
    return " ".join(x for x in (session.get("title"), "(%s)" % session["year"] if session.get("year") else "") if x)


class NowPlaying:
    """What Plex is playing, asked at most every couple of seconds however many pages are open."""

    def __init__(self, client, site, max_age=2.0):
        self.client, self.site, self.max_age = client, site, max_age
        self.lock = threading.Lock()
        self.at, self.value = 0.0, {"playing": [], "error": None}

    def snapshot(self):
        with self.lock:
            if time.time() - self.at < self.max_age:
                return self.value
            try:
                playing = []
                for session in self.client.sessions():
                    playing.append({
                        "label": describe(session),
                        "player": session.get("player") or session.get("product") or "",
                        "state": session.get("state") or "",
                        "position": (session.get("offset_ms") or 0) / 1000.0,
                        "duration": (session.get("duration_ms") or 0) / 1000.0,
                        "sheet": sheets.sheet_for(self.site, session["file"]) if session.get("file") else None,
                    })
                self.value = {"playing": playing, "error": None}
            except Exception as error:  # Plex down, token wrong, odd reply: the page says so and keeps polling
                self.value = {"playing": [], "error": str(error)[:200]}
            self.at = time.time()
            return self.value


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/", "/index.html"):
            return self.reply(sheets.page("Before you watch", HOME_BODY, HOME_SCRIPT).encode(), "text/html; charset=utf-8")
        if path == "/api/now":
            return self.reply(json.dumps(self.server.now.snapshot()).encode(), "application/json")
        if path == "/api/sheets":
            return self.reply(json.dumps(sheets.list_sheets(self.server.site)).encode(), "application/json")
        return super().do_GET()

    def reply(self, body, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Pages poll every few seconds; only problems are worth a log line.
        if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            super().log_message(format, *args)


def serve(site, port=8766, bind="0.0.0.0", plex_url="http://localhost:32400", token_file=None):
    client = plex.PlexClient(plex_url, plex.load_token(token_file))
    server = ThreadingHTTPServer((bind, port), functools.partial(Handler, directory=site))
    server.site = site
    server.now = NowPlaying(client, site)
    print(f"serving {site} on http://{bind}:{port}/", flush=True)
    server.serve_forever()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--site", required=True, help="folder holding the cheat sheet pages")
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--plex-url", default="http://localhost:32400")
    p.add_argument("--plex-token-file", default=None, help="file containing the Plex token (or set PLEX_TOKEN)")
    a = p.parse_args(argv)
    serve(a.site, a.port, a.bind, a.plex_url, a.plex_token_file)


if __name__ == "__main__":
    main()
