# UNTESTED: written without Docker available. Expect to adjust it on first build.
#
# One image, three variants chosen at build time:
#   EXTRAS=service                      API, and control/cpu workers
#   EXTRAS=service  WITH_BROWSER=true   browser workers (adds Chromium and its system libraries)
#   EXTRAS=service,voice                gpu workers (adds Kokoro and faster-whisper, with PyTorch)
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src

ARG EXTRAS=service
RUN pip install --no-cache-dir ".[${EXTRAS}]"

ARG WITH_BROWSER=false
RUN if [ "$WITH_BROWSER" = "true" ]; then playwright install --with-deps chromium; fi

ENTRYPOINT ["demogen"]
