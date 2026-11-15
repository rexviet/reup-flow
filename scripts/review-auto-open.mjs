import { execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdir, readFile, writeFile, rename, unlink, cp, lstat } from 'node:fs/promises';
import { join, resolve, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { ROOT, validateIdentity, matches, acquireLock, projectPath } from './review-studio.mjs';

const exec = promisify(execFile);
const runtime = join(ROOT, 'data/review-launcher');
const statePath = join(runtime, 'auto-open.json');
const studioPath = join(runtime, 'studio.json');
const pause = ms => new Promise(done => setTimeout(done, ms));

export function readyProjects(rows, state) {
  if (!Array.isArray(rows)) throw new Error('Invalid ready-project response');
  return rows.filter(row => {
    validateIdentity(row.aweme_id, String(row.generation));
    if (!Number.isSafeInteger(row.generation) ||
        !['ready_for_review', 'needs_human'].includes(row.status))
      throw new Error('Invalid ready-project identity/status');
    return !state.opened.includes(`${row.aweme_id}/g${row.generation}`);
  });
}

async function save(state) {
  await writeFile(`${statePath}.tmp`, JSON.stringify(state), { mode: 0o600 });
  await rename(`${statePath}.tmp`, statePath);
}

export async function studio() {
  try {
    const record = JSON.parse(await readFile(studioPath, 'utf8'));
    return await matches(record) ? record : null;
  } catch (error) {
    if (error.code === 'ENOENT') return null;
    throw error;
  }
}

// Opens the next ready project only while no Studio is open. A Studio that is open is never
// replaced here: it may be the project an admin just opened from the dashboard. The project
// stays unopened (and shows as awaiting review on the dashboard) until the Studio closes.
export async function openNext(rows, state, { current, launch, persist }) {
  const pending = readyProjects(rows, state);
  if (!pending.length) return;
  const row = pending[0];
  const active = await current();
  const same = active?.aweme === row.aweme_id &&
    active?.generation === String(row.generation);
  if (active && !(same && active.pagesOpened === true)) return;
  if (!active) await launch(row);
  const identity = `${row.aweme_id}/g${row.generation}`;
  const opened = [...state.opened, identity];
  await persist({ ...state, opened });
  state.opened = opened;
}

export async function replaceStudio(active) {
  const folder = await projectPath(join(ROOT, 'data/review'), active.aweme, active.generation);
  if (active.project !== folder || !(await matches(active)))
    throw new Error('Studio ownership changed');
  const backup = join(ROOT, 'backups/review-auto-open', active.aweme,
    `g${active.generation}`, new Date().toISOString().replace(/[:.]/g, '-'));
  await cp(folder, backup, {
    recursive: true, errorOnExist: true, force: false,
    filter: async source => {
      const name = relative(folder, source);
      if (['renders', '.hyperframes/preview', '.waveform-cache', '.thumbnails']
        .some(prefix => name === prefix || name.startsWith(prefix + '/'))) return false;
      const stat = await lstat(source);
      if (stat.isSymbolicLink()) throw new Error('Symlink in review source');
      return stat.isDirectory() || /\.(html|css|json|srt)$/.test(name);
    },
  });
  // Recheck after disk I/O. A reused PID or another launcher is never stopped.
  const latest = await studio();
  if (latest?.pid !== active.pid || latest.command !== active.command ||
      !(await matches(active))) throw new Error('Studio ownership changed during backup');
  process.kill(active.pid, 'SIGTERM');
  for (let n = 0; n < 100; n++) {
    if (!(await matches(active))) return;
    await pause(100);
  }
  throw new Error('Previous Studio is still stopping');
}

export async function run() {
  await mkdir(runtime, { recursive: true, mode: 0o700 });
  const lockPath = join(runtime, 'auto-open.lock');
  const lock = await acquireLock(lockPath);
  let stopping = false, child;
  const stop = () => { stopping = true; child?.kill('SIGTERM'); };
  process.on('SIGINT', stop);
  process.on('SIGTERM', stop);
  try {
    let state;
    try { state = JSON.parse(await readFile(statePath, 'utf8')); }
    catch (error) {
      if (error.code !== 'ENOENT') throw error;
      state = { since: new Date().toISOString(), opened: [] };
      await save(state);
    }
    if (!Number.isFinite(Date.parse(state.since)) || !Array.isArray(state.opened))
      throw new Error('Invalid auto-open state; preserve it for inspection');
    console.log(`Watching new review projects since ${state.since}`);
    while (!stopping) {
      try {
        const { stdout } = await exec('docker', ['compose', 'exec', '-T',
          'localization-starter', 'python', '-m', 'localization.starter.review_ready',
          state.since], { cwd: ROOT, timeout: 20000, maxBuffer: 4 * 1024 * 1024 });
        if (stopping) break;
        await openNext(JSON.parse(stdout), state, {
          current: studio, persist: save,
          launch: async row => {
            child = spawn(process.execPath, [join(ROOT, 'scripts/review-studio.mjs'),
              'open', row.aweme_id, String(row.generation)], { cwd: ROOT, stdio: 'ignore' });
            let spawnError;
            let confirmed = false;
            child.on('error', error => { spawnError = error; });
            try {
              for (let n = 0; n < 240 && !stopping; n++) {
                if (spawnError || child.exitCode !== null || child.signalCode !== null) break;
                const active = await studio();
                if (active?.aweme === row.aweme_id &&
                    active.generation === String(row.generation) && active.pagesOpened === true) {
                  confirmed = true;
                  console.log(`Opened review ${row.aweme_id}/g${row.generation} (${row.status})`);
                  return;
                }
                await pause(250);
              }
            } finally {
              if (!confirmed && child.exitCode === null && child.signalCode === null)
                child.kill('SIGTERM');
            }
            throw new Error('Studio launch not confirmed');
          },
        });
      } catch {
        // Child errors can include credentials from service diagnostics. Never echo them.
        console.error('Auto-open retry pending; check Docker/bridge or managed Studio status.');
      }
      if (!stopping) await pause(5000);
    }
  } finally {
    stop();
    if (child && child.exitCode === null && child.signalCode === null)
      await new Promise(done => { child.once('exit', done); setTimeout(done, 10000); });
    process.removeListener('SIGINT', stop);
    process.removeListener('SIGTERM', stop);
    await lock.close();
    await unlink(lockPath);
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url))
  run().catch(() => { console.error('Review watcher stopped; inspect its local state.'); process.exitCode = 1; });
