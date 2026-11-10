import { spawn, execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createRequire } from 'node:module';
import { createServer } from 'node:net';
import { request as httpRequest } from 'node:http';
import {
  mkdir,
  realpath,
  readFile,
  writeFile,
  unlink,
  mkdtemp,
  rm,
  lstat,
  open,
} from 'node:fs/promises';
import { dirname, join, resolve, relative, isAbsolute } from 'node:path';
import { fileURLToPath } from 'node:url';
import { tmpdir } from 'node:os';

const exec = promisify(execFile);
export const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
export const PORT = 3002;
const runtime = join(ROOT, 'data', 'review-launcher');
const recordPath = join(runtime, 'studio.json');
const delay = (ms) => new Promise((done) => setTimeout(done, ms));

export function validateIdentity(aweme, generation) {
  if (
    !/^[0-9]{1,32}$/.test(aweme) ||
    !/^(0|[1-9][0-9]*)$/.test(generation) ||
    !Number.isSafeInteger(Number(generation))
  )
    throw new Error('Expected numeric aweme and generation');
}

export async function projectPath(root, aweme, generation) {
  validateIdentity(aweme, generation);
  const folder = join(root, aweme, `g${generation}`);
  for (let path = folder; path !== dirname(path); path = dirname(path)) {
    if ((await lstat(path)).isSymbolicLink())
      throw new Error('Symlink in project path');
  }
  const boundary = await realpath(root),
    actual = await realpath(folder);
  const suffix = relative(boundary, actual);
  if (suffix.startsWith('..') || isAbsolute(suffix))
    throw new Error('Project path escaped review root');
  return actual;
}

export function composeArgs(aweme, generation) {
  validateIdentity(aweme, generation);
  return [
    'compose',
    'exec',
    '-T',
    'review-bridge',
    'python',
    '-m',
    'localization.review_bridge.cli',
    'launch',
    aweme,
    generation,
  ];
}

export async function assertFree(port) {
  const server = createServer();
  try {
    await new Promise((accept, reject) => {
      server.once('error', reject);
      server.listen(port, '127.0.0.1', accept);
    });
  } catch (error) {
    const owner = await exec('lsof', [
      '-nP',
      `-iTCP:${port}`,
      '-sTCP:LISTEN',
    ]).catch(() => ({ stdout: 'Unknown owner' }));
    throw new Error(
      `Port ${port} is occupied. Stop its owner explicitly.\n${owner.stdout}`,
      { cause: error },
    );
  }
  await new Promise((accept, reject) =>
    server.close((error) => (error ? reject(error) : accept())),
  );
}

function probeRequest(url, { headers, signal }) {
  return new Promise((accept, reject) => {
    const request = httpRequest(url, { headers, signal }, response => {
      response.resume();
      accept({ status: response.statusCode, arrayBuffer: async () => new ArrayBuffer(0) });
    });
    request.on('error', reject);
    request.end();
  });
}

export async function boundaryProbe(base, fetcher = probeRequest) {
  for (const headers of [
    { Host: 'evil.invalid' },
    { Origin: 'https://evil.invalid' },
  ]) {
    const response = await fetcher(`${base}/api/projects`, {
      headers,
      signal: AbortSignal.timeout(3000),
    });
    await response.arrayBuffer();
    if (![400, 403, 421].includes(response.status)) {
      throw new Error(
        'Studio Host/Origin gate is open; production review data was not mounted. See docs/hyperframes-review-spike.md.',
      );
    }
  }
}

async function cli() {
  const require = createRequire(
    join(ROOT, 'services/localization/spikes/hyperframes-review/package.json'),
  );
  const packagePath = require.resolve('hyperframes/package.json');
  if (JSON.parse(await readFile(packagePath, 'utf8')).version !== '0.8.77')
    throw new Error('Install the locked hyperframes@0.8.77 spike package');
  return join(dirname(packagePath), 'bin/hyperframes.mjs');
}

function start(binary, folder) {
  const args = [
    '--import',
    join(ROOT, 'scripts/studio-boundary.mjs'),
    binary,
    'preview',
    folder,
    '--port',
    String(PORT),
    '--foreground',
    '--no-open',
  ];
  // Do not pass the application's provider/S3/DB credentials into Studio.
  const env = Object.fromEntries(
    ['PATH', 'HOME', 'TMPDIR', 'LANG']
      .filter((key) => process.env[key])
      .map((key) => [key, process.env[key]]),
  );
  env.DO_NOT_TRACK = '1';
  const child = spawn(process.execPath, args, {
    cwd: ROOT,
    stdio: 'ignore',
    env,
  });
  child.on('error', () => {});
  return child;
}

async function ready(child) {
  for (let attempt = 0; attempt < 60; attempt++) {
    if (child.exitCode !== null || child.signalCode !== null)
      throw new Error('Studio exited before becoming ready');
    try {
      const response = await fetch(`http://127.0.0.1:${PORT}/api/projects`, {
        signal: AbortSignal.timeout(500),
      });
      await response.arrayBuffer();
      if (response.ok) return;
    } catch {
      /* bounded startup polling */
    }
    await delay(250);
  }
  throw new Error('Studio did not become ready');
}

async function assertLoopbackListener(child) {
  const { stdout } = await exec('lsof', ['-nP', '-a', '-p', String(child.pid),
    `-iTCP:${PORT}`, '-sTCP:LISTEN']);
  const listeners = stdout.split('\n').filter(line => line.includes(' TCP '));
  if (!listeners.length || listeners.some(line => !line.includes(`127.0.0.1:${PORT} (LISTEN)`)))
    throw new Error('Studio must listen only on the managed loopback socket');
}

async function terminate(child) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  child.kill('SIGTERM');
  for (
    let n = 0;
    n < 40 && child.exitCode === null && child.signalCode === null;
    n++
  )
    await delay(100);
  if (child.exitCode === null && child.signalCode === null)
    child.kill('SIGKILL');
}

export async function matches(
  record,
  inspect = async (pid) =>
    (await exec('ps', ['-p', String(pid), '-o', 'command='])).stdout.trim(),
) {
  if (
    !Number.isSafeInteger(record.pid) ||
    record.pid <= 1 ||
    record.port !== PORT
  )
    return false;
  try {
    return (await inspect(record.pid)) === record.command;
  } catch {
    return false;
  }
}

export async function acquireLock(path, inspect) {
  let handle;
  try {
    handle = await open(path, 'wx', 0o600);
  } catch (error) {
    if (error.code !== 'EEXIST') throw error;
    if ((await lstat(path)).isSymbolicLink())
      throw new Error('Launcher lock is a symlink');
    const old = await readFile(path, 'utf8');
    let record;
    try {
      record = JSON.parse(old);
    } catch {
      throw new Error(
        'Incomplete launcher lock; inspect its owner before removing it',
      );
    }
    if (await matches(record, inspect))
      throw new Error('Another launcher is still running');
    if ((await readFile(path, 'utf8')) !== old)
      throw new Error('Launcher lock changed; retry');
    await unlink(path);
    handle = await open(path, 'wx', 0o600);
  }
  const command = (
    await exec('ps', ['-p', String(process.pid), '-o', 'command='])
  ).stdout.trim();
  await handle.writeFile(
    JSON.stringify({ pid: process.pid, port: PORT, command }),
  );
  return handle;
}

async function openPages(aweme, generation) {
  const { stdout } = await exec('docker', composeArgs(aweme, generation), {
    cwd: ROOT,
    maxBuffer: 8192,
  });
  const result = launchResponse(stdout, aweme, generation);
  await exec('open', [
    `http://127.0.0.1:${PORT}`,
    `http://127.0.0.1:3310/#${result.grant}`,
  ]);
}

export function launchResponse(stdout, aweme, generation) {
  let result;
  try {
    result = JSON.parse(stdout);
  } catch {
    throw new Error('Invalid bridge launch response');
  }
  if (
    !result ||
    typeof result !== 'object' ||
    result.aweme_id !== aweme ||
    result.generation !== Number(generation) ||
    !/^[A-Za-z0-9_-]{43}$/.test(result.grant)
  )
    throw new Error('Invalid bridge launch response');
  return result;
}

export async function main(args) {
  const [major, minor] = process.versions.node.split('.').map(Number);
  if (major < 22 || (major === 22 && minor < 13))
    throw new Error('The pinned Studio requires Node 22.13 or newer');
  await mkdir(runtime, { recursive: true, mode: 0o700 });
  if ((await lstat(runtime)).isSymbolicLink())
    throw new Error('Launcher runtime is a symlink');
  let existing;
  try {
    existing = JSON.parse(await readFile(recordPath, 'utf8'));
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  if (args[0] === 'stop' && args.length === 1) {
    if (!existing) return;
    if (!(await matches(existing)))
      throw new Error(
        'Recorded Studio PID no longer matches; refusing to stop it',
      );
    process.kill(existing.pid, 'SIGTERM');
    return;
  }
  if (args[0] !== 'open' || args.length !== 3)
    throw new Error(
      'Usage: node scripts/review-studio.mjs open <aweme> <generation> | stop',
    );
  const [, aweme, generation] = args;
  validateIdentity(aweme, generation);
  if (existing && (await matches(existing))) {
    if (existing.aweme !== aweme || existing.generation !== generation)
      throw new Error(
        'Another review project is open; stop its launcher first',
      );
    await openPages(aweme, generation);
    return;
  }
  await assertFree(PORT);
  const lockPath = join(runtime, 'launcher.lock');
  const lock = await acquireLock(lockPath);
  let child,
    scratch,
    stopping = false;
  const stop = () => {
    stopping = true;
    child?.kill('SIGTERM');
  };
  const checkStopping = () => {
    if (stopping) throw new Error('Launcher stopped');
  };
  process.on('SIGINT', stop);
  process.on('SIGTERM', stop);
  try {
    const binary = await cli();
    checkStopping();
    // Probe an empty project first. An unsafe native server never sees real review data.
    scratch = await mkdtemp(join(tmpdir(), 'review-studio-probe-'));
    await writeFile(
      join(scratch, 'index.html'),
      '<!doctype html><html><body>Boundary probe</body></html>',
    );
    child = start(binary, scratch);
    await ready(child);
    await assertLoopbackListener(child);
    await boundaryProbe(`http://127.0.0.1:${PORT}`);
    await terminate(child);
    child = null;
    checkStopping();
    await assertFree(PORT);
    // Materialize via trusted CLI, then resolve the host folder without accepting a path.
    const { stdout } = await exec('docker', composeArgs(aweme, generation), {
      cwd: ROOT,
      maxBuffer: 8192,
    });
    const { grant } = launchResponse(stdout, aweme, generation);
    const folder = await projectPath(
      join(ROOT, 'data', 'review'),
      aweme,
      generation,
    );
    checkStopping();
    child = start(binary, folder);
    await ready(child);
    await assertLoopbackListener(child);
    await boundaryProbe(`http://127.0.0.1:${PORT}`);
    const command = (
      await exec('ps', ['-p', String(child.pid), '-o', 'command='])
    ).stdout.trim();
    await writeFile(
      recordPath,
      JSON.stringify({
        pid: child.pid,
        command,
        port: PORT,
        project: folder,
        aweme,
        generation,
      }),
      { mode: 0o600 },
    );
    await exec('open', [
      `http://127.0.0.1:${PORT}`,
      `http://127.0.0.1:3310/#${grant}`,
    ]);
    console.log(
      `Review Studio on 127.0.0.1:${PORT}; PID ${child.pid}. Ctrl+C stops this Studio.`,
    );
    await new Promise((accept, reject) => {
      if (child.exitCode !== null || child.signalCode !== null) {
        if (child.exitCode && !child.signalCode)
          reject(new Error('Studio exited'));
        else accept();
        return;
      }
      child.once('exit', (code) =>
        code === 0 || child.signalCode
          ? accept()
          : reject(new Error('Studio exited')),
      );
      child.once('error', reject);
    });
  } finally {
    await terminate(child);
    process.removeListener('SIGINT', stop);
    process.removeListener('SIGTERM', stop);
    await unlink(recordPath).catch(() => {});
    await lock.close();
    await unlink(lockPath);
    if (scratch) await rm(scratch, { recursive: true, force: true });
  }
}

if (
  process.argv[1] &&
  resolve(process.argv[1]) === fileURLToPath(import.meta.url)
) {
  main(process.argv.slice(2)).catch((error) => {
    // execFile failures can embed argv including a fragment grant. Do not echo them.
    console.error(
      error.cmd
        ? 'Launcher subprocess failed; review the service status.'
        : error.message,
    );
    process.exitCode = 1;
  });
}
