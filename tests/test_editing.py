import os
import tempfile
import unittest

from plex_cc_fixer import llm, lyrics, polish, shows, speech


def speech_cue(*spoken):
    return speech.speech_cue([list(word) for word in spoken])


def words(text, start=0.0, step=0.5):
    return [[start + i * step, start + i * step + 0.4, token] for i, token in enumerate(text.split())]


class Claude:
    """Answers with a canned reply, whatever is asked."""

    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    def ask(self, prompt, schema, **kwargs):
        self.prompts.append(prompt)
        return self.reply


class RetimeStranded(unittest.TestCase):
    def test_first_word_of_a_sentence_moves_to_the_speech_after_the_silence(self):
        spoken = words("was his ass. President", 0) + words("Trump attacked him.", 8)
        moved = speech.retime_stranded(spoken)
        self.assertAlmostEqual(moved[3][1], moved[4][0] - speech.MIN_GAP)
        self.assertEqual(moved[:3], spoken[:3])
        self.assertEqual(moved[4:], spoken[4:])

    def test_the_cue_no_longer_holds_the_stranded_word(self):
        cues = speech.resegment(words("was his ass. President", 0) + words("Trump attacked him.", 8))
        self.assertEqual([cue["text"] for cue in cues], ["was his ass.", "President Trump attacked him."])

    def test_a_sentence_that_ended_stays(self):
        spoken = words("was his ass.", 0) + words("Trump attacked him.", 8)
        self.assertEqual(speech.retime_stranded(spoken), spoken)

    def test_a_long_run_before_a_pause_stays(self):
        spoken = words("Done. and then we all went", 0) + words("home.", 9)
        self.assertEqual(speech.retime_stranded(spoken), spoken)

    def test_words_with_no_sentence_before_them_stay(self):
        spoken = words("and so", 0) + words("home.", 9)
        self.assertEqual(speech.retime_stranded(spoken), spoken)


class Cutting(unittest.TestCase):
    def test_initials_do_not_end_a_sentence(self):
        cues = speech.resegment(words("Vice President J.D. Vance said so."))
        self.assertEqual([cue["text"] for cue in cues], ["Vice President J.D. Vance said so."])

    def test_a_hyphenated_word_is_not_cut_at_its_hyphen(self):
        text = "and it is not looking good but President Trump is laser -focused on it now."
        cues = speech.resegment(words(text, step=0.2))
        self.assertIn("laser-focused", " ".join(cue["text"] for cue in cues))
        for n in range(20, 60, 4):
            for cue in speech.resegment(words(text, step=0.2), n):
                self.assertFalse(cue["text"].startswith("-"))


class ApplyText(unittest.TestCase):
    def test_punctuation_and_capitals_keep_the_times(self):
        cue = speech_cue(*words("was jon husted's ass president"))
        before = [word[:2] for word in cue["words"]]
        spans, marked = polish.apply_text(cue, "was Jon Husted's ass. President")
        self.assertEqual(cue["text"], "was Jon Husted's ass. President")
        self.assertEqual([word[:2] for word in cue["words"]], before)
        self.assertEqual(spans, [])
        self.assertFalse(marked)

    def test_a_wrong_word_is_listed_and_takes_the_old_words_time(self):
        cue = speech_cue(*words("then cute hotters came"))
        spans, _ = polish.apply_text(cue, "Then cute otters came.")
        self.assertEqual(spans, [("hotters", "otters")])
        self.assertEqual(cue["words"][2][:2], [1.0, 1.4])

    def test_one_word_heard_as_two_shares_its_time(self):
        cue = speech_cue(*words("the attacks came"))
        polish.apply_text(cue, "The a tax came")
        self.assertEqual([word[2] for word in cue["words"]], ["The", "a", "tax", "came"])
        self.assertAlmostEqual(cue["words"][2][1], 0.9)

    def test_a_marked_reveal_starts_its_own_cue(self):
        cue = speech_cue(*words("saving you money on zoloft."))
        _, marked = polish.apply_text(cue, "Saving you money on |Zoloft.")
        self.assertTrue(marked)
        cues = speech.resegment(cue["words"])
        self.assertEqual([c["text"] for c in cues], ["Saving you money on", "Zoloft."])

    def test_a_marker_set_apart_from_the_word_counts_too(self):
        self.assertEqual(polish.read_text("on | Zoloft."), (["on", "Zoloft."], 1))

    def test_a_marker_on_the_first_word_is_ignored(self):
        cue = speech_cue(*words("zoloft saves money"))
        self.assertFalse(polish.apply_text(cue, "| Zoloft saves money.")[1])

    def test_an_answer_for_another_cue_is_refused(self):
        cue = speech_cue(*words("one two three four five six seven"))
        self.assertIsNone(polish.apply_text(cue, "Nothing here is at all the same as before."))
        self.assertEqual(cue["text"], "one two three four five six seven")


class Polish(unittest.TestCase):
    def test_sentences_found_by_claude_recut_the_cues(self):
        cues = [speech_cue(*words("our party is about to lose the midterms stop", 0)),
                speech_cue(*words("playing smash or pass with each other", 5))]
        claude = Claude({"cues": [{"id": 0, "text": "Our party is about to lose the midterms. Stop"},
                                  {"id": 1, "text": "playing smash or pass with each other."}]})
        result, changes = polish.polish(cues, [], {}, claude, 42, log=lambda line: None)
        self.assertEqual([cue["text"] for cue in result],
                         ["Our party is about to lose the midterms.", "Stop playing smash or", "pass with each other."])
        self.assertEqual(changes, [])

    def test_caption_cues_take_new_text_and_lyric_cues_stay(self):
        cues = [{"start": 0, "end": 2, "text": "our tour has Begun", "source": "captions", "words": None},
                {"start": 3, "end": 5, "text": "♪ A line ♪", "source": "lyrics", "words": None}]
        claude = Claude({"cues": [{"id": 0, "text": "Our tour has begun."}, {"id": 1, "text": "changed"}]})
        result, _ = polish.polish(cues, [], {}, claude, 42, log=lambda line: None)
        self.assertEqual([cue["text"] for cue in result], ["Our tour has begun.", "♪ A line ♪"])
        self.assertNotIn("♪", claude.prompts[0].split("The lines, as id")[1].split("The broadcaster's")[0])


class Outside(unittest.TestCase):
    def test_stretches_outside_the_programme_are_not_sent(self):
        cues = [speech_cue(*words("some words here.", i * 3.0)) for i in range(polish.CHUNK_CUES * 3)]
        claude = Claude({"cues": []})
        facts = {"segments": [{"start_seconds": 330, "end_seconds": 400}]}
        polish.polish(cues, [], facts, claude, 42, log=lambda line: None)
        self.assertEqual(len(claude.prompts), 1)


class Failing:
    """Answers some calls and fails the others."""

    def __init__(self, answered):
        self.answered = answered

    def ask(self, prompt, schema, **kwargs):
        self.answered -= 1
        if self.answered < 0:
            raise llm.LLMError("not logged in")
        return {"cues": []}


class PolishFailures(unittest.TestCase):
    def cues(self, stretches):
        return [speech_cue(*words("some words here.", i * 3.0)) for i in range(polish.CHUNK_CUES * stretches)]

    def test_a_run_claude_mostly_did_not_answer_fails(self):
        with self.assertRaisesRegex(llm.LLMError, r"(\d+) of \1 stretches got no answer: not logged in"):
            polish.polish(self.cues(4), [], {}, Failing(0), 42, log=lambda line: None)

    def test_one_stretch_in_many_going_unedited_is_tolerated(self):
        result, _ = polish.polish(self.cues(8), [], {}, Failing(8), 42, log=lambda line: None)
        self.assertTrue(result)


class Lyrics(unittest.TestCase):
    def test_the_right_song_with_times_is_chosen(self):
        plain = {"trackName": "A Song", "artistName": "Singer", "plainLyrics": "one"}
        timed = {"trackName": "A Song", "artistName": "Singer", "syncedLyrics": "[00:10.50] one\n[01:02.00]two\n[01:05.00]"}
        other = {"trackName": "Another", "artistName": "Singer", "syncedLyrics": "[00:01.00]x"}
        found = lyrics.choose([other, plain, timed], "Singer", "A Song")
        self.assertEqual(lyrics.parse_lyrics(found), [(10.5, "one"), (62.0, "two")])

    def test_plain_lyrics_serve_when_none_have_times(self):
        found = lyrics.choose([{"trackName": "A Song", "artistName": "Singer", "plainLyrics": "one\n\ntwo"}], "Singer", "A Song")
        self.assertEqual(lyrics.parse_lyrics(found), [(None, "one"), (None, "two")])

    def facts(self):
        return {"segments": [{"kind": "music", "title": "Singer: A Song", "start_seconds": 100, "end_seconds": 200,
                              "songs": [{"artist": "Singer", "title": "A Song"}]}]}

    def test_lines_replace_what_was_heard_and_leave_the_introduction(self):
        heard = [speech_cue(*words("ladies and gentlemen", 100)), speech_cue(*words("wrong words here", 112)),
                 {"start": 118, "end": 120, "text": "♪ fragment", "source": "captions", "words": None},
                 speech_cue(*words("thank you", 190))]
        lines = [(10.0, "first"), (14.0, "second"), (18.0, "third"), (22.0, "fourth")]
        claude = Claude({"lines": [{"line": 1, "text": "", "start": "0:01:50.0"}, {"line": 2, "text": "", "start": "0:01:54.0"},
                                   {"line": 3, "text": "as sung", "start": "0:01:58.0"}, {"line": 0, "text": "added", "start": "0:02:02.5"},
                                   {"line": 4, "text": "", "start": "0:02:04.0"}, {"line": 2, "text": "", "start": "0:02:06.0"}]})
        result = lyrics.place(heard, self.facts(), [], claude, find=lambda a, t: lines, log=lambda line: None)
        self.assertEqual([cue["text"] for cue in result],
                         ["ladies and gentlemen", "♪ first ♪", "♪ second ♪", "♪ as sung ♪", "♪ added ♪", "♪ fourth ♪",
                          "♪ second ♪", "thank you"])
        self.assertEqual(result[1]["end"], 114.0)
        self.assertEqual(result[6]["end"], 126 + lyrics.LINE_SECONDS)

    def placed_lines(self):
        return {"lines": [{"line": 1, "text": "", "start": f"0:01:{50 + 2 * n}.0"} for n in range(4)]}

    def test_what_was_heard_is_the_evidence(self):
        claude = Claude(self.placed_lines())
        lyrics.place([], self.facts(), [], claude, hear=lambda start, end, artist: [[110.0, 110.4, artist], [110.5, 110.9, "sang"]],
                     find=lambda a, t: [(None, "first")], log=lambda line: None)
        self.assertIn("00:01:50.0 | Singer sang", claude.prompts[0])

    def test_when_listening_fails_the_transcript_is_the_evidence(self):
        def deaf(start, end, artist):
            raise RuntimeError("no audio")
        claude = Claude(self.placed_lines())
        lyrics.place([speech_cue(*words("wrong words here", 112))], self.facts(), [], claude, hear=deaf,
                     find=lambda a, t: [(None, "first")], log=lambda line: None)
        self.assertIn("wrong words here", claude.prompts[0])

    def test_heard_words_break_into_lines_at_pauses(self):
        self.assertEqual(lyrics.heard_lines([[1.0, 1.2, "one"], [1.3, 1.5, "two"], [5.0, 5.2, "three"]]),
                         "00:00:01.0 | one two\n00:00:05.0 | three")

    def test_a_song_with_no_lyrics_keeps_its_cues(self):
        heard = [speech_cue(*words("wrong words here", 112))]
        result = lyrics.place(heard, self.facts(), [], Claude({"lines": []}), find=lambda a, t: [], log=lambda line: None)
        self.assertEqual(result, heard)

    def test_too_few_placed_lines_keep_the_cues(self):
        heard = [speech_cue(*words("wrong words here", 112))]
        claude = Claude({"lines": [{"line": 1, "text": "", "start": "0:01:50.0"}, {"line": 1, "text": "", "start": "9:00:00.0"}]})
        result = lyrics.place(heard, self.facts(), [], claude, find=lambda a, t: [(None, "first")], log=lambda line: None)
        self.assertEqual(result, heard)

    def test_lookup_trouble_keeps_the_cues(self):
        def down(artist, title):
            raise OSError("no route")
        heard = [speech_cue(*words("wrong words here", 112))]
        self.assertEqual(lyrics.place(heard, self.facts(), [], Claude({}), find=down, log=lambda line: None), heard)


class Shows(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()

    def write(self, name, text):
        with open(os.path.join(self.folder, name), "w", encoding="utf-8") as f:
            f.write(text)

    def test_the_show_is_the_library_folder_without_its_year(self):
        self.assertEqual(shows.show_name("/tv/Some Show (1975)/Season 52/Some Show (1975) - S52E04.ts"), "Some Show")

    def test_options_and_notes_are_read(self):
        self.write("Some Show.md", "---\nlyrics: true\n---\n<!-- a remark -->\nExplain little.\n")
        self.assertEqual(shows.load("/tv/Some Show (1975)/Season 1/a.ts", self.folder),
                         {"notes": "Explain little.", "lyrics": True})

    def test_a_show_with_no_file_gets_nothing(self):
        self.assertEqual(shows.load("/tv/Other (2001)/Season 1/a.ts", self.folder), {"notes": "", "lyrics": False})

    def test_a_file_without_options_is_all_notes(self):
        self.write("Some Show.md", "Explain little.\n")
        self.assertEqual(shows.load("/tv/Some Show/Season 1/a.ts", self.folder), {"notes": "Explain little.", "lyrics": False})


if __name__ == "__main__":
    unittest.main()
