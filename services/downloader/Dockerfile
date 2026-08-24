# Build context is the REPO ROOT, not services/downloader: this is a pnpm
# workspace, so the lockfile and the workspace manifest live at the root and
# a service-scoped context cannot see them (installs would silently drift
# from the lockfile).
FROM node:22-alpine AS build
RUN corepack enable pnpm
WORKDIR /repo
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml tsconfig.base.json ./
COPY services/downloader/package.json services/downloader/
RUN pnpm install --frozen-lockfile
COPY services/downloader services/downloader
RUN pnpm -C services/downloader build

FROM node:22-alpine AS runtime
RUN corepack enable pnpm
WORKDIR /repo
ENV NODE_ENV=production
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY services/downloader/package.json services/downloader/
RUN pnpm install --frozen-lockfile --prod
COPY --from=build /repo/services/downloader/dist services/downloader/dist

# Drop root. This process holds API_KEY and writes to a bind-mounted DATA_DIR,
# and it sits on the same network as an unauthenticated scraper -- there is no
# reason for it to be uid 0. `node` (uid 1000) ships in the base image.
RUN mkdir -p /data && chown -R node:node /data /repo
USER node

# wget is used by the compose healthcheck; alpine ships busybox wget already.
EXPOSE 3000
CMD ["node", "services/downloader/dist/main.js"]
