// The environment the host ingest worker runs with (see scripts/host-ingest-worker.mjs): the
// host speech worker's (settings read from `docker compose config`, container addresses
// replaced by loopback ones), plus the Demucs command that runs on this Mac's GPU.
import { join } from 'node:path';
import { hostEnv } from './host-voice-env.mjs';

/** Where scripts/setup-host-demucs.sh puts what the worker needs. */
export function hostDemucsDir(root) {
  return join(root, 'services/localization/.host-demucs');
}

export function ingestEnv(config, root, base = {}) {
  const env = hostEnv(config, root, base);
  env.DEMUCS_BIN = join(hostDemucsDir(root), 'demucs-host');
  return env;
}
