import json
import os
import stat
import tempfile
import time
import unittest

from plex_cc_fixer import plex, server


class FakePlex(plex.PlexClient):
    """A Plex that keeps subtitles in a list. With same_id, an upload takes the place of one with its title."""

    def __init__(self, streams=(), same_id=False, lose_uploads=False):
        super().__init__("http://plex", "token")
        self.streams = [dict(s) for s in streams]
        self.same_id, self.lose_uploads, self.calls, self.selected_to = same_id, lose_uploads, [], None

    def uploaded_subtitles(self, rating_key):
        return [{"id": s["id"], "key": str(s["id"]), "title": s["title"], "selected": s.get("selected", False)}
                for s in self.streams]

    def fetch(self, path, timeout=60):
        return next(s["text"] for s in self.streams if str(s["id"]) == path)

    def select_subtitle(self, rating_key, stream_id):
        self.selected_to = stream_id

    def _send(self, path, params=None, data=None, method="POST", headers=None, timeout=120):
        self.calls.append((method, path))
        if method == "DELETE":
            self.streams = [s for s in self.streams if f"/library/streams/{s['id']}" != path]
        elif not self.lose_uploads:
            title = params["title"][:-len(".srt")]
            old = next((s for s in self.streams if s["title"] == title), None)
            if self.same_id and old:
                old["text"] = data
            else:
                self.streams.append({"id": 100 + len(self.calls), "title": title, "text": data})
        return 200


class AttachSrt(unittest.TestCase):
    def attach(self, client, text=b"new text\n"):
        with tempfile.NamedTemporaryFile(suffix=".srt") as f:
            f.write(text)
            f.flush()
            return client.attach_srt("/a.ts", f.name, "E1.en.srt", log=lambda line: None, key="7")

    def test_the_same_text_is_left_alone(self):
        client = FakePlex([{"id": 1, "title": "E1.en", "text": b"new text"}])
        self.attach(client)
        self.assertEqual(client.calls, [])

    def test_new_text_replaces_the_old_and_stays_selected(self):
        client = FakePlex([{"id": 1, "title": "E1.en", "text": b"old", "selected": True},
                           {"id": 2, "title": "E1.v2.en", "text": b"other"}])
        self.attach(client)
        self.assertEqual({s["title"]: s["text"] for s in client.streams}, {"E1.en": b"new text\n", "E1.v2.en": b"other"})
        self.assertEqual(client.selected_to, client.streams[-1]["id"])

    def test_an_upload_that_takes_the_old_ones_place_removes_nothing(self):
        client = FakePlex([{"id": 1, "title": "E1.en", "text": b"old"}], same_id=True)
        self.attach(client)
        self.assertEqual([s["id"] for s in client.streams], [1])
        self.assertFalse([call for call in client.calls if call[0] == "DELETE"])

    def test_an_upload_that_does_not_show_up_fails_and_keeps_the_old_one(self):
        client = FakePlex([{"id": 1, "title": "E1.en", "text": b"old"}], lose_uploads=True)
        with self.assertRaises(SystemExit):
            self.attach(client)
        self.assertEqual([s["id"] for s in client.streams], [1])

    def test_a_first_upload_needs_nothing_else(self):
        client = FakePlex()
        self.attach(client)
        self.assertEqual(len(client.streams), 1)
        self.assertIsNone(client.selected_to)


class Notify(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.told = os.path.join(self.folder, "told")
        self.program = os.path.join(self.folder, "notify.sh")
        with open(self.program, "w") as f:
            f.write(f'#!/bin/sh\necho "$JOB_STATE|$JOB_LABEL|$JOB_MAKE|$JOB_ERROR|$JOB_SHEET" >> "{self.told}"\n')
        os.chmod(self.program, os.stat(self.program).st_mode | stat.S_IEXEC)

    def job(self, state, **more):
        return dict({"id": 1, "state": state, "label": "Show · S1E1", "make": "all", "error": "", "file": "/tv/Show/Season 1/a.ts"}, **more)

    def lines(self, expected):
        for _ in range(100):
            if os.path.exists(self.told):
                with open(self.told) as f:
                    found = f.read().splitlines()
                if len(found) >= expected:
                    return found
            time.sleep(0.05)
        return []

    def test_a_finished_and_a_failed_job_are_told(self):
        jobs = server.Jobs(self.folder, self.folder, notify=self.program)
        jobs.tell(self.job("done"))
        jobs.tell(self.job("failed", error="no login"))
        self.assertCountEqual(self.lines(2), ["done|Show · S1E1|all||", "failed|Show · S1E1|all|no login|"])

    def test_a_cancelled_job_is_not(self):
        jobs = server.Jobs(self.folder, self.folder, notify=self.program)
        jobs.tell(self.job("cancelled"))
        time.sleep(0.2)
        self.assertFalse(os.path.exists(self.told))

    def test_a_job_cut_off_by_a_restart_is_told(self):
        os.makedirs(os.path.join(self.folder, "jobs"))
        with open(os.path.join(self.folder, "jobs", "history.json"), "w") as f:
            json.dump([self.job("running")], f)
        server.Jobs(self.folder, self.folder, notify=self.program)
        self.assertEqual(self.lines(1), ["failed|Show · S1E1|all|The service was restarted while this was running.|"])

    def test_a_program_that_fails_does_no_harm(self):
        jobs = server.Jobs(self.folder, self.folder, notify="/does/not/exist")
        jobs.tell(self.job("done"))
        time.sleep(0.1)


if __name__ == "__main__":
    unittest.main()
