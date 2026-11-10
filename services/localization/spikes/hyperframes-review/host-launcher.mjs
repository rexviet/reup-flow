import { createRequire } from 'node:module';
import { realpathSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, resolve, isAbsolute, relative } from 'node:path';
import { spawn } from 'node:child_process';
import { createServer } from 'node:net';

export const PORT = 18440;
export const ROOT = dirname(fileURLToPath(import.meta.url));

export function mapProject(containerPath, containerRoot, hostRoot) {
  const suffix = relative(containerRoot, containerPath);
  if (!isAbsolute(containerPath) || suffix.startsWith('..') || isAbsolute(suffix)) {
    throw new Error('Project is outside bind mount');
  }
  const mapped = realpathSync(resolve(hostRoot, suffix));
  const boundary = realpathSync(hostRoot);
  const local = relative(boundary, mapped);
  if (local.startsWith('..') || isAbsolute(local)) throw new Error('Symlink outside bind mount');
  return mapped;
}

export async function assertFree(port) {
  const probe = createServer();
  await new Promise((accept, reject) => {
    probe.once('error', reject);
    probe.listen(port, '127.0.0.1', accept);
  });
  await new Promise((accept, reject) => probe.close(error => error ? reject(error) : accept()));
}

export function cliArgs(project) {
  const require = createRequire(import.meta.url);
  const pkg = require.resolve('hyperframes/package.json');
  const installed = JSON.parse(readFileSync(pkg, 'utf8')).version;
  if (installed !== '0.8.77') throw new Error(`Expected 0.8.77; installed ${installed}`);
  return [join(dirname(pkg), 'bin/hyperframes.mjs'), 'preview', project,
    '--port', String(PORT), '--foreground', '--no-open'];
}

export async function launch() {
  const project = mapProject('/review/project', '/review', ROOT);
  await assertFree(PORT);
  // Never shell-expand a project path or ask a container to spawn a host process.
  const child = spawn(process.execPath, cliArgs(project), {
    cwd: ROOT, stdio: 'inherit', env: { ...process.env, DO_NOT_TRACK: '1' },
  });
  console.log(JSON.stringify({ pid: child.pid, port: PORT, project, cwd: ROOT }));
  const stop = () => child.kill('SIGTERM');
  process.once('SIGINT', stop);
  process.once('SIGTERM', stop);
  child.once('exit', code => {
    process.removeListener('SIGINT', stop);
    process.removeListener('SIGTERM', stop);
    process.exitCode = code ?? 1;
  });
  child.once('error', error => { console.error(error.message); process.exitCode = 1; });
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  launch().catch(error => { console.error(error.message); process.exitCode = 1; });
}
