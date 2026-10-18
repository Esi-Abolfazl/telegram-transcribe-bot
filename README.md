# telegram-whisper-bot

A self-hosted Telegram bot that turns voice into text. Send it a voice note, an audio file or a
forwarded voice; it replies with the transcript. The language is auto-detected, Persian included.
Transcripts longer than Telegram's 4096-character limit arrive as several messages.

- **Any model.** OpenRouter's 25+ transcription models (Whisper, ElevenLabs Scribe, GPT-4o
  Transcribe, Gemini...), any OpenAI-compatible API, or a local Whisper on your own machine.
- **Switch from Telegram.** `/model` lists the models your endpoint offers, `/model <id>` switches,
  `/model reset` goes back to the one in `.env`. The choice survives restarts.
- **Private by default.** Only Telegram user ids in `ALLOWED_USER_IDS` get an answer; anyone else
  is ignored and their id is logged. On OpenRouter, requests ask for providers that don't keep
  your audio (`data_collection: deny`). Logs hold user ids and transcript lengths, never text.
- Long polling, no webhook, no open port: it runs anywhere Docker runs, a laptop included.

## Quick start (Docker)

You need Docker with Compose, a Telegram account, and an [OpenRouter key](https://openrouter.ai/keys)
(or a local model, see below).

1. In Telegram, open @BotFather, send `/newbot`, and copy the token it gives you.
2. Send @userinfobot any message; it replies with your Telegram user id.
3. Get the compose file and the env template into a new folder:

   ```bash
   mkdir telegram-whisper-bot && cd telegram-whisper-bot
   ```

   ```bash
   curl -fsSLO https://raw.githubusercontent.com/Esi-Abolfazl/telegram-whisper-bot/main/docker-compose.yml
   ```

   ```bash
   curl -fsSL https://raw.githubusercontent.com/Esi-Abolfazl/telegram-whisper-bot/main/.env.example -o .env
   ```

4. Open `.env` and fill in `TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_IDS` and `TRANSCRIBE_API_KEY`.
5. Start it:

   ```bash
   docker compose up -d
   ```

6. Send your bot a voice note.

Logs: `docker compose logs -f bot`. After you edit `.env`, run `docker compose up -d` again: only a
recreated container reads the new values (`docker restart` keeps the old ones).

Only one copy may poll a bot token at a time; a second one fails with `409 Conflict`.

## Configuration

All settings live in `.env` ([`.env.example`](.env.example) explains each).

| Variable | Default | What it does |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | required | From @BotFather |
| `ALLOWED_USER_IDS` | empty (nobody) | Who gets answers, comma separated |
| `TRANSCRIBE_API` | `openrouter` | `openrouter` (JSON API) or `openai` (OpenAI multipart API, local servers) |
| `TRANSCRIBE_BASE_URL` | `https://openrouter.ai/api/v1` | The API root; `/audio/transcriptions` is appended |
| `TRANSCRIBE_API_KEY` | empty | Required for OpenRouter; empty for a local server without auth |
| `TRANSCRIBE_MODEL` | `elevenlabs/scribe-v2` | The default model; `/model` overrides it |
| `DATA_DIR` | `data` | Where the `/model` choice is saved |

Every allowed user may run `/model`, and the choice applies to everyone on that bot.

## Changing the model

From Telegram:

- `/model` shows the current model and every transcription model the endpoint lists.
- `/model openai/whisper-large-v3` switches. An id the endpoint doesn't list is refused.
- `/model reset` returns to `TRANSCRIBE_MODEL`.

From `.env`: set `TRANSCRIBE_MODEL`, then `docker compose up -d`. A `/model` choice still wins
until `/model reset`.

### Which model

Measured 2026-10-18 on a 39 s Persian voice note (against a hand-corrected transcript) and a
2:50 song with orchestral music (against its lyrics), through the bot's own pipeline:

| Model | Persian WER | Song WER | $ per minute of audio | Verdict |
|---|---|---|---|---|
| `elevenlabs/scribe-v2` | 6.0% | 23% | $0.0000306/s × 60 s = $0.0018 | **default**: the only one that reads sung lyrics |
| `openai/whisper-large-v3` | 3.6% | 94% | $0.0000075/s × 60 s = $0.00045 | best on speech, a song comes out as "Thank you." |
| `google/gemini-3.5-transcribe` | 3.6% | 100% | token-priced | best on speech, returns nothing for a song |
| `openai/gpt-4o-transcribe` | 12.0% | 56% | token-priced | worse on both |

scribe-v2 transcribes verbatim, so replies keep fillers (`ااا`, "um") and audio tags such as
`[dramatic music]`. For speech only, `openai/whisper-large-v3` is cleaner and cheaper.

## Local model

Audio never leaves your machine and costs nothing per minute. `docker-compose.yml` ships an
optional [whisper-server](https://github.com/hwdsl2/docker-whisper) (faster-whisper, CPU,
amd64 and arm64) behind the `local` profile.

1. In `.env`:

   ```
   TRANSCRIBE_API=openai
   TRANSCRIBE_BASE_URL=http://whisper:9000/v1
   TRANSCRIBE_API_KEY=
   ```

2. Start both services:

   ```bash
   docker compose --profile local up -d
   ```

The first start downloads the model (`large-v3-turbo`, about 1.6 GB, kept in the `whisper-data`
volume). To trade quality for speed and memory, change `WHISPER_MODEL` in `docker-compose.yml` to
`small` or `base`; `base` is fast but weak on Persian. The server serves one model at a time, so
`/model` shows that one and switching means changing `WHISPER_MODEL`.

Any other server that speaks the OpenAI transcription API works the same way: set
`TRANSCRIBE_API=openai` and point `TRANSCRIBE_BASE_URL` at its `/v1`. OpenAI itself is
`https://api.openai.com/v1` with an OpenAI key.

## Run from source

Needs [uv](https://docs.astral.sh/uv/) and ffmpeg (`brew install uv ffmpeg` on macOS).

```bash
cp .env.example .env
```

Fill `.env`, then:

```bash
uv run --env-file .env telegram-whisper-bot
```

Tests (message splitting, the allow-list, the model choice):

```bash
uv run python -m unittest discover -s tests
```

## How it ships

Every push to `main` runs the tests, builds a linux/amd64 image, checks that its ffmpeg can encode
mp3, and pushes `ghcr.io/esi-abolfazl/telegram-whisper-bot:latest` plus a tag per commit. A
watchtower started with `--label-enable` on your host picks up new images on its own, since the
bot service carries its label. `docker compose exec bot printenv GIT_SHA` names the commit a
running image was built from.

## Limits

- The Bot API lets a bot download files up to 20 MB; bigger files get a short refusal.
- Each file is one transcription request, and OpenRouter's providers time out after 60 s. A
  recording of tens of minutes may fail; splitting it with ffmpeg would lift that.
- `/model` lists what the endpoint's `/models` returns. OpenRouter and local servers return
  transcription models only; OpenAI's own API returns every model it has, chat ones included, and
  picking one of those fails at the first voice with the API's error.

## License

[MIT](LICENSE)
