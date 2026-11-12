// The review worker as a process on this Mac, run under pm2: final renders of approved
// revisions, review scans, Studio open requests and the heartbeat the dashboard reads.
//
// It runs with the same compose settings as the `worker-agents` container (database and
// storage from `docker compose config`, so no secret lands in a file or in pm2's saved
// state), with the addresses that differ on the host replaced (lib/host-voice-env.mjs).
// BridgeSettings (the REVIEW_ prefix, the container's /review root) is not used here.
import { execFile, spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import { workerEnv } from './lib/review-worker-env.mjs';
import { cli } from './review-studio.mjs';

const exec = promisify(execFile);
const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');

async function composeConfig() {
  const { stdout } = await exec('docker', ['compose', 'config', '--format', 'json'], {
    cwd: ROOT,
    maxBuffer: 16 * 1024 * 1024,
  });
  return JSON.parse(stdout);
}

async function main() {
  const python = join(ROOT, 'services/localization/.venv/bin/python');
  if (!existsSync(python))
    throw new Error(`${python} is missing; run \`uv sync\` in services/localization first`);
  const env = workerEnv(await composeConfig(), ROOT, process.env, {
    node: process.execPath,
    hyperframes: await cli(),
  });
  const child = spawn(python, ['-m', 'localization.review_worker.main'], {
    cwd: join(ROOT, 'services/localization'),
    env,
    stdio: 'inherit',
  });
  // pm2 stops this process with SIGINT/SIGTERM; the worker waits for a running render
  // (up to 60 s), gives the row back and kills the renderer, so pass the signal on.
  for (const signal of ['SIGINT', 'SIGTERM'])
    process.on(signal, () => child.kill(signal));
  child.on('exit', (code, signal) => process.exit(code ?? (signal ? 1 : 0)));
}

// Not guarded by "is this the entry file": under pm2 the entry is pm2's own wrapper.
main().catch(error => {
  console.error(`review worker: ${error.message}`);
  process.exit(1);
});
