# Build context is the REPO ROOT, matching services/downloader/Dockerfile:
# this is a pnpm workspace, so the lockfile lives at the root and a
# service-scoped context cannot see it.
FROM node:22-alpine AS build
RUN corepack enable pnpm
WORKDIR /repo
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY services/web/package.json services/web/
RUN pnpm install --frozen-lockfile
COPY services/web services/web

# Vite inlines `import.meta.env.VITE_*` into the built JS at BUILD time, not
# at container start -- unlike services/downloader's runtime `process.env`
# reads. That is why these arrive as build ARGs baked into the image here,
# not as compose `environment:` entries read at container start: an
# `environment:` value would have zero effect on an already-built static
# bundle. See this package's README "Deploy" section before changing this.
ARG VITE_API_BASE_URL
ARG VITE_API_KEY
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
ENV VITE_API_KEY=${VITE_API_KEY}
RUN pnpm -C services/web build

FROM nginx:1.27-alpine AS runtime
COPY services/web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /repo/services/web/dist /usr/share/nginx/html
EXPOSE 80
