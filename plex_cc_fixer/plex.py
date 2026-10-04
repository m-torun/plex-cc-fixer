import os
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

    def _get_xml(self, path, params=None):
        with urllib.request.urlopen(self._url(path, params), timeout=60) as r:
            return ET.fromstring(r.read())

    def find_rating_key(self, media_path, title):
        terms = [title] + [p.strip() for p in title.split(";")]
        for term in terms:
            for video in self._get_xml("/search", {"query": term}).iter("Video"):
                for part in video.iter("Part"):
                    if part.get("file") == media_path:
                        return video.get("ratingKey")
        return None

    def subtitle_titles(self, rating_key):
        root = self._get_xml(f"/library/metadata/{rating_key}")
        return {s.get("title") for s in root.iter("Stream") if s.get("streamType") == "3"}

    def attach_srt(self, media_path, srt_path, stream_title, log=print):
        """Attach an SRT to the Plex item whose media file is media_path. Skips if already attached."""
        episode_title = os.path.splitext(os.path.basename(media_path))[0].split(" - ")[-1]
        key = self.find_rating_key(media_path, episode_title)
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
