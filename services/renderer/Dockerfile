# The render worker. Build context is the REPO ROOT: this is a pnpm workspace (the
# lockfile lives at the root) and the image also needs `contracts/` (the EditSpec schema)
# and `assets/fonts/` (the caption font).
#
# Everything that decides what a render looks like is pinned:
#  - the base image, by digest (a tag can be re-pointed upstream);
#  - Node packages, by the lockfile and `--frozen-lockfile`;
#  - the browser: Chrome Headless Shell is downloaded here, not by Remotion at run
#    time, and the build FAILS unless the file's sha256 is the one recorded below.
#
# Chromium shares memory through /dev/shm, which Docker sizes at 64 MB by default; the
# compose service raises it (`shm_size`).
FROM node:22-bookworm-slim@sha256:de170b39439eb0e5a3444ad7d60b015614a381e10fecae2e30ec9e735122f38d AS build
RUN corepack enable pnpm
WORKDIR /repo
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml tsconfig.base.json ./
COPY services/renderer/package.json services/renderer/
RUN pnpm install --frozen-lockfile
COPY services/renderer services/renderer
RUN pnpm -C services/renderer build

FROM node:22-bookworm-slim@sha256:de170b39439eb0e5a3444ad7d60b015614a381e10fecae2e30ec9e735122f38d AS runtime

# What Chrome needs to start (Remotion's list for Debian), and unzip for the browser.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl unzip libnss3 libdbus-1-3 libatk1.0-0 libasound2 libxrandr2 \
      libxkbcommon-dev libxfixes3 libxcomposite1 libxdamage1 libgbm-dev libatk-bridge2.0-0 \
      libcups2 \
 && rm -rf /var/lib/apt/lists/*

# Chrome Headless Shell, the build Remotion 4.0.535 is tested with.
ARG CHROME_VERSION=157.0.8080.0
ARG CHROME_SHA256_X64=7c4a2751b361aeb3cff3ed788fa18b7a0849371b983d478bb9205b6cc25a8c52
ARG CHROME_SHA256_ARM64=a28653057f53d2115c42445ff907f803b2ab9fc1aad77e98f95cf5fa619b2080
ARG TARGETARCH
RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) pkg=linux-x64;   sum="$CHROME_SHA256_X64";   bin=chrome-headless-shell-linux64/chrome-headless-shell ;; \
      arm64) pkg=linux-arm64; sum="$CHROME_SHA256_ARM64"; bin=chrome-headless-shell-linux-arm64/headless_shell ;; \
      *) echo "no pinned Chrome for $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/chrome.zip "https://remotion.media/chromium-headless-shell-${pkg}-${CHROME_VERSION}.zip"; \
    echo "${sum}  /tmp/chrome.zip" | sha256sum -c -; \
    mkdir -p /opt/chrome && unzip -q /tmp/chrome.zip -d /opt/chrome && rm /tmp/chrome.zip; \
    ln -s "/opt/chrome/${bin}" /opt/chrome/chrome; \
    /opt/chrome/chrome --version | grep -F "$CHROME_VERSION"
ENV CHROME_EXECUTABLE=/opt/chrome/chrome

RUN corepack enable pnpm
WORKDIR /repo
ENV NODE_ENV=production
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY services/renderer/package.json services/renderer/
RUN pnpm install --frozen-lockfile --prod
COPY --from=build /repo/services/renderer/dist services/renderer/dist
# The composition is bundled from source at startup, so the source ships too.
COPY services/renderer/src services/renderer/src
COPY contracts contracts
COPY assets/fonts /assets/fonts

# Drop root: this process holds the renderer's object-store key and runs a browser.
# `node` (uid 1000) ships in the base image; Remotion bundles into its tmp dir.
RUN chown -R node:node /repo
USER node
ENV CAPTION_FONT_PATH=/assets/fonts/BeVietnamPro-SemiBold.ttf
CMD ["node", "services/renderer/dist/worker.js"]
