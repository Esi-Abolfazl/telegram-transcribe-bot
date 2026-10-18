"""Telegram bot that answers a voice note or audio file with its Whisper transcript."""

import asyncio
import base64
import logging
import os
import tempfile
from pathlib import Path

import httpx
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, ContextTypes, MessageHandler, filters

# Counted by Telegram in UTF-16 units; len() matches for Persian and Latin text, emoji count double.
TELEGRAM_LIMIT = 4096
# The Bot API refuses getFile above this size.
DOWNLOAD_LIMIT = 20 * 1024 * 1024
TRANSCRIBE_URL = "https://openrouter.ai/api/v1/audio/transcriptions"

log = logging.getLogger("telegram_whisper_bot")


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


async def to_mp3(audio: bytes) -> bytes:
    # One known format for every input (OGG/Opus, m4a, wav...); Whisper resamples to 16 kHz mono anyway.
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


async def transcribe(audio: bytes, api_key: str, model: str) -> str:
    # shortcut: one request per file; providers time out after 60 s, so a very long recording
    # (tens of minutes) can fail. Split it into chunks with ffmpeg if that ever happens.
    body = {
        "model": model,
        "input_audio": {"data": base64.b64encode(await to_mp3(audio)).decode(), "format": "mp3"},
        "provider": {"data_collection": "deny"},
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(TRANSCRIBE_URL, json=body, headers={"Authorization": f"Bearer {api_key}"})
    if resp.status_code != 200:
        raise RuntimeError(f"OpenRouter {resp.status_code}: {resp.text[:500]}")
    return resp.json()["text"]


async def on_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    media = msg.voice or msg.audio or msg.document
    if media.file_size and media.file_size > DOWNLOAD_LIMIT:
        await msg.reply_text("Telegram lets bots download files up to 20 MB only.")
        return
    await msg.reply_chat_action(ChatAction.TYPING)
    try:
        audio = bytes(await (await media.get_file()).download_as_bytearray())
        text = await transcribe(audio, context.bot_data["api_key"], context.bot_data["model"])
    except Exception:
        log.exception("transcription failed for message %s", msg.message_id)
        await msg.reply_text("Transcription failed. The bot log has the reason.")
        return
    chunks = split_message(text) or ["(no speech found)"]
    for chunk in chunks:
        await msg.reply_text(chunk)
    # Length only: transcripts stay out of the logs.
    log.info("message %s: %s -> %d chars in %d reply(s)", msg.message_id, type(media).__name__, len(text), len(chunks))


async def on_stranger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user:
        log.info("ignored a message from user id %s (@%s)", user.id, user.username)


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    # httpx logs every request URL at INFO, and Bot API URLs carry the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    allowed = parse_allowed_ids(os.environ.get("ALLOWED_USER_IDS", ""))
    if not allowed:
        log.warning("ALLOWED_USER_IDS is empty: the bot answers nobody; send it a message to log your id")

    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.bot_data["api_key"] = os.environ["OPENROUTER_API_KEY"]
    app.bot_data["model"] = os.environ["OPENROUTER_MODEL"]
    app.add_handler(MessageHandler(transcribable(allowed), on_audio))
    app.add_handler(MessageHandler(~filters.User(user_id=allowed), on_stranger))
    log.info("polling; model %s; %d allowed user(s)", app.bot_data["model"], len(allowed))
    app.run_polling()
