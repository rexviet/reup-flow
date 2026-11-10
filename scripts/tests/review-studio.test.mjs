import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  mkdtemp,
  mkdir,
  symlink,
  rm,
  realpath,
  writeFile,
} from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  composeArgs,
  validateIdentity,
  projectPath,
  boundaryProbe,
  matches,
  acquireLock,
  launchResponse,
} from '../review-studio.mjs';

test('numeric identities, argv launch and path escape rejection', async () => {
  assert.doesNotThrow(() => validateIdentity('7001', '1'));
  for (const pair of [
    ['$(touch x)', '1'],
    ['7001', '../1'],
    ['7001', '01'],
    ['７', '1'],
  ])
    assert.throws(() => validateIdentity(...pair));
  const args = composeArgs('7001', '1');
  assert.equal(args[0], 'compose');
  assert.equal(args.at(-1), '1');
  assert.ok(!args.includes('sh'));
  const root = await realpath(
    await mkdtemp(join(tmpdir(), 'review-launcher-test-')),
  );
  try {
    await mkdir(join(root, '7001', 'g1'), { recursive: true });
    assert.equal(
      await projectPath(root, '7001', '1'),
      join(root, '7001', 'g1'),
    );
    await symlink(tmpdir(), join(root, '7001', 'g2'));
    await assert.rejects(projectPath(root, '7001', '2'), /Symlink/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('unsafe Studio server fails closed before real project is mounted', async () => {
  await assert.rejects(
    boundaryProbe(
      'http://localhost',
      async () => new Response('[]', { status: 200 }),
    ),
    /gate is open/,
  );
  const headers = [];
  await boundaryProbe('http://localhost', async (_, options) => {
    headers.push(options.headers);
    return new Response('', { status: 403 });
  });
  assert.deepEqual(headers, [
    { Host: 'evil.invalid' },
    { Origin: 'https://evil.invalid' },
  ]);
});

test('stop/reuse requires recorded PID, deterministic port and exact command match', async () => {
  const record = {
    pid: 1234,
    port: 3002,
    command: 'node pinned-cli preview /data/project',
  };
  assert.equal(await matches(record, async () => record.command), true);
  assert.equal(await matches(record, async () => 'user process'), false);
  assert.equal(
    await matches({ ...record, port: 3003 }, async () => record.command),
    false,
  );
  assert.equal(
    await matches(record, async () => {
      throw new Error('gone');
    }),
    false,
  );
});

test('stale launcher lock recovers without stopping a reused PID', async () => {
  const root = await realpath(
    await mkdtemp(join(tmpdir(), 'review-lock-test-')),
  );
  const path = join(root, 'launcher.lock');
  try {
    await writeFile(
      path,
      JSON.stringify({ pid: 4321, port: 3002, command: 'old launcher' }),
    );
    await assert.rejects(
      acquireLock(path, async () => 'old launcher'),
      /still running/,
    );
    const lock = await acquireLock(path, async () => 'unrelated reused PID');
    await lock.close();
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});


test('malformed launch response never echoes its pipe contents', () => {
  assert.throws(() => launchResponse('not-json-sensitive-pipe', '7001', '1'),
    error => error.message === 'Invalid bridge launch response');
  assert.throws(() => launchResponse('null', '7001', '1'), /Invalid bridge launch response/);
});
