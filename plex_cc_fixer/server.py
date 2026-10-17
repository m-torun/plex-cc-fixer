"""The plex-cc-fixer daemon: an HTTP API and the cheat sheet pages, with no interface of its own.

  python3 -m plex_cc_fixer.server --site DIR --work DIR [--port 8766]

  GET  /api/now           what Plex is playing, and each item's cheat sheet if it has one
  GET  /api/recordings    the newest recordings, with show, subtitles yes/no, cheat sheet, generation job
  POST /api/jobs          {"key": "<ratingKey>"} start subtitle and cheat sheet generation;
                          with "make": "sheet", the cheat sheet alone
  POST /api/jobs/cancel   {"id": <job id>}
  POST /api/seek          {"key": "<ratingKey>", "client": "<player's Plex client id>", "position": <seconds>}
                          move the playback of that recording on that player, and play on if it was paused
  POST /api/playback      {"key": "<ratingKey>", "client": "<player's Plex client id>", "action": "pause" | "play"}
  GET  /<path>            the cheat sheet pages, as files from the site folder

With --auto SHOW, each new recording of that show gets the full job by itself once Plex has finished
with it (recordings added before the option was first used are left alone).

An interface (plex-buddy) talks to this. It listens on 127.0.0.1 by default and has no
login. Standard library only; generation runs the pipeline as a separate process.
"""
import argparse
import functools
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from . import plex, sheets

KEPT_JOBS = 30
LISTED = 120          # recordings in /api/recordings, newest first
SUBTITLE_CHECK = 300  # seconds a recording's "has a subtitle" answer is reused
# What a job makes. For each: the pipeline's log lines, in order, with what is happening once
# each has been seen and how far along the run then is (None: transcription reports its own).
STEPS = {
    "all": (  # the subtitle, attached in Plex, and the cheat sheet
        ("[asr]", "Transcribing speech", None),
        ("[align]", "Merging the broadcast captions", None),
        ("[captions]", "Researching the episode on the web", 0.62),
        ("[research]", "Correcting words and timing punchlines", 0.85),
        ("[polish]", "Writing the cheat sheet", 0.97),
        ("[srt]", "Attaching the subtitle in Plex", None),
        ("[plex]", "Finishing", None),
    ),
    "sheet": (  # the cheat sheet alone, from the subtitle Plex already has; transcribed only when it has none
        ("[subtitle]", "Researching the episode on the web", 0.1),
        ("[asr]", "Transcribing speech", None),
        ("[align]", "Merging the broadcast captions", None),
        ("[captions]", "Researching the episode on the web", 0.82),
        ("[research]", "Writing the cheat sheet", 0.97),
        ("[site]", "Finishing", None),
    ),
}
TRANSCRIBED = {"all": 0.6, "sheet": 0.8}  # how far along a run is when transcription ends
AUTO_CHECK = 300   # seconds between looks for new recordings of the --auto shows
AUTO_SETTLE = 900  # seconds after a recording is added before its job starts; Plex's commercial detection goes first


def episode_name(item):
    """An episode's own name, without its show: "S52E03 · Shane Gillis; Rosalía"."""
    number = ""
    if item.get("season") and item.get("episode"):
        number = "S%02dE%02d" % (int(item["season"]), int(item["episode"]))
    title = item.get("title") if item.get("title") != item.get("show") else ""  # news shows repeat the show's name
    return " · ".join(x for x in (number, title) if x)


def describe(item):
    """A one-line name for a session or a recording."""
    if item.get("show"):
        return " · ".join(x for x in (item["show"], episode_name(item)) if x)
    return " ".join(x for x in (item.get("title"), "(%s)" % item["year"] if item.get("year") else "") if x)


class Cached:
    """Calls `make` at most every `max_age` seconds, however many requests ask."""

    def __init__(self, make, max_age):
        self.make, self.max_age = make, max_age
        self.lock, self.at, self.value = threading.Lock(), 0.0, None

    def get(self):
        with self.lock:
            if self.value is None or time.time() - self.at >= self.max_age:
                self.value, self.at = self.make(), time.time()
            return self.value

    def expire(self):
        with self.lock:
            self.at = 0.0


class Jobs:
    """Runs the pipeline for one recording at a time and remembers how each run went."""

    def __init__(self, work, site, threads=3, finished=lambda job: None, plex_args=()):
        self.work, self.site, self.threads, self.finished = work, site, threads, finished
        self.plex_args = list(plex_args)
        self.folder = os.path.join(work, "jobs")
        os.makedirs(self.folder, exist_ok=True)
        self.history = os.path.join(self.folder, "history.json")
        self.lock = threading.Lock()
        self.wake = threading.Condition(self.lock)
        self.process = None
        try:
            with open(self.history, encoding="utf-8") as f:
                self.jobs = json.load(f)
        except (OSError, ValueError):
            self.jobs = []
        for job in self.jobs:  # whatever was under way when the daemon last stopped did not finish
            if job["state"] in ("queued", "running"):
                job.update(state="failed", error="The service was restarted while this was running.", finished=time.time())
        threading.Thread(target=self.worker, daemon=True).start()

    def save(self):
        self.jobs = self.jobs[-KEPT_JOBS:]
        with open(self.history + ".tmp", "w", encoding="utf-8") as f:
            json.dump(self.jobs, f, indent=1)
        os.replace(self.history + ".tmp", self.history)

    def start(self, recording, make="all"):
        with self.lock:
            for job in self.jobs:
                if job["file"] == recording["file"] and job["state"] in ("queued", "running"):
                    return job
            job = {
                "id": int(time.time() * 1000), "key": recording["key"], "file": recording["file"], "make": make,
                "label": describe(recording), "show": recording.get("show") or "",
                "title": recording.get("title") or "", "duration": recording["duration_ms"] / 1000.0,
                "state": "queued", "step": "Waiting for another job to finish", "progress": None,
                "queued": time.time(), "started": None, "finished": None, "error": "",
            }
            self.jobs.append(job)
            self.save()
            self.wake.notify()
            return job

    def cancel(self, job_id):
        with self.lock:
            for job in self.jobs:
                if job["id"] == job_id and job["state"] in ("queued", "running"):
                    if job["state"] == "running" and self.process:
                        os.killpg(self.process.pid, signal.SIGTERM)
                    job.update(state="cancelled", finished=time.time(), step="", progress=None)
                    self.save()
                    return job
        raise KeyError("no such job under way")

    def snapshot(self):
        with self.lock:
            return [dict(job) for job in self.jobs]

    def latest_for(self, path):
        """The most recent job for a recording, or None."""
        with self.lock:
            for job in reversed(self.jobs):
                if job["file"] == path:
                    return dict(job)
        return None

    def command(self, job):
        show_folder = os.path.basename(os.path.dirname(os.path.dirname(job["file"]))) or "recordings"
        prompt = ", ".join(x.strip() for x in [job["show"]] + job["title"].split(";") if x.strip())
        command = "brief" if job.get("make") == "sheet" else "episode"
        return (["nice", "-n", "10", sys.executable, "-u", "-m", "plex_cc_fixer", command, job["file"],
                 "--work", os.path.join(self.work, show_folder), "--prompt", prompt,
                 "--threads", str(self.threads), "--site", self.site, "--rating-key", str(job["key"])]
                + self.plex_args + (["--polish"] if command == "episode" else []))

    def worker(self):
        while True:
            with self.lock:
                job = next((j for j in self.jobs if j["state"] == "queued"), None)
                while job is None:
                    self.wake.wait()
                    job = next((j for j in self.jobs if j["state"] == "queued"), None)
                job.update(state="running", started=time.time(), step="Extracting audio and captions", progress=0.0)
                self.save()
            self.run(job)

    def run(self, job):
        log_path = os.path.join(self.folder, "%d.log" % job["id"])
        env = dict(os.environ, PATH=os.path.expanduser("~/.local/bin") + os.pathsep + os.environ.get("PATH", ""))
        last = ""
        try:
            with open(log_path, "w", encoding="utf-8") as log:
                process = subprocess.Popen(
                    self.command(job), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
                    cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    start_new_session=True)
                with self.lock:
                    self.process = process
                for line in process.stdout:
                    log.write(line)
                    log.flush()
                    if line.strip():
                        last = line.strip()
                    self.follow(job, line)
                code = process.wait()
        except OSError as error:
            code, last = 1, str(error)
        with self.lock:
            self.process = None
            if job["state"] == "running":
                if code == 0:
                    job.update(state="done", step="", progress=1.0)
                else:
                    job.update(state="failed", step="", progress=None, error=last[-300:] or "exit code %d" % code)
                job["finished"] = time.time()
            self.save()
        self.finished(job)

    def follow(self, job, line):
        """Move the job's step and progress along from one line of the pipeline's log."""
        with self.lock:
            if job["state"] != "running":
                return
            make = job.get("make", "all")
            for marker, step, progress in STEPS[make]:
                if line.startswith(marker):
                    job["step"] = step
                    if progress is not None:
                        job["progress"] = progress
            if line.startswith("[asr]") and line.rstrip().endswith("s") and job["duration"]:
                try:  # "[asr]   1234.5s": how far into the recording transcription has come
                    share = TRANSCRIBED[make]
                    job["progress"] = min(share, share * float(line[5:].strip()[:-1]) / job["duration"])
                except ValueError:
                    pass


class Daemon:
    def __init__(self, client, site, work, threads, plex_args=()):
        self.client, self.site = client, site
        self.subtitles = {}  # ratingKey -> (has a subtitle, when asked)
        self.jobs = Jobs(work, site, threads, self.job_finished, plex_args)
        self.now = Cached(self.read_now, 2.0)
        self.recent = Cached(self.read_recent, 20.0)

    def job_finished(self, job):
        self.subtitles.pop(job["key"], None)
        self.recent.expire()

    def read_now(self):
        try:
            playing = []
            for session in self.client.sessions():
                playing.append({
                    "key": session.get("key"), "client": session.get("client"), "label": describe(session),
                    "player": session.get("player") or session.get("product") or "",
                    "state": session.get("state") or "",
                    "position": (session.get("offset_ms") or 0) / 1000.0,
                    "duration": (session.get("duration_ms") or 0) / 1000.0,
                    "sheet": sheets.sheet_for(self.site, session["file"]) if session.get("file") else None,
                })
            return {"playing": playing, "error": None}
        except Exception as error:  # Plex down, token wrong, odd reply: say so and keep answering
            return {"playing": [], "error": str(error)[:200]}

    def read_recent(self):
        try:
            found = self.client.recent(LISTED)
            for recording in found:
                known = self.subtitles.get(recording["key"])
                if known is None or time.time() - known[1] > SUBTITLE_CHECK:
                    known = self.subtitles[recording["key"]] = (self.client.has_subtitle(recording["key"]), time.time())
                recording["subtitles"] = known[0]
            return found, None
        except Exception as error:
            return [], str(error)[:200]

    def recordings(self):
        found, error = self.recent.get()
        rows = []
        for recording in found:
            rows.append({
                "key": recording["key"], "label": describe(recording), "show": recording.get("show") or "",
                "episode": episode_name(recording) or recording.get("title") or "", "added": recording["added"],
                "duration": recording["duration_ms"] / 1000.0, "subtitles": recording["subtitles"],
                "sheet": sheets.sheet_for(self.site, recording["file"]),
                "job": self.jobs.latest_for(recording["file"]),
            })
        return {"recordings": rows, "error": error}

    def playing_on(self, key, client_id):
        """The session of this recording on this player, and the player, for sending it a command."""
        found = self.read_now()  # asked now, not the copy that may be two seconds old
        session = next((p for p in found["playing"] if p["key"] == key and p["client"] == client_id), None)
        if session is None:
            raise KeyError("that recording is not playing on that player right now")
        player = self.client.player(client_id)
        if player is None:
            raise plex.PlayerError(session["player"] + " does not take remote-control commands through Plex")
        return session, player

    def seek(self, key, client_id, position):
        """Move the playback of this recording on this player to `position` seconds."""
        session, player = self.playing_on(key, client_id)
        if session["duration"]:
            position = min(position, session["duration"] - 5)
        position = max(0.0, position)
        player.seek(position)
        self.now.expire()
        return {"player": session["player"], "position": position}

    def playback(self, key, client_id, action):
        if action not in ("pause", "play"):
            raise ValueError('"action" is "pause" or "play"')
        session, player = self.playing_on(key, client_id)
        player.pause(resume=action == "play")
        self.now.expire()
        return {"player": session["player"], "action": action}

    def start_job(self, key, make="all"):
        if make not in STEPS:
            raise ValueError('"make" is "all" or "sheet"')
        found, error = self.recent.get()
        for recording in found:
            if recording["key"] == str(key):
                job = self.jobs.start(recording, make)
                self.recent.expire()
                return job
        raise KeyError(error or "that recording is not among the newest ones")


class Auto:
    """Starts the full job, once, for each new recording of the chosen shows."""

    def __init__(self, daemon, shows, folder):
        self.daemon, self.shows = daemon, {show.lower() for show in shows}
        self.path = os.path.join(folder, "auto.json")
        try:
            with open(self.path, encoding="utf-8") as f:
                self.state = json.load(f)
        except (OSError, ValueError):
            self.state = {"since": int(time.time()), "started": []}  # older recordings are left alone
            self.save()
        threading.Thread(target=self.watch, daemon=True).start()

    def save(self):
        self.state["started"] = self.state["started"][-200:]
        with open(self.path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=1)
        os.replace(self.path + ".tmp", self.path)

    def due(self, now=None):
        """The recordings that should get a job now."""
        now = now or time.time()
        found = []
        for recording in self.daemon.client.recent(30):
            if ((recording.get("show") or "").lower() in self.shows and recording["added"] >= self.state["since"]
                    and now - recording["added"] >= AUTO_SETTLE and "/.grab/" not in recording["file"]
                    and recording["file"] not in self.state["started"]
                    and not sheets.sheet_for(self.daemon.site, recording["file"])
                    and not self.daemon.jobs.latest_for(recording["file"])):
                found.append(recording)
        return found

    def watch(self):
        while True:
            try:
                due = self.due()
                # Not while Plex is still marking the commercials of a recording.
                if due and subprocess.run(["pgrep", "-f", "Plex Commercial Skipper"], stdout=subprocess.DEVNULL).returncode:
                    for recording in due:
                        job = self.daemon.jobs.start(recording)
                        self.state["started"].append(recording["file"])
                        self.save()
                        print(f"[auto] job {job['id']} for {describe(recording)}", flush=True)
                    self.daemon.recent.expire()
            except Exception as error:  # Plex down or an odd reply: try again next time
                print(f"[auto] {error}", flush=True)
            time.sleep(AUTO_CHECK)


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        daemon = self.server.app
        if path == "/api/now":
            return self.reply(200, daemon.now.get())
        if path == "/api/recordings":
            return self.reply(200, daemon.recordings())
        if path == "/api/jobs":
            return self.reply(200, daemon.jobs.snapshot())
        if path in ("/", "/index.html"):
            return self.reply(200, {"service": "plex-cc-fixer", "interface": "plex-buddy"})
        if path.startswith("/api/"):
            return self.reply(404, {"error": "not found"})
        return super().do_GET()

    def do_POST(self):
        daemon = self.server.app
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 4096:
                raise ValueError("request too large")
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/api/jobs":
                return self.reply(200, daemon.start_job(body["key"], body.get("make", "all")))
            if self.path == "/api/seek":
                return self.reply(200, daemon.seek(str(body["key"]), str(body["client"]), float(body["position"])))
            if self.path == "/api/playback":
                return self.reply(200, daemon.playback(str(body["key"]), str(body["client"]), str(body["action"])))
            if self.path == "/api/jobs/cancel":
                job = daemon.jobs.cancel(int(body["id"]))
                daemon.recent.expire()
                return self.reply(200, job)
            return self.reply(404, {"error": "not found"})
        except KeyError as error:
            return self.reply(404, {"error": str(error.args[0]) if error.args else "not found"})
        except (ValueError, TypeError) as error:
            return self.reply(400, {"error": str(error)})
        except plex.PlayerError as error:
            return self.reply(502, {"error": str(error)})

    def reply(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Interfaces poll every few seconds; only problems are worth a log line.
        if len(args) > 1 and str(args[1]).startswith(("4", "5")):
            super().log_message(format, *args)


def serve(site, work, port=8766, listen="127.0.0.1", plex_url="http://localhost:32400", token_file=None, threads=3,
          auto=()):
    client = plex.PlexClient(plex_url, plex.load_token(token_file))
    server = ThreadingHTTPServer((listen, port), functools.partial(Handler, directory=site))
    plex_args = ["--plex-url", plex_url] + (["--plex-token-file", token_file] if token_file else [])
    server.app = Daemon(client, site, work, threads, plex_args)
    if auto:
        Auto(server.app, auto, server.app.jobs.folder)
    print(f"plex-cc-fixer API on http://{listen}:{port}/ (pages from {site}, work in {work})", flush=True)
    server.serve_forever()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--site", required=True, help="folder holding the cheat sheet pages")
    p.add_argument("--work", required=True, help="folder for episode work files; one subfolder per show")
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--listen", default="127.0.0.1", help="address to listen on")
    p.add_argument("--plex-url", default="http://localhost:32400")
    p.add_argument("--plex-token-file", default=None, help="file containing the Plex token (or set PLEX_TOKEN)")
    p.add_argument("--threads", type=int, default=3, help="CPU threads Whisper may use in a generation job")
    p.add_argument("--auto", action="append", default=[], metavar="SHOW",
                   help="make the subtitle and cheat sheet of each new recording of this show by itself; repeatable")
    a = p.parse_args(argv)
    sys.stderr.reconfigure(line_buffering=True)  # Python 3.8 holds back request log lines under systemd otherwise
    serve(a.site, a.work, a.port, a.listen, a.plex_url, a.plex_token_file, a.threads, a.auto)


if __name__ == "__main__":
    main()
