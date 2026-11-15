import assert from 'node:assert/strict';
import { readFileSync, realpathSync, mkdtempSync, mkdirSync, symlinkSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createServer } from 'node:net';
import { spawnSync } from 'node:child_process';
import test from 'node:test';
import { ROOT, PORT, mapProject, assertFree, cliArgs } from './host-launcher.mjs';

test('exact package, nested lock and CLI help are reproducible', () => {
  const pkg = JSON.parse(readFileSync(join(ROOT, 'package.json')));
  assert.equal(pkg.devDependencies.hyperframes, '0.8.77');
  assert.match(readFileSync(join(ROOT, 'pnpm-lock.yaml'), 'utf8'), /hyperframes@0\.8\.77/);
  const cli = cliArgs(join(ROOT, 'project'))[0];
  for (const args of [['--help'], ['preview', '--help'], ['transcribe', '--help']]) {
    const result = spawnSync(process.execPath, [cli, ...args], { encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /0\.8\.77/);
    if (args[0] === 'preview') assert.match(result.stdout, /--foreground/);
    if (args[0] === 'transcribe') assert.match(result.stdout, /--preserve-cues/);
  }
});

test('host mapping rejects traversal and symlinks and preserves spaces in argv', () => {
  const root = mkdtempSync(join(tmpdir(), 'hyperframes-spike-'));
  try {
    mkdirSync(join(root, 'project with spaces'));
    assert.equal(
      mapProject('/review/project with spaces', '/review', root),
      realpathSync(join(root, 'project with spaces')),
    );
    assert.throws(() => mapProject('/outside/project', '/review', root));
    symlinkSync(tmpdir(), join(root, 'escape'));
    assert.throws(() => mapProject('/review/escape', '/review', root));
    const args = cliArgs(join(root, 'project with spaces'));
    assert.equal(args[2], join(root, 'project with spaces'));
    assert.equal(args[4], String(PORT));
  } finally { rmSync(root, { recursive: true }); }
});

test('occupied deterministic port is refused', async () => {
  const server = createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try { await assert.rejects(assertFree(server.address().port), { code: 'EADDRINUSE' }); }
  finally { await new Promise(resolve => server.close(resolve)); }
});
