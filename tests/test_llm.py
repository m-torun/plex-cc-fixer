import datetime
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from plex_cc_fixer import llm


class Reset(unittest.TestCase):
    now = datetime.datetime(2026, 11, 2, 18, 40, 0)

    def wait(self, message, now=None):
        return llm.seconds_until_reset(message, now or self.now)

    def test_a_reset_later_today(self):
        self.assertEqual(self.wait("You've hit your 5-hour limit · resets 8:20pm (UTC)"), 100 * 60 + llm.LIMIT_MARGIN)

    def test_a_reset_on_the_hour(self):
        self.assertEqual(self.wait("You've hit your limit · resets 7pm (UTC)"), 20 * 60 + llm.LIMIT_MARGIN)

    def test_a_reset_after_midnight_is_tomorrows(self):
        self.assertEqual(self.wait("resets 1am (UTC)"), (5 * 60 + 20 + 60) * 60 + llm.LIMIT_MARGIN)

    def test_noon_and_midnight(self):
        self.assertEqual(self.wait("resets 12pm", datetime.datetime(2026, 11, 2, 11, 0)), 3600 + llm.LIMIT_MARGIN)
        self.assertEqual(self.wait("resets 12am", datetime.datetime(2026, 11, 2, 23, 0)), 3600 + llm.LIMIT_MARGIN)

    def test_a_reset_just_passed_is_not_tomorrows(self):
        self.assertEqual(self.wait("resets 6:39pm (UTC)"), llm.LIMIT_MARGIN)

    def test_a_message_with_a_date_still_finds_the_time(self):
        self.assertEqual(self.wait("resets Nov 3, 7pm (UTC)"), 20 * 60 + llm.LIMIT_MARGIN)

    def test_no_time_given(self):
        self.assertEqual(self.wait("You've hit your usage limit"), llm.LIMIT_UNKNOWN)


def reply(**fields):
    return subprocess.CompletedProcess([], 0, json.dumps(fields), "")


class Ask(unittest.TestCase):
    def setUp(self):
        self.lines, self.slept = [], []
        with mock.patch.object(llm, "find_cli", return_value="claude"):
            self.claude = llm.Claude(tempfile.mkdtemp(), log=self.lines.append)
        self.claude.sleep = self.slept.append

    def ask(self, *replies):
        with mock.patch.object(llm.subprocess, "run", side_effect=list(replies)) as run:
            answer = self.claude.ask("question", {"type": "object"})
        return answer, run.call_count

    def test_it_waits_out_the_limit_and_carries_on(self):
        answer, calls = self.ask(reply(is_error=True, result="You've hit your 5-hour limit · resets 8:20pm (UTC)"),
                                 reply(structured_output={"ok": 1}))
        self.assertEqual((answer, calls), ({"ok": 1}, 2))
        self.assertEqual(len(self.slept), 1)
        self.assertEqual(self.lines, ["[limit] Waiting for Claude's usage limit, which resets 8:20pm (UTC)",
                                      "[limit] Working again after Claude's usage limit"])

    def test_it_waits_again_if_the_limit_is_still_on(self):
        limited = reply(is_error=True, result="You've hit your usage limit")
        answer, calls = self.ask(limited, limited, reply(structured_output={"ok": 1}))
        self.assertEqual((answer, calls), ({"ok": 1}, 3))
        self.assertEqual(self.slept, [llm.LIMIT_UNKNOWN, llm.LIMIT_UNKNOWN])

    def test_it_gives_up_after_a_day(self):
        limited = reply(is_error=True, result="You've hit your usage limit")
        tries = llm.LIMIT_WAIT_MAX // llm.LIMIT_UNKNOWN + 1
        with self.assertRaises(llm.LimitReached):
            self.ask(*[limited] * tries)
        self.assertEqual(sum(self.slept), llm.LIMIT_WAIT_MAX)

    def test_any_other_error_is_not_waited_for(self):
        with self.assertRaisesRegex(llm.LLMError, "Failed to authenticate"):
            self.ask(reply(is_error=True, result="Failed to authenticate: OAuth session expired"))
        self.assertEqual(self.slept, [])

    def test_an_answer_is_kept_and_not_asked_for_again(self):
        self.ask(reply(structured_output={"ok": 1}))
        answer, calls = self.ask()
        self.assertEqual((answer, calls), ({"ok": 1}, 0))
        self.assertEqual(len(os.listdir(self.claude.cache_dir)), 1)


if __name__ == "__main__":
    unittest.main()
