"""Telegram bot that answers a voice note, audio file or video with its transcript."""

import asyncio
import contextlib
import base64
import html
import logging
import os
import pickle
import tempfile
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import httpx
from telegram import BotCommand, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PersistenceInput,
    PicklePersistence,
    filters,
)

# Counted by Telegram in UTF-16 units (emoji count double), so split_message measures in units, not len().
TELEGRAM_LIMIT = 4096
# The Bot API refuses getFile above this size.
DOWNLOAD_LIMIT = 20 * 1024 * 1024
APIS = ("openrouter", "openai")
OPENROUTER_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "elevenlabs/scribe-v2"
# Measured on scribe-v2 word timings (2026-10-18): sung line ends pause 0.3 to 0.42 s, speech
# 0.36 to 1.6 s. MIN_LINE_WORDS keeps a breath mid-sentence from leaving a two-word line.
LINE_PAUSE = 0.3
PARAGRAPH_PAUSE = 2.0
MIN_LINE_WORDS = 6
SENTENCE_ENDS = (".", "?", "!", "؟")
NO_SOUND = "This has no sound to transcribe. Send a voice note, audio or a video with sound."
HELP = (
    "Send me a voice note, an audio file, a video or a round video, forwarded or not, and I reply with "
    "its transcript. Telegram lets bots download up to 20 MB.\n\n"
    "/model shows the transcription model and the ones you can pick (admins only).\n"
    "/model <id> switches to another model (admins only).\n"
    "/model reset goes back to the model set in .env (admins only).\n"
    "/allow <id>, /deny <id> and /users manage who may use the bot (admins only)."
)
NOT_ADMIN = "Only an admin can do that."
OPEN_BOT = "The bot is open to everyone, so the list is not used."

UNREACHABLE = "Could not reach the endpoint to list its models. The model was not changed."

log = logging.getLogger("telegram_transcribe_bot")


@dataclass(frozen=True)
class Endpoint:
    """Where audio goes. `api` picks the request shape: OpenRouter's JSON or OpenAI's multipart."""

    api: str
    base_url: str
    api_key: str

    def headers(self) -> dict[str, str]:
        # A local server usually runs without a key.
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}


def utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split text into chunks of at most `limit` UTF-16 units, breaking at the last newline or space."""
    chunks = []
    text = text.strip()
    while utf16_units(text) > limit:
        fit = units = 0
        for char in text:  # whole characters only, so a surrogate pair is never cut
            units += 2 if ord(char) > 0xFFFF else 1
            if units > limit:
                break
            fit += 1
        cut = max(text.rfind("\n", 0, fit + 1), text.rfind(" ", 0, fit + 1))
        if cut <= 0:
            cut = fit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


def parse_allowed_ids(raw: str) -> list[int] | None:
    """The id list, or None for "*" (everyone)."""
    if raw.strip() == "*":
        return None
    return [int(part) for part in raw.replace(",", " ").split()]


def resolve_admins(admin_raw: str, allowed: list[int] | None) -> list[int]:
    """Who may run /model: ADMIN_USER_IDS when set, else the allowed ids (nobody when everyone is allowed)."""
    return parse_allowed_ids(admin_raw) or allowed or []


def user_filter(ids: list[int] | filters.BaseFilter | None) -> filters.BaseFilter:
    """The one place that decides who may use the bot: None means everyone. A filter passes through, so one shared object can be mutated at runtime."""
    if isinstance(ids, filters.BaseFilter):
        return ids
    return filters.ALL if ids is None else filters.User(user_id=ids)


def parse_user_id(args: list[str]) -> int | None:
    """The one positive integer id in a command's arguments, else None."""
    if len(args) == 1 and args[0].isascii() and args[0].isdigit() and int(args[0]) > 0:
        return int(args[0])
    return None


def change_allowed(added: list[int], protected: set[int], user_id: int, add: bool) -> tuple[list[int], str]:
    """The new Telegram-added list and the reply for /allow (add) or /deny."""
    if add:
        return (added if user_id in added else [*added, user_id]), f"Allowed: {user_id}"
    if user_id in protected:
        return added, f"{user_id} is set in .env and stays allowed."
    if user_id not in added:
        return added, f"{user_id} was not in the list."
    return [i for i in added if i != user_id], f"Denied: {user_id}"


def incoming_media(allowed_ids: list[int] | filters.BaseFilter | None) -> filters.BaseFilter:
    """Every file or media message from allowed users: anything without sound still gets an answer."""
    media = (
        filters.VOICE | filters.AUDIO | filters.VIDEO | filters.VIDEO_NOTE | filters.Document.ALL
        | filters.ANIMATION | filters.PHOTO | filters.Sticker.ALL
    )
    # Handlers also match edited_message; an edited caption must not pay for a second transcription.
    return filters.UpdateType.MESSAGE & media & user_filter(allowed_ids)


def incoming_text(allowed_ids: list[int] | filters.BaseFilter | None) -> filters.BaseFilter:
    """Plain text (no command) from allowed users: they get the help text."""
    return filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND & user_filter(allowed_ids)


def current_model(bot_data: dict, default: str) -> str:
    """The /model choice when one is saved, else the .env model."""
    return bot_data.get("model") or default


def accepts_model(model: str, available: list[str] | None) -> bool:
    """Any id passes when the endpoint can't list its models; otherwise it must be listed."""
    return available is None or model in available


def models_html(model: str, from_env: bool, available: list[str] | None) -> str:
    """The /model card as rich-message HTML: the model in use, then every offered model grouped by provider."""
    rows = [
        "<h1>Transcription model</h1>",
        "<table bordered striped>",
        "<tr><th>In use</th><th>Set by</th></tr>",
        f"<tr><td><code>{html.escape(model)}</code></td><td>{'.env' if from_env else '/model'}</td></tr>",
        "</table>",
    ]
    if available is None:
        rows.append("<aside>This endpoint does not list its models, so any id is accepted.</aside>")
    else:
        groups: dict[str, list[str]] = {}
        for model_id in available:
            provider, slash, _ = model_id.partition("/")
            groups.setdefault(provider if slash else "local", []).append(model_id)
        for provider, ids in sorted(groups.items()):
            rows += [f"<details><summary>{html.escape(provider)} · {len(ids)}</summary>", "<ul>"]
            rows += [
                f"<li><code>{html.escape(i)}</code>{' <b>in use</b>' if i == model else ''}</li>" for i in ids
            ]
            rows += ["</ul>", "</details>"]
    rows.append("<footer>Tap an id to copy it. /model &lt;id&gt; switches · /model reset goes back to .env</footer>")
    return "\n".join(rows)


class NoAudio(Exception):
    """Nothing to hear: a silent video, or a file that isn't media."""


async def run(*cmd: str) -> str:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {err.decode(errors='replace').strip()}")
    return out.decode()


async def to_mp3(audio: bytes) -> bytes:
    # One known format for every input (OGG/Opus, m4a, wav...); speech models resample to 16 kHz mono anyway.
    # Temp files, not pipes: an m4a with its index at the end needs a seekable input.
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp, "in"), Path(tmp, "out.mp3")
        src.write_bytes(audio)
        try:
            streams = await run(
                "ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(src)
            )
        except RuntimeError as exc:
            raise NoAudio from exc  # not media at all: a PDF, a zip, a text file
        if not streams.strip():
            raise NoAudio
        await run(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(src),
            "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(dst),
        )
        return dst.read_bytes()


def lay_out(result: dict) -> str:
    """The transcript with a line per phrase, cut at pauses when the model returns word timings."""
    words = [w for w in result.get("words") or [] if w.get("type") != "spacing" and "start" in w]
    if not words:
        segments = result.get("segments") or []
        if len(segments) > 1:
            return "\n".join(s["text"].strip() for s in segments)
        return result.get("text", "").strip()
    lines: list[str] = []
    line: list[str] = []

    def flush() -> None:
        if line:
            lines.append(" ".join(line))
            line.clear()

    prev_end = words[0]["start"]
    for w in words:
        gap = w["start"] - prev_end
        prev_end = w["end"]
        if gap >= PARAGRAPH_PAUSE:
            flush()
            lines.append("")
        elif line and (gap >= LINE_PAUSE and len(line) >= MIN_LINE_WORDS or line[-1].endswith(SENTENCE_ENDS)):
            flush()
        if w.get("type") == "audio_event":
            flush()
            lines.append(w["word"].strip())
        else:
            line.append(w["word"].strip())
    flush()
    return "\n".join(lines).strip()


async def request(client: httpx.AsyncClient, endpoint: Endpoint, model: str, mp3: bytes, timed: bool) -> httpx.Response:
    url = f"{endpoint.base_url.rstrip('/')}/audio/transcriptions"
    if endpoint.api == "openrouter":
        # Only the JSON shape carries OpenRouter's data policy: skip providers that keep audio.
        body = {
            "model": model,
            "input_audio": {"data": base64.b64encode(mp3).decode(), "format": "mp3"},
            "provider": {"data_collection": "deny"},
        }
        if timed:
            body |= {"response_format": "verbose_json", "timestamp_granularities": ["word"]}
        return await client.post(url, json=body, headers=endpoint.headers())
    data = {"model": model}
    if timed:
        data |= {"response_format": "verbose_json", "timestamp_granularities[]": "word"}
    files = {"file": ("audio.mp3", mp3, "audio/mpeg")}
    return await client.post(url, data=data, files=files, headers=endpoint.headers())


async def transcribe(audio: bytes, endpoint: Endpoint, model: str) -> str:
    # shortcut: one request per file; OpenRouter's providers time out after 60 s, so a very long
    # recording (tens of minutes) can fail. Split it into chunks with ffmpeg if that ever happens.
    mp3 = await to_mp3(audio)
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await request(client, endpoint, model, mp3, timed=True)
        if resp.status_code == 400:
            # Some models take only plain JSON, without word timings; ask once more without them.
            resp = await request(client, endpoint, model, mp3, timed=False)
    if resp.status_code != 200:
        raise RuntimeError(f"{endpoint.api} {resp.status_code}: {resp.text[:500]}")
    body = resp.json()
    if isinstance(body, dict) and "error" in body:  # some providers answer 200 with an error body
        raise RuntimeError(f"{endpoint.api}: {body['error']}"[:500])
    return lay_out(body)


class ModelsUnavailable(Exception):
    """The models request failed (network error or error status): the endpoint's answer is unknown."""


def model_ids(resp: httpx.Response) -> list[str] | None:
    """Model ids from a /models response; None when it has no usable list, ModelsUnavailable on an error status."""
    try:
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise ModelsUnavailable from exc
    try:
        return sorted(m["id"] for m in resp.json()["data"])
    except (KeyError, TypeError, ValueError):
        log.warning("no model list in the response from %s", resp.url, exc_info=True)
        return None


async def list_models(endpoint: Endpoint) -> list[str] | None:
    """Transcription model ids the endpoint offers, or None when it can't say; raises ModelsUnavailable on a failed request."""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            # OpenRouter filters by this; OpenAI-compatible servers ignore it.
            resp = await client.get(
                f"{endpoint.base_url.rstrip('/')}/models",
                params={"output_modalities": "transcription"},
                headers=endpoint.headers(),
            )
    except httpx.HTTPError as exc:
        log.warning("could not list models at %s", endpoint.base_url, exc_info=True)
        raise ModelsUnavailable from exc
    return model_ids(resp)


# Updates run 4 at a time; each message's first reply waits its turn so replies keep the order sent.
# shortcut: one lock per chat id is kept for the process lifetime; prune idle ones if an open bot sees very many chats.
_first_reply_locks: dict[int, asyncio.Lock] = {}


def first_reply(chat_id: int) -> asyncio.Lock:
    return _first_reply_locks.setdefault(chat_id, asyncio.Lock())


async def edit_or_reply(msg, working, text: str) -> None:
    """Put `text` into the placeholder; send it as a quoted reply when there is none or the edit fails."""
    if working:
        try:
            await working.edit_text(text)
            return
        except TelegramError:
            pass
    await msg.reply_text(text, do_quote=True)


async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE, *, endpoint: Endpoint, default_model: str) -> None:
    msg = update.effective_message
    async with first_reply(msg.chat_id):
        if msg.animation or msg.photo or msg.sticker:
            await msg.reply_text(NO_SOUND, do_quote=True)
            return
        media = msg.voice or msg.audio or msg.video or msg.video_note or msg.document
        if media.file_size and media.file_size > DOWNLOAD_LIMIT:
            await msg.reply_text("Telegram lets bots download files up to 20 MB only.", do_quote=True)
            return
        model = current_model(context.bot_data, default_model)
        # Tells the sender it arrived, and becomes the transcript. Best effort: a network blip
        # on this note must not cost the transcript (edit_or_reply falls back to a new message).
        working = None
        with contextlib.suppress(TelegramError):
            working = await msg.reply_text(f"Transcribing with {model}…", do_quote=True)
    try:
        audio = bytes(await (await media.get_file()).download_as_bytearray())
        text = await transcribe(audio, endpoint, model)
    except NoAudio:
        await edit_or_reply(msg, working, NO_SOUND)
        return
    except Exception as exc:
        log.exception("transcription failed for message %s", msg.message_id)
        # Only allowed users get here, and the message carries no secret.
        await edit_or_reply(msg, working, f"Transcription failed with {model}:\n{exc}"[:TELEGRAM_LIMIT])
        return
    chunks = split_message(text) or ["(no speech found)"]
    await edit_or_reply(msg, working, chunks[0])
    for chunk in chunks[1:]:
        await msg.reply_text(chunk, do_quote=True)
    # Length only: transcripts stay out of the logs.
    log.info(
        "message %s from user %s: %s via %s -> %d chars in %d reply(s)",
        msg.message_id, update.effective_user.id, type(media).__name__, model, len(text), len(chunks),
    )


async def on_model(update: Update, context: ContextTypes.DEFAULT_TYPE, *, endpoint: Endpoint, default_model: str) -> None:
    msg = update.effective_message
    if not context.args:
        try:
            available = await list_models(endpoint)
        except ModelsUnavailable:
            await msg.reply_text(UNREACHABLE)
            return
        card = models_html(current_model(context.bot_data, default_model), "model" not in context.bot_data, available)
        # python-telegram-bot 22.8 has no send_rich_message yet; this is the raw Bot API call.
        await context.bot.do_api_request(
            "sendRichMessage", api_kwargs={"chat_id": msg.chat_id, "rich_message": {"html": card}}
        )
        return
    requested = context.args[0]
    if requested == "reset":
        context.bot_data.pop("model", None)
        await context.application.update_persistence()
        await msg.reply_html(f"Back to the .env model: <code>{html.escape(default_model)}</code>")
        return
    try:
        available = await list_models(endpoint)
    except ModelsUnavailable:
        await msg.reply_text(UNREACHABLE)
        return
    if not accepts_model(requested, available):
        await msg.reply_html(
            f"<code>{html.escape(requested)}</code> is not a transcription model this endpoint offers. /model lists them."
        )
        return
    context.bot_data["model"] = requested
    # Save now, not at the next 60 s flush: a crash or SIGKILL in between would drop the choice.
    await context.application.update_persistence()
    log.info("model set to %s by user %s", requested, update.effective_user.id)
    await msg.reply_html(f"Model: <code>{html.escape(requested)}</code>. Send a voice to try it.")


async def on_allow(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, allow: bool, flt: filters.BaseFilter, env_ids: list[int] | None, admins: list[int]
) -> None:
    msg = update.effective_message
    if env_ids is None:
        await msg.reply_text(OPEN_BOT)
        return
    user_id = parse_user_id(context.args)
    if user_id is None:
        await msg.reply_text(f"Usage: /{'allow' if allow else 'deny'} <user id> (a positive number).")
        return
    protected = {*env_ids, *admins}
    added, reply = change_allowed(context.bot_data.get("allowed", []), protected, user_id, allow)
    context.bot_data["allowed"] = added
    if allow:
        flt.add_user_ids(user_id)
    elif user_id not in protected:
        flt.remove_user_ids(user_id)
    await context.application.update_persistence()
    log.info("%s %s by user %s", "allow" if allow else "deny", user_id, update.effective_user.id)
    await msg.reply_text(reply)


async def on_users(update: Update, context: ContextTypes.DEFAULT_TYPE, *, env_ids: list[int] | None, admins: list[int]) -> None:
    def show(ids) -> str:
        return ", ".join(map(str, ids)) or "none"

    await update.effective_message.reply_text(
        f"Admins: {show(admins)}\n.env: {'everyone' if env_ids is None else show(env_ids)}\n"
        f"Added from Telegram: {show(context.bot_data.get('allowed', []))}"
    )


async def on_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    async with first_reply(msg.chat_id):
        await msg.reply_text(HELP, do_quote=True)


async def on_not_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(NOT_ADMIN)


async def on_stranger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user:
        log.info("ignored a message from user id %s (@%s)", user.id, user.username)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("unhandled error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        # Fixed text: the exception may carry paths or URLs.
        with contextlib.suppress(TelegramError):
            await update.effective_message.reply_text("Something went wrong. The log has the details.")


async def set_menu(app: Application) -> None:
    await app.bot.set_my_commands([BotCommand("model", "Show or change the transcription model"), BotCommand("help", "How to use this bot")])


async def post_init(app: Application, *, flt: filters.BaseFilter) -> None:
    await set_menu(app)
    # Everyone-mode uses filters.ALL, which has no id list to restore.
    if isinstance(flt, filters.User):
        flt.add_user_ids(app.bot_data.get("allowed", []))


def quarantine_corrupt_pickle(path: Path) -> None:
    """Move an unreadable persistence file aside: PicklePersistence raises on it and the bot would crash-loop."""
    if not path.exists():
        return
    try:
        pickle.loads(path.read_bytes())
    except Exception:
        aside = path.with_name(path.name + ".corrupt")
        log.warning("%s is unreadable; moved to %s", path, aside, exc_info=True)
        path.replace(aside)


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    # httpx logs every request URL at INFO, and Bot API URLs carry the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    api = os.environ.get("TRANSCRIBE_API", "openrouter")
    if api not in APIS:
        raise SystemExit(f"TRANSCRIBE_API must be one of {', '.join(APIS)}, not {api!r}")
    endpoint = Endpoint(
        api=api,
        base_url=os.environ.get("TRANSCRIBE_BASE_URL") or OPENROUTER_URL,
        api_key=os.environ.get("TRANSCRIBE_API_KEY", ""),
    )
    if api == "openrouter" and not endpoint.api_key:
        raise SystemExit("TRANSCRIBE_API_KEY is empty, and OpenRouter needs one (https://openrouter.ai/keys)")
    default_model = os.environ.get("TRANSCRIBE_MODEL") or DEFAULT_MODEL
    allowed = parse_allowed_ids(os.environ.get("ALLOWED_USER_IDS", ""))
    if allowed == []:
        log.warning("ALLOWED_USER_IDS is empty: the bot answers nobody; send it a message to log your id")
    admin_raw = os.environ.get("ADMIN_USER_IDS", "")
    if admin_raw.strip() == "*":
        raise SystemExit("ADMIN_USER_IDS must be a list of user ids; * is only for ALLOWED_USER_IDS")
    admins = resolve_admins(admin_raw, allowed)

    data_dir = Path(os.environ.get("DATA_DIR", "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    state_file = data_dir / "bot.pickle"
    quarantine_corrupt_pickle(state_file)
    # bot_data holds only the /model choice and the ids added with /allow; secrets stay in `endpoint`, never on disk.
    persistence = PicklePersistence(
        state_file,
        store_data=PersistenceInput(bot_data=True, chat_data=False, user_data=False, callback_data=False),
    )
    shared = user_filter(None if allowed is None else [*allowed, *admins])
    app = (
        Application.builder()
        .token(os.environ["TELEGRAM_BOT_TOKEN"])
        .persistence(persistence)
        .post_init(partial(post_init, flt=shared))
        .concurrent_updates(4)  # a slow transcription must not block /model and /help
        .build()
    )
    app.add_error_handler(on_error)
    config = {"endpoint": endpoint, "default_model": default_model}
    admin_only = filters.User(user_id=admins)
    app.add_handler(MessageHandler(incoming_media(shared), partial(on_media, **config)))
    app.add_handler(CommandHandler("model", partial(on_model, **config), filters=admin_only))
    for name, allow in (("allow", True), ("deny", False)):
        app.add_handler(CommandHandler(name, partial(on_allow, allow=allow, flt=shared, env_ids=allowed, admins=admins), filters=admin_only))
    app.add_handler(CommandHandler("users", partial(on_users, env_ids=allowed, admins=admins), filters=admin_only))
    app.add_handler(CommandHandler(["model", "allow", "deny", "users"], on_not_admin, filters=shared))
    app.add_handler(CommandHandler(["start", "help"], on_help, filters=shared))
    app.add_handler(MessageHandler(incoming_text(shared), on_text))
    app.add_handler(MessageHandler(~shared, on_stranger))
    log.info(
        "polling; %s at %s; default model %s; %s allowed; %d admin(s)",
        api, endpoint.base_url, default_model, "everyone" if allowed is None else f"{len(allowed)} user(s)", len(admins),
    )
    # Keep retrying while Telegram is unreachable at startup (a flaky VPN or network) instead of exiting.
    app.run_polling(bootstrap_retries=-1)
