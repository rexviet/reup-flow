// The environment of the review worker (see scripts/review-worker.mjs).
import { join } from 'node:path';
import { hostEnv } from './host-voice-env.mjs';

/**
 * Host environment of the speech worker plus the settings only the review worker reads.
 * `base` is what the process inherits; `node` runs the Studio helper and `hyperframes` is the
 * pinned renderer's entry file.
 */
export function workerEnv(config, root, base, { node, hyperframes }) {
  const env = hostEnv(config, root, base);
  env.REVIEW_WORKER_DATA_ROOT = join(root, 'data/review');
  env.FINAL_RENDER_WORKERS = base.FINAL_RENDER_WORKERS ?? env.FINAL_RENDER_WORKERS ?? '2';
  env.REVIEW_WORKER_MIN_FREE_GB =
    base.REVIEW_WORKER_MIN_FREE_GB ?? env.REVIEW_WORKER_MIN_FREE_GB ?? '5';
  env.REVIEW_WORKER_NODE = node;
  env.HYPERFRAMES_BIN = hyperframes;
  env.REVIEW_WORKER_STUDIO_SCRIPT = join(root, 'scripts/review-studio-request.mjs');
  // Set in pm2's env (ecosystem.config.cjs) to split work with a Windows render machine;
  // unset, the worker does everything under the name the dashboard reads. The export root is
  // where "save to folder" writes on this Mac; unset, those requests wait. FFMPEG_BIN (an
  // absolute path) is the ffmpeg of posters and thumbnails; unset, the worker looks on PATH and
  // then in /opt/homebrew/bin and /usr/local/bin, so a bare pm2 PATH still finds it.
  for (const key of ['REVIEW_WORKER_ROLES', 'REVIEW_WORKER_NAME', 'FINAL_RENDER_GPU',
    'REVIEW_WORKER_EXPORT_ROOT', 'FFMPEG_BIN'])
    if (base[key] !== undefined) env[key] = base[key];
  return env;
}
