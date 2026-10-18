import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from telegram import Animation, Chat, PhotoSize, Sticker, Document, Message, Update, User, Video, VideoNote, Voice

from telegram_transcribe_bot import NoAudio, accepts_model, current_model, lay_out, models_html, parse_allowed_ids, run, split_message, to_mp3, incoming_media


VOICE = {"voice": Voice(file_id="f", file_unique_id="u", duration=3)}


def update_from(user_id: int, media: dict | None = None) -> Update:
    message = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type=Chat.PRIVATE),
        from_user=User(id=user_id, first_name="x", is_bot=False),
        **(VOICE if media is None else media or {"text": "hi"}),
    )
    return Update(update_id=1, message=message)


class SplitMessage(unittest.TestCase):
    def test_short_text_is_one_chunk(self):
        self.assertEqual(split_message("سلام دنیا"), ["سلام دنیا"])

    def test_empty_text_is_no_chunk(self):
        self.assertEqual(split_message("  \n "), [])

    def test_breaks_at_a_space_and_keeps_every_word(self):
        words = ["کلمه"] * 3000  # 14999 chars
        chunks = split_message(" ".join(words))
        self.assertTrue(all(0 < len(c) <= 4096 for c in chunks))
        self.assertEqual(" ".join(chunks).split(), words)

    def test_hard_cut_without_whitespace(self):
        self.assertEqual(split_message("a" * 10, limit=4), ["aaaa", "aaaa", "aa"])


class AllowList(unittest.TestCase):
    def test_parses_commas_and_spaces(self):
        self.assertEqual(parse_allowed_ids("12, 34 56"), [12, 34, 56])
        self.assertEqual(parse_allowed_ids(""), [])

    def test_only_allowed_users_audio_passes(self):
        f = incoming_media([12])
        self.assertTrue(f.check_update(update_from(12)))
        self.assertFalse(f.check_update(update_from(99)))
        self.assertFalse(f.check_update(update_from(12, {})))

    def test_videos_and_video_files_pass(self):
        f = incoming_media([12])
        for media in (
            {"video": Video(file_id="f", file_unique_id="u", width=1, height=1, duration=3)},
            {"video_note": VideoNote(file_id="f", file_unique_id="u", length=1, duration=3)},
            {"document": Document(file_id="f", file_unique_id="u", mime_type="video/mp4")},
        ):
            self.assertTrue(f.check_update(update_from(12, media)), media)

    def test_media_without_sound_passes_so_the_bot_can_say_so(self):
        f = incoming_media([12])
        gif = Animation(file_id="f", file_unique_id="u", width=1, height=1, duration=1)
        for media in (
            {"animation": gif, "document": Document("f", "u", mime_type="video/mp4")},
            {"photo": (PhotoSize(file_id="f", file_unique_id="u", width=1, height=1),)},
            {"sticker": Sticker("f", "u", 1, 1, False, False, Sticker.REGULAR)},
            {"document": Document(file_id="f", file_unique_id="u", mime_type="application/pdf")},
        ):
            self.assertTrue(f.check_update(update_from(12, media)), media)

    def test_empty_list_lets_nobody_through(self):
        self.assertFalse(incoming_media([]).check_update(update_from(12)))


class SilentMedia(unittest.IsolatedAsyncioTestCase):
    async def test_a_video_without_sound_is_no_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            silent = Path(tmp, "silent.mp4")
            await run("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=16x16:d=1", str(silent))
            with self.assertRaises(NoAudio):
                await to_mp3(silent.read_bytes())

    async def test_a_file_that_is_not_media_is_no_audio(self):
        with self.assertRaises(NoAudio):
            await to_mp3(b"%PDF-1.4 not audio at all")


def timed(*spec):
    """Words from (text, start, end) or (text, start, end, type) tuples."""
    return {"words": [dict(zip(("word", "start", "end", "type"), w)) for w in spec]}


class LayOut(unittest.TestCase):
    def test_a_pause_after_enough_words_starts_a_line(self):
        words = [(f"w{i}", i, i + 0.9) for i in range(6)] + [("next", 6.4, 6.6)]
        self.assertEqual(lay_out(timed(*words)), "w0 w1 w2 w3 w4 w5\nnext")

    def test_a_short_breath_keeps_the_line(self):
        self.assertEqual(lay_out(timed(("one", 0, 0.5), ("two", 1.0, 1.2))), "one two")

    def test_a_long_silence_starts_a_paragraph_and_events_stand_alone(self):
        text = lay_out(timed(("[music]", 0, 5, "audio_event"), ("hi", 9, 9.2), ("there.", 9.3, 9.5), ("Bye", 9.6, 9.8)))
        self.assertEqual(text, "[music]\n\nhi there.\nBye")

    def test_without_timings_falls_back_to_segments_then_text(self):
        self.assertEqual(lay_out({"segments": [{"text": " a "}, {"text": "b"}], "text": "a b"}), "a\nb")
        self.assertEqual(lay_out({"text": " plain "}), "plain")


class ModelChoice(unittest.TestCase):
    def test_saved_choice_wins_over_env(self):
        self.assertEqual(current_model({"model": "a/b"}, "env/model"), "a/b")
        self.assertEqual(current_model({}, "env/model"), "env/model")

    def test_listed_endpoint_refuses_unknown_ids(self):
        self.assertTrue(accepts_model("a/b", ["a/b", "c/d"]))
        self.assertFalse(accepts_model("typo", ["a/b"]))

    def test_unlisted_endpoint_accepts_any_id(self):
        self.assertTrue(accepts_model("whatever", None))


class ModelCard(unittest.TestCase):
    def test_groups_by_provider_and_marks_the_one_in_use(self):
        card = models_html("b/x", False, ["a/y", "b/x", "b/z", "base"])
        self.assertIn("<summary>a · 1</summary>", card)
        self.assertIn("<summary>b · 2</summary>", card)
        self.assertIn("<summary>local · 1</summary>", card)
        self.assertIn("<code>b/x</code> <b>in use</b>", card)
        self.assertIn("<td>/model</td>", card)

    def test_escapes_ids(self):
        self.assertIn("<code>a/&lt;x&gt;</code>", models_html("a/<x>", True, None))


if __name__ == "__main__":
    unittest.main()
