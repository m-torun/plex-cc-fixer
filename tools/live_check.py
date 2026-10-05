#!/usr/bin/env python3
"""Check the cheat sheet server against a real player, without anyone holding the remote.

Plays a library item on a player through Plex's remote-control protocol (the one
Plex's own apps use on each other), asks the cheat sheet server what is playing
while it runs, then stops playback and puts the item's watch state back.

The player's screen shows the item, with sound, for the seconds given. Run it on
the Plex server. Python 3.6+, no packages needed.

  tools/live_check.py --player roku --rating-key 2208 --plex-address 192.168.1.2:32400
"""

import argparse
import itertools
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlencode
from urllib.request import Request, urlopen

NAME = "plex-cc-fixer-check"
LIBRARY = "com.plexapp.plugins.library"


class Plex:
    def __init__(self, url, token):
        self.url, self.token = url.rstrip("/"), token

    def get(self, path, params=None, method="GET"):
        url = self.url + path + ("?" + urlencode(params) if params else "")
        request = Request(url, method=method, headers={
            "X-Plex-Token": self.token, "X-Plex-Client-Identifier": NAME, "X-Plex-Product": NAME})
        with urlopen(request, timeout=15) as response:
            return ET.fromstring(response.read() or b"<empty/>")

    def watch_state(self, rating_key):
        item = self.get("/library/metadata/%s" % rating_key)[0]
        return int(item.get("viewOffset") or 0), int(item.get("viewCount") or 0)

    def restore(self, rating_key, before):
        """Put resume point and watched mark back. A play count above one comes back as one."""
        library = {"identifier": LIBRARY, "key": rating_key}
        if before[0]:
            self.get("/:/progress", dict(library, time=before[0], state="stopped"))
        else:
            # Setting progress to 0 leaves the new resume point in place; clearing and re-marking does not.
            self.get("/:/unscrobble", library)
            if before[1]:
                self.get("/:/scrobble", library)


class Player:
    def __init__(self, host, port, client_id):
        self.address, self.client_id = "http://%s:%s" % (host, port), client_id
        self.ids = itertools.count(int(time.time()) % 1000000)

    def send(self, path, params, timeout=8):
        query = urlencode(dict(params, commandID=next(self.ids)))
        request = Request("%s%s?%s" % (self.address, path, query), headers={
            "X-Plex-Client-Identifier": NAME, "X-Plex-Device-Name": NAME,
            "X-Plex-Target-Client-Identifier": self.client_id})
        with urlopen(request, timeout=timeout) as response:
            return response.read()

    def answers(self):
        """False when the player's app is not serving requests, as on a Roku showing its screensaver."""
        try:
            self.send("/player/timeline/poll", {"wait": 0}, timeout=4)
            return True
        except OSError:
            return False

    def video(self):
        """State and position (seconds) of the video the player has open. Asked twice: the first answer can be stale."""
        found = ("", None)
        for attempt in range(2):
            for element in ET.fromstring(self.send("/player/timeline/poll", {"wait": 0})).iter("Timeline"):
                if element.get("type") == "video":
                    position = element.get("time", "")
                    found = (element.get("state", ""), int(position) / 1000.0 if position.isdigit() else None)
            if found[0] != "stopped":
                break
            time.sleep(0.5)
        return found


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--player", required=True, help="part of the player's name as Plex lists it")
    parser.add_argument("--rating-key", required=True, help="library item to play; pick one nobody is watching")
    parser.add_argument("--plex-address", required=True, help="address:port the player should use to reach Plex")
    parser.add_argument("--plex-url", default="http://127.0.0.1:32400")
    parser.add_argument("--token-file", default=os.path.expanduser("~/.config/plex-token"))
    parser.add_argument("--server-url", default="http://127.0.0.1:8766", help="the cheat sheet server")
    parser.add_argument("--offset", type=int, default=0, help="position to start at, in seconds")
    parser.add_argument("--seconds", type=float, default=15, help="how long to leave it playing")
    parser.add_argument("--expect", choices=("sheet", "no-sheet"), help="fail unless the server offers / does not offer a sheet")
    parser.add_argument("--launch-url", help="URL to POST to open the player's Plex app when it does not answer, "
                                             "for a Roku: http://<roku>:8060/launch/13535")
    args = parser.parse_args()

    began = time.time()

    def say(text):
        print("%5.1fs  %s" % (time.time() - began, text), flush=True)

    with open(args.token_file) as f:
        plex = Plex(args.plex_url, f.read().strip())
    wanted = args.player.lower()
    listed = [e for e in plex.get("/clients") if wanted in e.get("name", "").lower()]
    if not listed:
        sys.exit("Plex lists no player matching %r; its Plex app has to be open" % args.player)
    name = listed[0].get("name")
    player = Player(listed[0].get("host"), listed[0].get("port"), listed[0].get("machineIdentifier"))
    if not player.answers():
        if not args.launch_url:
            sys.exit("%s is listed by Plex but does not answer (a Roku behind its screensaver does this); "
                     "pass --launch-url to open its Plex app" % name)
        say("%s does not answer; opening its Plex app" % name)
        urlopen(Request(args.launch_url, data=b"", method="POST"), timeout=10).read()
        waited = time.time()
        while time.time() - waited < 30 and not player.answers():
            time.sleep(1)
        if not player.answers():
            sys.exit("%s still does not answer %d s after opening its Plex app" % (name, time.time() - waited))
        say("its Plex app answers after %.0f s" % (time.time() - waited))
        time.sleep(3)
    state, _ = player.video()
    if state in ("playing", "paused", "buffering"):
        sys.exit("%s is %s something; not interrupting it" % (name, state))
    say("player: %s at %s, idle" % (name, player.address))

    key = "/library/metadata/%s" % args.rating_key
    before = plex.watch_state(args.rating_key)
    server = plex.get("/identity").get("machineIdentifier")
    seen, started, failed = [], False, False
    try:
        queue = plex.get("/playQueues", {
            "type": "video", "shuffle": 0, "repeat": 0, "continuous": 0, "own": 1,
            "uri": "server://%s/%s%s" % (server, LIBRARY, key)}, method="POST")
        token = plex.get("/security/token", {"type": "delegation", "scope": "all"}).get("token")
        host, port = args.plex_address.rsplit(":", 1)
        player.send("/player/playback/playMedia", {
            "key": key, "offset": args.offset * 1000, "machineIdentifier": server, "address": host, "port": port,
            "protocol": "http", "type": "video", "token": token, "providerIdentifier": LIBRARY,
            "containerKey": "/playQueues/%s?window=100&own=1" % queue.get("playQueueID")})
        started = True
        waited = time.time()
        while time.time() - waited < 25 and player.video()[0] != "playing":
            time.sleep(0.5)
        if player.video()[0] != "playing":
            say("FAIL  playback did not start within 25 s")
            failed = True
        else:
            say("playing on the player %.1f s after the command" % (time.time() - waited))
            end = time.time() + args.seconds
            while time.time() < end:
                with urlopen(args.server_url.rstrip("/") + "/api/now", timeout=5) as response:
                    now = json.load(response)
                mine = [p for p in now.get("playing", []) if p.get("player") == name]
                if mine:
                    seen.append(mine[0])
                    say("server: %s | %s | %s at %.0f s | sheet: %s" % (
                        mine[0]["label"], mine[0]["player"], mine[0]["state"], mine[0]["position"],
                        "yes" if mine[0]["sheet"] else "no"))
                else:
                    say("server: this player not listed yet%s" % (" (error: %s)" % now["error"] if now.get("error") else ""))
                time.sleep(3)
    finally:
        if started:
            state, position = player.video()
            # A playback command goes only to a player that is playing or paused and reports a position.
            if state in ("playing", "paused") and position is not None:
                player.send("/player/playback/stop", {"type": "video"})
                time.sleep(3)
            say("player is now %s" % (player.video()[0] or "silent"))
            plex.restore(args.rating_key, before)
            after = plex.watch_state(args.rating_key)
            say("watch state (resume ms, plays): before %s, now %s" % (before, after))

    if not failed:
        if not seen:
            say("FAIL  the server never listed the player's session")
            failed = True
        elif args.expect and (bool(seen[-1]["sheet"]) != (args.expect == "sheet")):
            say("FAIL  expected %s" % args.expect)
            failed = True
        else:
            say("ok    the server listed the session%s" % (
                " and offered " + seen[-1]["sheet"] if seen[-1]["sheet"] else " with no sheet"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
