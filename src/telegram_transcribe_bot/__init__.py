"""Telegram bot that answers a voice note or audio file with its transcript."""

import asyncio
import base64
import html
import logging
import os
import tempfile
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import httpx
from telegram import BotCommand, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PersistenceInput,
    PicklePersistence,
    filters,
)

# Counted by Telegram in UTF-16 units; len() matches for Persian and Latin text, emoji count double.
TELEGRAM_LIMIT = 4096
# The Bot API refuses getFile above this size.
DOWNLOAD_LIMIT = 20 * 1024 * 1024
APIS = ("openrouter", "openai")
OPENROUTER_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "elevenlabs/scribe-v2"
HELP = (
    "Send me a voice note, an audio file or a forwarded voice and I reply with its transcript.\n\n"
    "/model shows the transcription model and the ones you can pick.\n"
    "/model <id> switches to another model.\n"
    "/model reset goes back to the model set in .env."
)

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


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split text into Telegram-sized chunks, breaking at the last newline or space."""
    chunks = []
    text = text.strip()
    while len(text) > limit:
        cut = max(text.rfind("\n", 0, limit + 1), text.rfind(" ", 0, limit + 1))
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


def parse_allowed_ids(raw: str) -> list[int]:
    return [int(part) for part in raw.replace(",", " ").split()]


def transcribable(allowed_ids: list[int]) -> filters.BaseFilter:
    """Voice notes, audio files and audio sent as a document, from allowed users only."""
    media = filters.VOICE | filters.AUDIO | filters.Document.AUDIO
    return media & filters.User(user_id=allowed_ids)


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


async def to_mp3(audio: bytes) -> bytes:
    # One known format for every input (OGG/Opus, m4a, wav...); speech models resample to 16 kHz mono anyway.
    # Temp files, not pipes: an m4a with its index at the end needs a seekable input.
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp, "in"), Path(tmp, "out.mp3")
        src.write_bytes(audio)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(src),
            "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(dst),
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {err.decode(errors='replace').strip()}")
        return dst.read_bytes()


async def transcribe(audio: bytes, endpoint: Endpoint, model: str) -> str:
    # shortcut: one request per file; OpenRouter's providers time out after 60 s, so a very long
    # recording (tens of minutes) can fail. Split it into chunks with ffmpeg if that ever happens.
    mp3 = await to_mp3(audio)
    url = f"{endpoint.base_url.rstrip('/')}/audio/transcriptions"
    async with httpx.AsyncClient(timeout=300) as client:
        if endpoint.api == "openrouter":
            # Only the JSON shape carries OpenRouter's data policy: skip providers that keep audio.
            body = {
                "model": model,
                "input_audio": {"data": base64.b64encode(mp3).decode(), "format": "mp3"},
                "provider": {"data_collection": "deny"},
            }
            resp = await client.post(url, json=body, headers=endpoint.headers())
        else:
            files = {"file": ("audio.mp3", mp3, "audio/mpeg")}
            resp = await client.post(url, data={"model": model}, files=files, headers=endpoint.headers())
    if resp.status_code != 200:
        raise RuntimeError(f"{endpoint.api} {resp.status_code}: {resp.text[:500]}")
    return resp.json()["text"]


async def list_models(endpoint: Endpoint) -> list[str] | None:
    """Transcription model ids the endpoint offers, or None when it can't say."""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            # OpenRouter filters by this; OpenAI-compatible servers ignore it.
            resp = await client.get(
                f"{endpoint.base_url.rstrip('/')}/models",
                params={"output_modalities": "transcription"},
                headers=endpoint.headers(),
            )
        resp.raise_for_status()
        return sorted(m["id"] for m in resp.json()["data"])
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        log.warning("could not list models at %s", endpoint.base_url, exc_info=True)
        return None


async def on_audio(update: Update, context: ContextTypes.DEFAULT_TYPE, *, endpoint: Endpoint, default_model: str) -> None:
    msg = update.effective_message
    media = msg.voice or msg.audio or msg.document
    if media.file_size and media.file_size > DOWNLOAD_LIMIT:
        await msg.reply_text("Telegram lets bots download files up to 20 MB only.")
        return
    await msg.reply_chat_action(ChatAction.TYPING)
    model = current_model(context.bot_data, default_model)
    try:
        audio = bytes(await (await media.get_file()).download_as_bytearray())
        text = await transcribe(audio, endpoint, model)
    except Exception as exc:
        log.exception("transcription failed for message %s", msg.message_id)
        # Only allowed users get here, and the message carries no secret.
        await msg.reply_text(f"Transcription failed with {model}:\n{exc}"[:TELEGRAM_LIMIT])
        return
    chunks = split_message(text) or ["(no speech found)"]
    for chunk in chunks:
        await msg.reply_text(chunk)
    # Length only: transcripts stay out of the logs.
    log.info("message %s: %s via %s -> %d chars in %d reply(s)", msg.message_id, type(media).__name__, model, len(text), len(chunks))


async def on_model(update: Update, context: ContextTypes.DEFAULT_TYPE, *, endpoint: Endpoint, default_model: str) -> None:
    msg = update.effective_message
    if not context.args:
        card = models_html(
            current_model(context.bot_data, default_model), "model" not in context.bot_data, await list_models(endpoint)
        )
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
    if not accepts_model(requested, await list_models(endpoint)):
        await msg.reply_html(
            f"<code>{html.escape(requested)}</code> is not a transcription model this endpoint offers. /model lists them."
        )
        return
    context.bot_data["model"] = requested
    # Save now, not at the next 60 s flush: a crash or SIGKILL in between would drop the choice.
    await context.application.update_persistence()
    log.info("model set to %s by user %s", requested, update.effective_user.id)
    await msg.reply_html(f"Model: <code>{html.escape(requested)}</code>. Send a voice to try it.")


async def on_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP)


async def on_stranger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user:
        log.info("ignored a message from user id %s (@%s)", user.id, user.username)


async def set_menu(app: Application) -> None:
    await app.bot.set_my_commands([BotCommand("model", "Show or change the transcription model"), BotCommand("help", "How to use this bot")])


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
    if not allowed:
        log.warning("ALLOWED_USER_IDS is empty: the bot answers nobody; send it a message to log your id")

    data_dir = Path(os.environ.get("DATA_DIR", "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    # bot_data holds only the /model choice; secrets stay in `endpoint`, never on disk.
    persistence = PicklePersistence(
        data_dir / "bot.pickle",
        store_data=PersistenceInput(bot_data=True, chat_data=False, user_data=False, callback_data=False),
    )
    app = (
        Application.builder()
        .token(os.environ["TELEGRAM_BOT_TOKEN"])
        .persistence(persistence)
        .post_init(set_menu)
        .build()
    )
    is_allowed = filters.User(user_id=allowed)
    config = {"endpoint": endpoint, "default_model": default_model}
    app.add_handler(MessageHandler(transcribable(allowed), partial(on_audio, **config)))
    app.add_handler(CommandHandler("model", partial(on_model, **config), filters=is_allowed))
    app.add_handler(CommandHandler(["start", "help"], on_help, filters=is_allowed))
    app.add_handler(MessageHandler(~is_allowed, on_stranger))
    log.info("polling; %s at %s; default model %s; %d allowed user(s)", api, endpoint.base_url, default_model, len(allowed))
    app.run_polling()
