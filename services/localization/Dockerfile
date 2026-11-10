# Image for the localization workers and the outbox starter. One image, worker and companion
# commands (see docker-compose.yml): the role is chosen by the command.
#
# Build context: services/localization.
#
# Pinned by digest, because a tag can be re-pointed upstream. The same applies to
# the model weights: they are downloaded at build time and the build FAILS unless
# the file's sha256 matches the one recorded here, so a swapped upstream file
# never reaches a running container. Demucs then loads them through
# `demucs_safe`, which forces `torch.load(weights_only=True)`.
FROM docker.io/astral/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app/src

WORKDIR /app

# The service's own dependencies, exactly as locked.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Demucs and the CPU build of torch it runs on. demucs declares an old torchaudio
# ceiling that predates the torch version that has `add_safe_globals`, so that one
# bound is overridden; nothing else is.
RUN printf 'torchaudio==2.5.1\n' > /tmp/overrides.txt \
 && uv pip install --python /opt/venv/bin/python --override /tmp/overrides.txt \
      --extra-index-url https://download.pytorch.org/whl/cpu \
      torch==2.5.1 torchaudio==2.5.1 demucs==4.0.1 \
 && rm /tmp/overrides.txt

# htdemucs weights. The file name carries the first 8 hex digits of its sha256,
# which is what demucs itself checks; the full digest is checked here.
ARG DEMUCS_WEIGHTS_URL=https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/955717e8-8726e21a.th
ARG DEMUCS_WEIGHTS_SHA256=8726e21a993978c7ba086d3872e7608d7d5bfca646ca4aca459ffda844faa8b4
ENV TORCH_HOME=/opt/torch
RUN mkdir -p /opt/torch/hub/checkpoints \
 && curl -fsSL -o /opt/torch/hub/checkpoints/955717e8-8726e21a.th "$DEMUCS_WEIGHTS_URL" \
 && echo "$DEMUCS_WEIGHTS_SHA256  /opt/torch/hub/checkpoints/955717e8-8726e21a.th" | sha256sum -c -

COPY src ./src
RUN printf '#!/bin/sh\nexec /opt/venv/bin/python -m localization.providers.demucs_safe "$@"\n' \
      > /usr/local/bin/demucs-safe \
 && chmod +x /usr/local/bin/demucs-safe
ENV DEMUCS_BIN=/usr/local/bin/demucs-safe

RUN useradd --system --uid 10001 --home-dir /home/loc --create-home loc
USER loc
