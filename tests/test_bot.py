import json
import pickle
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone
from pathlib import Path

import httpx

from telegram.error import TelegramError
from telegram import Animation, Chat, PhotoSize, Sticker, Document, Message, MessageEntity, Update, User, Video, VideoNote, Voice

from telegram_transcribe_bot import ModelsUnavailable, NoAudio, accepts_model, model_ids, quarantine_corrupt_pickle, utf16_units, current_model, lay_out, models_html, parse_allowed_ids, run, split_message, to_mp3, incoming_media, incoming_text
from telegram_transcribe_bot import OPEN_BOT, on_allow, post_init, resolve_admins, user_filter
from telegram_transcribe_bot import Endpoint, NO_SOUND, on_error, on_media, on_model, request, transcribe


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


class SplitEmoji(unittest.TestCase):
    def test_chunks_fit_in_utf16_units_and_rejoin(self):
        text = "😀" * 5000
        chunks = split_message(text)
        self.assertTrue(all(utf16_units(c) <= 4096 for c in chunks))
        self.assertEqual("".join(chunks), text)


class AllowList(unittest.TestCase):
    def test_edited_message_does_not_pass(self):
        edited = Update(update_id=1, edited_message=update_from(12).message)
        self.assertFalse(incoming_media([12]).check_update(edited))
        self.assertTrue(incoming_media([12]).check_update(update_from(12)))

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

    def test_star_means_everyone(self):
        self.assertIsNone(parse_allowed_ids("*"))
        self.assertIsNone(parse_allowed_ids(" * "))

    def test_everyone_passes_with_none_but_not_with_a_list(self):
        self.assertTrue(incoming_media(None).check_update(update_from(99)))
        self.assertTrue(incoming_text(None).check_update(update_from(99, {})))
        self.assertFalse(incoming_media([12]).check_update(update_from(99)))

    def test_admins_resolve_from_the_explicit_list_then_the_allowed_ids(self):
        self.assertEqual(resolve_admins("5", [12]), [5])
        self.assertEqual(resolve_admins("", [12]), [12])
        self.assertEqual(resolve_admins("", None), [])


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


class ModelList(unittest.TestCase):
    def response(self, status, **kw):
        return httpx.Response(status, request=httpx.Request("GET", "http://x/models"), **kw)

    def test_error_status_raises_and_bad_body_is_unknown(self):
        with self.assertRaises(ModelsUnavailable):
            model_ids(self.response(503))
        self.assertIsNone(model_ids(self.response(200, json={"models": []})))
        self.assertEqual(model_ids(self.response(200, json={"data": [{"id": "b"}, {"id": "a"}]})), ["a", "b"])


class CorruptPickle(unittest.TestCase):
    def test_empty_file_moves_aside_and_valid_one_stays(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "bot.pickle")
            path.write_bytes(b"")
            quarantine_corrupt_pickle(path)
            self.assertFalse(path.exists())
            self.assertTrue(Path(tmp, "bot.pickle.corrupt").exists())
            path.write_bytes(pickle.dumps({"bot_data": {"model": "a/b"}}))
            quarantine_corrupt_pickle(path)
            self.assertTrue(path.exists())


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


OR = Endpoint("openrouter", "http://x/v1", "k")
OAI = Endpoint("openai", "http://x/v1", "")


def mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class Request(unittest.IsolatedAsyncioTestCase):
    async def sent(self, endpoint, timed):
        seen = []
        async with mock_client(lambda r: seen.append(r) or httpx.Response(200, json={})) as client:
            await request(client, endpoint, "m/x", b"mp3", timed)
        return seen[0]

    async def test_openrouter_sends_json_with_deny_and_optional_timings(self):
        body = json.loads((await self.sent(OR, True)).content)
        self.assertEqual(body["input_audio"]["format"], "mp3")
        self.assertEqual(body["provider"]["data_collection"], "deny")
        self.assertEqual(body["timestamp_granularities"], ["word"])
        self.assertNotIn("timestamp_granularities", json.loads((await self.sent(OR, False)).content))

    async def test_openai_sends_multipart_with_the_model(self):
        req = await self.sent(OAI, False)
        self.assertIn("multipart/form-data", req.headers["content-type"])
        self.assertIn(b'name="model"', req.content)
        self.assertIn(b"m/x", req.content)


class Transcribe(unittest.IsolatedAsyncioTestCase):
    async def run_with(self, *responses):
        calls = []

        def handler(req):
            calls.append(req)
            return responses[len(calls) - 1]

        real = httpx.AsyncClient
        with patch("telegram_transcribe_bot.to_mp3", AsyncMock(return_value=b"mp3")), patch(
            "telegram_transcribe_bot.httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler))
        ):
            try:
                return calls, await transcribe(b"a", OR, "m/x")
            except RuntimeError as exc:
                return calls, exc

    async def test_a_400_retries_once_without_timings(self):
        calls, text = await self.run_with(httpx.Response(400), httpx.Response(200, json={"text": "hi"}))
        self.assertEqual((len(calls), text), (2, "hi"))
        self.assertNotIn(b"timestamp_granularities", calls[1].content)

    async def test_a_failing_final_status_raises(self):
        calls, exc = await self.run_with(httpx.Response(400), httpx.Response(500, text="boom"))
        self.assertEqual(len(calls), 2)
        self.assertIsInstance(exc, RuntimeError)

    async def test_a_200_error_body_raises_cut_to_500(self):
        _, exc = await self.run_with(httpx.Response(200, json={"error": "e" * 900}))
        self.assertIsInstance(exc, RuntimeError)
        self.assertLessEqual(len(str(exc)), 500)
        self.assertIn("eee", str(exc))


def message(**media):
    msg = MagicMock(message_id=1, animation=None, photo=(), sticker=None, voice=None, audio=None, video=None,
                    video_note=None, document=None)
    for k, v in media.items():
        setattr(msg, k, v)
    msg.reply_text = AsyncMock()
    return msg


class OnMedia(unittest.IsolatedAsyncioTestCase):
    async def call(self, msg):
        update = MagicMock(effective_message=msg)
        await on_media(update, MagicMock(bot_data={}), endpoint=OR, default_model="m/x")

    def voice(self, size=10):
        voice = MagicMock(file_size=size)
        voice.get_file = AsyncMock(return_value=MagicMock(download_as_bytearray=AsyncMock(return_value=bytearray(b"a"))))
        return voice

    async def test_a_voice_gets_a_quoted_transcript_and_the_note_is_deleted(self):
        msg = message(voice=self.voice())
        with patch("telegram_transcribe_bot.transcribe", AsyncMock(return_value="hello")):
            await self.call(msg)
        msg.reply_text.assert_awaited_with("hello", do_quote=True)
        msg.reply_text.return_value.delete.assert_awaited_once()

    async def test_no_audio_gets_the_no_sound_reply(self):
        msg = message(voice=self.voice())
        with patch("telegram_transcribe_bot.transcribe", AsyncMock(side_effect=NoAudio)):
            await self.call(msg)
        msg.reply_text.assert_awaited_with(NO_SOUND, do_quote=True)

    async def test_a_file_over_20_mb_is_refused_without_downloading(self):
        voice = self.voice(size=21 * 1024 * 1024)
        msg = message(voice=voice)
        await self.call(msg)
        voice.get_file.assert_not_awaited()
        self.assertIn("20 MB", msg.reply_text.await_args.args[0])


class OnModel(unittest.IsolatedAsyncioTestCase):
    async def call(self, listing):
        msg = MagicMock(reply_html=AsyncMock(), reply_text=AsyncMock())
        context = MagicMock(args=["a/b"], bot_data={}, application=MagicMock(update_persistence=AsyncMock()))
        with patch("telegram_transcribe_bot.list_models", listing):
            await on_model(MagicMock(effective_message=msg), context, endpoint=OR, default_model="m/x")
        return context.bot_data

    async def test_a_listed_id_is_saved(self):
        self.assertEqual(await self.call(AsyncMock(return_value=["a/b"])), {"model": "a/b"})

    async def test_an_unlisted_id_is_refused(self):
        self.assertEqual(await self.call(AsyncMock(return_value=["c/d"])), {})

    async def test_an_unreachable_endpoint_saves_nothing(self):
        self.assertEqual(await self.call(AsyncMock(side_effect=ModelsUnavailable)), {})


class ManageAccess(unittest.IsolatedAsyncioTestCase):
    async def call(self, args, *, allow=True, env_ids=(12,), admins=(5,), added=None, flt=None):
        msg = MagicMock(reply_text=AsyncMock())
        bot_data = {} if added is None else {"allowed": added}
        context = MagicMock(args=args, bot_data=bot_data, application=MagicMock(update_persistence=AsyncMock()))
        flt = flt or user_filter([*(env_ids or ()), *admins])
        await on_allow(
            MagicMock(effective_message=msg), context, allow=allow, flt=flt, env_ids=None if env_ids is None else list(env_ids), admins=list(admins)
        )
        return bot_data, msg.reply_text.await_args.args[0], flt

    async def test_allow_adds_once(self):
        data, reply, flt = await self.call(["77"])
        self.assertEqual((data["allowed"], reply), ([77], "Allowed: 77"))
        data, _, _ = await self.call(["77"], added=[77], flt=flt)
        self.assertEqual(data["allowed"], [77])

    async def test_allow_rejects_a_non_integer_and_a_negative(self):
        for arg in ("abc", "-3", "0", "1.5"):
            data, reply, _ = await self.call([arg])
            self.assertEqual(data, {})
            self.assertTrue(reply.startswith("Usage:"))

    async def test_deny_removes_an_added_id(self):
        data, reply, _ = await self.call(["77"], allow=False, added=[77, 8])
        self.assertEqual((data["allowed"], reply), ([8], "Denied: 77"))

    async def test_deny_refuses_env_and_admin_ids_and_reports_unknown(self):
        for uid, text in ((12, "12 is set in .env and stays allowed."), (5, "5 is set in .env and stays allowed."), (9, "9 was not in the list.")):
            data, reply, flt = await self.call([str(uid)], allow=False, added=[77])
            self.assertEqual((data["allowed"], reply), ([77], text))
        self.assertTrue(incoming_media(flt).check_update(update_from(12)))

    async def test_the_shared_filter_follows_allow_and_deny(self):
        _, _, flt = await self.call(["77"])
        self.assertTrue(incoming_media(flt).check_update(update_from(77)))
        await self.call(["77"], allow=False, added=[77], flt=flt)
        self.assertFalse(incoming_media(flt).check_update(update_from(77)))

    async def test_everyone_mode_changes_nothing(self):
        data, reply, _ = await self.call(["77"], env_ids=None)
        self.assertEqual((data, reply), ({}, OPEN_BOT))

    async def test_saved_ids_are_restored_at_startup(self):
        flt = user_filter([12])
        app = MagicMock(bot_data={"allowed": [77]}, bot=MagicMock(set_my_commands=AsyncMock()))
        await post_init(app, flt=flt)
        self.assertTrue(incoming_media(flt).check_update(update_from(77)))


class PlainText(unittest.TestCase):
    def test_only_an_allowed_users_plain_text_passes(self):
        f = incoming_text([12])
        self.assertTrue(f.check_update(update_from(12, {})))
        self.assertFalse(f.check_update(update_from(99, {})))
        command = update_from(12, {"text": "/model", "entities": (MessageEntity(MessageEntity.BOT_COMMAND, 0, 6),)})
        self.assertFalse(f.check_update(command))


class ErrorHandler(unittest.IsolatedAsyncioTestCase):
    async def test_replies_with_fixed_text_and_survives_no_update(self):
        update = MagicMock(spec=Update, effective_message=MagicMock(reply_text=AsyncMock()))
        context = MagicMock(error=RuntimeError("secret"))
        await on_error(update, context)
        self.assertNotIn("secret", update.effective_message.reply_text.await_args.args[0])
        update.effective_message.reply_text.side_effect = TelegramError("x")
        await on_error(update, context)
        await on_error(None, context)


if __name__ == "__main__":
    unittest.main()
