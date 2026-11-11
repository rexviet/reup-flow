// The speech worker (`stt` and `tts` queues) as a process on this Mac, run under pm2.
//
// VoiceStudio listens on 127.0.0.1 of the host. From a container the only way to it is Docker
// Desktop's host gateway, which drops a large share of new connections, so the activities that
// call it run here and reach it directly. Everything else the worker needs comes from the same
// settings the `worker-agents` container gets: they are read from `docker compose config`, so
// no secret is copied into a file, an ecosystem file or pm2's saved state, and the two workers
// cannot drift apart. Only the addresses that differ on the host are replaced
// (see lib/host-voice-env.mjs).
import { execFile, spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import { hostEnv } from './lib/host-voice-env.mjs';

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
  const env = hostEnv(await composeConfig(), ROOT, process.env);
  const child = spawn(python, ['-m', 'localization.workers.run', 'voice'], {
    cwd: join(ROOT, 'services/localization'),
    env,
    stdio: 'inherit',
  });
  // pm2 stops this process with SIGINT/SIGTERM; the worker drains its running activities
  // on the same signals, so pass them on and leave with the worker's own exit code.
  for (const signal of ['SIGINT', 'SIGTERM'])
    process.on(signal, () => child.kill(signal));
  child.on('exit', (code, signal) => process.exit(code ?? (signal ? 1 : 0)));
}

// Not guarded by "is this the entry file": under pm2 the entry is pm2's own wrapper.
main().catch(error => {
  console.error(`host voice worker: ${error.message}`);
  process.exit(1);
});
