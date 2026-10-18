# telegram-whisper-bot

A personal Telegram bot. Send it a voice note, an audio file or a forwarded voice; it replies with
a transcript. The language is auto-detected (Persian included). Transcripts longer than
Telegram's 4096-character limit arrive as several messages.

- Long polling, no webhook. Ships and runs like traktak and meme_bot: an image on GHCR, one
  compose service on the same host, updated by traktak's watchtower.
- Transcription runs on OpenRouter (`/api/v1/audio/transcriptions`), model set by
  `OPENROUTER_MODEL`: `elevenlabs/scribe-v2` by default, $0.0000306/s × 60 s = $0.0018 per
  minute of audio. See Model choice below. The request asks OpenRouter for
  providers that don't keep your data (`data_collection: deny`).
- Only Telegram user ids in `ALLOWED_USER_IDS` get an answer. Everyone else is ignored and their id
  is logged.
- No database and no volume: the bot keeps no state.

## Configure

1. In Telegram, open @BotFather, send `/newbot`, and copy the token it gives you.
2. Create the production env file from the template:

   ```bash
   cp .env.example .env.prod
   ```

3. Open `.env.prod` and fill in `TELEGRAM_BOT_TOKEN` and `OPENROUTER_API_KEY`.
4. Find your Telegram user id: send @userinfobot any message, it replies with your id. Put it in
   `ALLOWED_USER_IDS` (several ids: comma separated).

Every `.env*` file except `.env.example` is gitignored.

## Deploy

Push to `main`. GitHub Actions runs the tests, builds a linux/amd64 image, checks that its ffmpeg
can encode mp3, and pushes `ghcr.io/esi-abolfazl/telegram-whisper-bot:latest` to private GHCR.
traktak's watchtower (label-driven, host-wide) pulls a new `:latest` within ~5 min. No second
watchtower: `docker-compose.yml` only defines the labeled `bot` service.

The server needs only `docker-compose.yml` and `.env`. The host is already `docker login`'d to
ghcr.io with a `read:packages` token for this owner, which covers this image too.

One-time setup, from this folder (replace `user@SERVER` with the traktak host):

```bash
ssh user@SERVER 'mkdir -p ~/telegram-whisper-bot'
```

```bash
scp docker-compose.yml user@SERVER:~/telegram-whisper-bot/ && scp .env.prod user@SERVER:~/telegram-whisper-bot/.env
```

```bash
ssh user@SERVER 'cd ~/telegram-whisper-bot && docker compose up -d'
```

Then send the bot a voice note.

- Env change: edit `.env.prod`, re-scp it as `.env`, then `docker compose up -d` (env reloads
  only when compose recreates the container; `docker restart` keeps the old env).
- Force an update now: `docker compose pull && docker compose up -d`.
- Logs: `ssh user@SERVER 'cd ~/telegram-whisper-bot && docker compose logs -f bot'`. They hold
  user ids and transcript lengths, never transcript text.
- Which build runs: `docker compose exec bot printenv GIT_SHA` (the commit Actions built it from).

Only one copy may poll a token at a time: a second one fails with `409 Conflict`. Stop the server
container before you run the same token locally, or use a separate test bot for local runs.

## Run locally

Needs [uv](https://docs.astral.sh/uv/) and ffmpeg (`brew install uv ffmpeg`).

```bash
cp .env.example .env.local
```

Fill `.env.local` (a separate test bot's token is easiest, see the 409 note above), then:

```bash
uv run --env-file .env.local telegram-whisper-bot
```

## Tests

```bash
uv run python -m unittest discover -s tests
```

They cover message splitting and the allow-list filter. CI runs them before every image build.

## Model choice

Probed 2026-10-18 on two real samples through the bot's own ffmpeg step and request body: a
39 s Persian voice note (scored against a hand-corrected transcript) and a 2:50 song with
orchestral music (scored on its best-matching sung stanza).

| Model | Persian WER | Song WER | Verdict |
|---|---|---|---|
| `elevenlabs/scribe-v2` | 6.0% | 23% | **default**: the only one that reads sung lyrics |
| `openai/whisper-large-v3` | 3.6% | 94% | best Persian, but a song comes out as "Thank you." |
| `google/gemini-3.5-transcribe` | 3.6% | 100% | best Persian, returns nothing for a song |
| `openai/gpt-4o-transcribe` | 12.0% | 56% | worse on both |

scribe-v2 transcribes verbatim, so Persian replies keep fillers (`ااا`) and audio tags such as
`[مکث]` or `[dramatic music]`; most of its Persian WER is those. For voice notes only, switch
`OPENROUTER_MODEL` to `openai/whisper-large-v3`.

## Limits

- The Bot API lets a bot download files up to 20 MB; bigger files get a short refusal.
- Each file is one transcription request, and OpenRouter's providers time out after 60 s. A recording of
  tens of minutes may fail; splitting it with ffmpeg would lift that.
