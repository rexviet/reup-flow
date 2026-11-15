// The environment the host speech worker runs with (see scripts/host-voice-worker.mjs).
import { basename, join } from 'node:path';

const SERVICE = 'worker-agents';

/** The host port Docker publishes `target` of `service` on, or `fallback`. */
export function publishedPort(config, service, target, fallback) {
  const ports = config.services?.[service]?.ports ?? [];
  const found = ports.find(p => Number(p.target) === target && p.published);
  return found ? String(found.published) : String(fallback);
}

/**
 * The environment of the host worker. `config` is the parsed `docker compose config --format
 * json`; `base` is what the process itself inherits (PATH, HOME, ...).
 */
export function hostEnv(config, root, base = {}) {
  const compose = config.services?.[SERVICE]?.environment;
  if (!compose) throw new Error(`docker compose config has no ${SERVICE} environment`);
  const env = {
    PATH: base.PATH ?? '/usr/bin:/bin:/usr/sbin:/sbin',
    HOME: base.HOME ?? '',
    LANG: base.LANG ?? 'en_US.UTF-8',
  };
  for (const [key, value] of Object.entries(compose))
    if (value !== null && value !== undefined) env[key] = String(value);

  env.TEMPORAL_ADDRESS = `127.0.0.1:${publishedPort(config, 'temporal', 7233, 7233)}`;
  env.S3_ENDPOINT = `http://127.0.0.1:${publishedPort(config, 'minio', 9000, 9000)}`;
  env.LANGFUSE_HOST = `http://127.0.0.1:${publishedPort(config, 'langfuse-web', 3000, 3100)}`;

  if (env.LOC_DATABASE_URL) {
    const url = new URL(env.LOC_DATABASE_URL);
    url.hostname = '127.0.0.1';
    url.port = publishedPort(config, 'postgres', 5432, 5433);
    env.LOC_DATABASE_URL = url.toString();
  }

  // VoiceStudio itself: straight to loopback, which plain http may use only if allow-listed.
  const port = env.VOICESTUDIO_URL ? new URL(env.VOICESTUDIO_URL).port || '3900' : '3900';
  env.VOICESTUDIO_URL = `http://127.0.0.1:${port}`;
  const allowed = (env.VOICESTUDIO_ALLOW_PRIVATE_HTTP_CIDRS ?? '').split(',').filter(Boolean);
  if (!allowed.includes('127.0.0.1/32')) allowed.push('127.0.0.1/32');
  env.VOICESTUDIO_ALLOW_PRIVATE_HTTP_CIDRS = allowed.join(',');

  // The container sees the fonts at /assets/fonts; on the host they are in the checkout.
  if (env.CAPTION_FONT_PATH)
    env.CAPTION_FONT_PATH = join(root, 'assets/fonts', basename(env.CAPTION_FONT_PATH));

  env.VOICE_QUEUES_ON_HOST = 'true';
  env.PYTHONUNBUFFERED = '1';
  return env;
}
