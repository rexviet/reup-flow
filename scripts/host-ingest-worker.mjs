// The ingest worker (`ingest` queue) as a process on this Mac, run under pm2: it splits the voice
// from the music with Demucs on the GPU (Apple's MPS), which the media container cannot reach and
// where a CPU separation of a few minutes of video took the better part of an hour under load.
//
// Settings come from `docker compose config`, as for the host speech worker, so no secret is
// copied into a file, an ecosystem file or pm2's saved state. Demucs itself is installed by
// scripts/setup-host-demucs.sh.
import { execFile, spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import { hostDemucsDir, ingestEnv } from './lib/host-ingest-env.mjs';

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
  const demucs = join(hostDemucsDir(ROOT), 'demucs-host');
  if (!existsSync(demucs))
    throw new Error(`${demucs} is missing; run scripts/setup-host-demucs.sh first`);
  const env = ingestEnv(await composeConfig(), ROOT, process.env);
  const child = spawn(python, ['-m', 'localization.workers.run', 'ingest'], {
    cwd: join(ROOT, 'services/localization'),
    env,
    stdio: 'inherit',
  });
  // pm2 stops this process with SIGINT/SIGTERM; the worker drains its running activity on the
  // same signals, so pass them on and leave with the worker's own exit code.
  for (const signal of ['SIGINT', 'SIGTERM'])
    process.on(signal, () => child.kill(signal));
  child.on('exit', (code, signal) => process.exit(code ?? (signal ? 1 : 0)));
}

// Not guarded by "is this the entry file": under pm2 the entry is pm2's own wrapper.
main().catch(error => {
  console.error(`host ingest worker: ${error.message}`);
  process.exit(1);
});
