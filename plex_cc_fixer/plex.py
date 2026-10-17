import itertools
import json
import os
import socket
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


def load_token(token_file=None):
    token = os.environ.get("PLEX_TOKEN")
    if token:
        return token.strip()
    path = os.path.expanduser(token_file or "~/.config/plex-token")
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


class PlexClient:
    def __init__(self, url, token):
        self.url = url.rstrip("/")
        self.token = token

    def _url(self, path, params=None):
        q = dict(params or {}, **{"X-Plex-Token": self.token})
        return f"{self.url}{path}?{urllib.parse.urlencode(q)}"

    def _get_xml(self, path, params=None, timeout=60):
        with urllib.request.urlopen(self._url(path, params), timeout=timeout) as r:
            return ET.fromstring(r.read())

    def sessions(self):
        """What is playing now: one dict per session with the item's names, its media file and the player."""
        playing = []
        for item in self._get_xml("/status/sessions", timeout=5):
            player = item.find("Player")
            if player is None:
                continue
            part = item.find("Media/Part")
            path = part.get("file") if part is not None else None
            if not path and item.get("ratingKey"):
                path = self._media_file(item.get("ratingKey"))
            playing.append({
                "key": item.get("ratingKey"),
                "client": player.get("machineIdentifier"),
                "type": item.get("type"),
                "title": item.get("title"),
                "show": item.get("grandparentTitle"),
                "season": item.get("parentIndex"),
                "episode": item.get("index"),
                "year": item.get("year"),
                "file": path,
                "offset_ms": int(item.get("viewOffset") or 0),
                "duration_ms": int(item.get("duration") or 0),
                "player": player.get("title"),
                "product": player.get("product"),
                "state": player.get("state"),
            })
        return playing

    def recent(self, limit=15):
        """The newest recordings across the libraries of shows: one dict per episode, newest first."""
        found = []
        for section in self._get_xml("/library/sections", timeout=5):
            if section.get("type") != "show":
                continue
            listing = self._get_xml(f"/library/sections/{section.get('key')}/all", {
                "type": 4, "sort": "addedAt:desc", "X-Plex-Container-Start": 0, "X-Plex-Container-Size": limit}, timeout=10)
            for item in listing.iter("Video"):
                part = item.find("Media/Part")
                if part is None or not part.get("file"):
                    continue
                found.append({
                    "key": item.get("ratingKey"),
                    "title": item.get("title"),
                    "show": item.get("grandparentTitle"),
                    "season": item.get("parentIndex"),
                    "episode": item.get("index"),
                    "file": part.get("file"),
                    "added": int(item.get("addedAt") or 0),
                    "duration_ms": int(item.get("duration") or 0),
                })
        return sorted(found, key=lambda r: -r["added"])[:limit]

    def has_subtitle(self, rating_key):
        """True when the item has a subtitle that is not part of the recording (an upload or a sidecar file)."""
        root = self._get_xml(f"/library/metadata/{rating_key}", timeout=5)
        return any(s.get("streamType") == "3" and s.get("key") for s in root.iter("Stream"))

    def _media_file(self, rating_key):
        """The media file of a library item; remembered, since a session is asked about every few seconds."""
        cache = self.__dict__.setdefault("_files", {})
        if rating_key not in cache:
            part = self._get_xml(f"/library/metadata/{rating_key}", timeout=5).find(".//Part")
            cache[rating_key] = part.get("file") if part is not None else None
        return cache[rating_key]

    def find_rating_key(self, media_path, title=None):
        """The library item whose media file is media_path: by searching for its title, then by going through the libraries."""
        title = title or os.path.splitext(os.path.basename(media_path))[0].split(" - ")[-1]
        terms = [title] + [p.strip() for p in title.split(";")]
        for term in terms:
            for video in self._get_xml("/search", {"query": term}).iter("Video"):
                for part in video.iter("Part"):
                    if part.get("file") == media_path:
                        return video.get("ratingKey")
        # An episode named like its show ("CBS News Sunday Morning") is not among the search results.
        for section in self._get_xml("/library/sections", timeout=10):
            listing = self._get_xml(f"/library/sections/{section.get('key')}/all",
                                    {"type": 4} if section.get("type") == "show" else {}, timeout=60)
            for video in listing.iter("Video"):
                if any(part.get("file") == media_path for part in video.iter("Part")):
                    return video.get("ratingKey")
        return None

    def uploaded_subtitles(self, rating_key):
        """The subtitles attached to an item that are not part of the recording; the selected one first, then the newest."""
        root = self._get_xml(f"/library/metadata/{rating_key}", timeout=10)
        found = [{"id": int(s.get("id") or 0), "key": s.get("key"), "title": s.get("title") or "",
                  "selected": s.get("selected") == "1"}
                 for s in root.iter("Stream") if s.get("streamType") == "3" and s.get("key")]
        return sorted(found, key=lambda s: (not s["selected"], -s["id"]))

    def fetch(self, path, timeout=60):
        with urllib.request.urlopen(self._url(path), timeout=timeout) as r:
            return r.read()

    def player(self, client_id):
        """The player with this Plex client id, when Plex lists it as one that takes commands."""
        for entry in self._get_xml("/clients", timeout=5):
            if entry.get("machineIdentifier") == client_id and entry.get("host") and entry.get("port"):
                roku = "roku" in (entry.get("platform", "") + entry.get("product", "")).lower()
                return Player(entry.get("host"), entry.get("port"), client_id, entry.get("name") or "", roku)
        # Some apps, such as Plex for Apple TV, are missing from the server's list but tell the account where they listen.
        for device in self.account_players():
            if device.get("clientIdentifier") == client_id:
                for connection in device.get("connections") or []:
                    address = urllib.parse.urlsplit(connection.get("uri") or "")
                    if connection.get("local") and address.hostname and address.port:
                        return Player(address.hostname, address.port, client_id, device.get("name") or "")
        return None

    def account_players(self):
        """The players signed in to the Plex account, as plex.tv lists them. Remembered for a few minutes."""
        cached = self.__dict__.get("_account")
        if cached and time.time() - cached[0] < 300:
            return cached[1]
        request = urllib.request.Request("https://plex.tv/api/v2/resources?" + urllib.parse.urlencode({
            "includeHttps": 1, "X-Plex-Token": self.token, "X-Plex-Client-Identifier": Player.SENDER}),
            headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                found = [d for d in json.load(response) if "player" in (d.get("provides") or "").split(",")]
        except (OSError, ValueError):
            found = []
        self._account = (time.time(), found)
        return found

    def play_on(self, player, rating_key, seconds):
        """Open a library item on a player and move it to `seconds`. For a player that has dropped what it was
        playing: a Roku does when its screensaver comes on during a pause."""
        server = self._get_xml("/identity", timeout=5).get("machineIdentifier")
        key = f"/library/metadata/{rating_key}"
        request = urllib.request.Request(self._url("/playQueues", {
            "type": "video", "uri": f"server://{server}/com.plexapp.plugins.library{key}",
            "shuffle": 0, "repeat": 0, "continuous": 0, "own": 1}), data=b"", method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            queue = ET.fromstring(response.read()).get("playQueueID")
        token = self._get_xml("/security/token", {"type": "delegation", "scope": "all"}, timeout=5).get("token")
        # The address the player reaches this server at: the one this machine uses to reach the player.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((player.host, 9))
            address = probe.getsockname()[0]
        player.send("/player/playback/playMedia", {
            "key": key, "offset": 0, "machineIdentifier": server, "address": address,
            "port": urllib.parse.urlsplit(self.url).port or 32400, "protocol": "http", "type": "video",
            "token": token, "providerIdentifier": "com.plexapp.plugins.library",
            "containerKey": f"/playQueues/{queue}?window=100&own=1"}, timeout=15)
        player.wait_playing()
        # A Roku ignores a start offset in playMedia, so the position is set once it plays.
        player.send("/player/playback/seekTo", {"type": "video", "offset": int(round(seconds * 1000))})

    def subtitle_titles(self, rating_key):
        root = self._get_xml(f"/library/metadata/{rating_key}")
        return {s.get("title") for s in root.iter("Stream") if s.get("streamType") == "3"}

    def attach_srt(self, media_path, srt_path, stream_title, log=print, key=None):
        """Attach an SRT to the Plex item whose media file is media_path. Skips if already attached."""
        key = key or self.find_rating_key(media_path)
        if not key:
            raise SystemExit(f"no Plex item found whose media file is {media_path}")
        plex_title = stream_title[:-len(".srt")] if stream_title.endswith(".srt") else stream_title
        if plex_title in self.subtitle_titles(key):
            log(f"[plex] {plex_title} already attached to ratingKey {key}; skipping")
            return key
        url = self._url(f"/library/metadata/{key}/subtitles", {"title": stream_title, "format": "srt"})
        with open(srt_path, "rb") as f:
            req = urllib.request.Request(url, data=f.read(), method="POST",
                                         headers={"Content-Type": "text/plain"})
        with urllib.request.urlopen(req, timeout=120) as r:
            log(f"[plex] uploaded {plex_title} to ratingKey {key} (HTTP {r.status})")
        if plex_title not in self.subtitle_titles(key):
            raise SystemExit("upload accepted but the subtitle is not visible in Plex metadata")
        return key


class PlayerError(RuntimeError):
    pass


class NothingOpen(PlayerError):
    """The player answers but has no video open."""


class Player:
    """A Plex player, driven with the requests Plex's own apps use on one another."""
    SENDER = "plex-cc-fixer"
    ids = itertools.count(int(time.time()) % 1000000)

    ROKU_PLEX_APP = 13535  # the Plex app's id on a Roku

    def __init__(self, host, port, client_id, name="", roku=False):
        self.host, self.address, self.client_id = host, f"http://{host}:{port}", client_id
        self.name = name or "the player"
        self.roku = f"http://{host}:8060" if roku else None  # the Roku's own control API

    def wake(self):
        """A Roku shows its screensaver a few minutes into a pause, and its Plex app then answers nothing.
        Opening the app again takes the screensaver away, but the app has then dropped the paused video."""
        if not self.roku:
            return
        try:
            with urllib.request.urlopen(f"{self.roku}/query/active-app", timeout=3) as response:
                asleep = b"<screensaver" in response.read()
            if not asleep:
                return
            launch = urllib.request.Request(f"{self.roku}/launch/{self.ROKU_PLEX_APP}", data=b"", method="POST")
            urllib.request.urlopen(launch, timeout=5).read()
        except OSError:
            return  # then the command itself says the player does not answer
        for attempt in range(20):  # until the app answers again
            time.sleep(1)
            try:
                self.send("/player/timeline/poll", {"wait": 0}, timeout=3)
                return
            except PlayerError:
                pass

    def wait_playing(self, limit=30):
        since = time.time()
        while time.time() - since < limit:
            try:
                if self.video()[0] == "playing":
                    return
            except PlayerError:
                pass
            time.sleep(1)
        raise PlayerError(f"{self.name} did not start playing within {limit} s")

    def send(self, path, params, timeout=6):
        query = urllib.parse.urlencode(dict(params, commandID=next(Player.ids)))
        request = urllib.request.Request(f"{self.address}{path}?{query}", headers={
            "X-Plex-Client-Identifier": self.SENDER, "X-Plex-Device-Name": self.SENDER,
            "X-Plex-Target-Client-Identifier": self.client_id})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except OSError as error:
            raise PlayerError(f"{self.name} did not answer ({error})")

    def video(self):
        """State, and position in seconds, of the video the player has open. Asked twice: the first answer can be stale."""
        self.wake()
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

    def seek(self, seconds):
        """Move playback to `seconds`. The command goes only to a player that is playing or paused and
        reports a position: a seek sent to one with nothing open has crashed a player's app."""
        state, position = self.video()
        if state not in ("playing", "paused") or position is None:
            raise NothingOpen(f"{self.name} is not playing anything it can jump in")
        self.send("/player/playback/seekTo", {"type": "video", "offset": int(round(seconds * 1000))})
        if state == "paused":
            # A jump means play from there. A Roku plays on by itself after a jump; a player that stays paused is told to.
            time.sleep(3)
            if self.video()[0] == "paused":
                self.send("/player/playback/play", {"type": "video"})

    def pause(self, resume=False):
        """Pause playback, or with `resume` play on, under the same rule as `seek`."""
        state, position = self.video()
        if state not in ("playing", "paused") or position is None:
            raise NothingOpen(f"{self.name} is not playing anything it can pause")
        self.send("/player/playback/" + ("play" if resume else "pause"), {"type": "video"})
