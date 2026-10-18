FROM python:3.13-alpine
# ffmpeg turns every input (OGG/Opus voice, m4a, wav...) into the mp3 sent to the transcription model.
# First layer: slowest to fetch and least likely to change.
RUN apk add --no-cache ffmpeg
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev
ARG GIT_SHA
ENV GIT_SHA=${GIT_SHA}
CMD ["uv", "run", "--frozen", "--no-sync", "telegram-transcribe-bot"]
