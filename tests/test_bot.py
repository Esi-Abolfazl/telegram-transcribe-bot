import unittest
from datetime import datetime, timezone

from telegram import Chat, Message, Update, User, Voice

from telegram_whisper_bot import parse_allowed_ids, split_message, transcribable


def update_from(user_id: int, *, voice: bool = True) -> Update:
    message = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type=Chat.PRIVATE),
        from_user=User(id=user_id, first_name="x", is_bot=False),
        voice=Voice(file_id="f", file_unique_id="u", duration=3) if voice else None,
        text=None if voice else "hi",
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
        f = transcribable([12])
        self.assertTrue(f.check_update(update_from(12)))
        self.assertFalse(f.check_update(update_from(99)))
        self.assertFalse(f.check_update(update_from(12, voice=False)))

    def test_empty_list_lets_nobody_through(self):
        self.assertFalse(transcribable([]).check_update(update_from(12)))


if __name__ == "__main__":
    unittest.main()
