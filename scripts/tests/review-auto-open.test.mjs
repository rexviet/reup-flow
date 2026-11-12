import test from 'node:test';
import assert from 'node:assert/strict';
import { openNext, readyProjects } from '../review-auto-open.mjs';

const row = { aweme_id: '7001', generation: 2, status: 'ready_for_review' };
const state = () => ({ since: '2026-11-10T14:00:00Z', opened: [] });

test('both terminal project states open once across watcher restarts', async () => {
  const saved = state(), opened = [];
  const deps = { current: async () => null, launch: async r => opened.push(r.aweme_id),
    persist: async () => {} };
  await openNext([row], saved, deps);
  await openNext([row], JSON.parse(JSON.stringify(saved)), deps);
  await openNext([{ ...row, aweme_id: '7002', status: 'needs_human' }], saved, deps);
  assert.deepEqual(opened, ['7001', '7002']);
});

test('a new completion never replaces a Studio that is open and stays pending', async () => {
  const saved = state(), actions = [];
  const active = { aweme: '8000', generation: '1', pagesOpened: true };
  await openNext([row], saved, {
    current: async () => active,
    replace: async () => actions.push('replace'),
    launch: async () => actions.push('launch'), persist: async () => actions.push('persist'),
  });
  assert.deepEqual(actions, []);
  assert.deepEqual(saved.opened, []);
});

test('the pending project opens once the Studio is gone', async () => {
  const saved = state(), actions = [];
  let active = { aweme: '8000', generation: '1', pagesOpened: true };
  const deps = {
    current: async () => active,
    launch: async r => actions.push(`launch ${r.aweme_id}`),
    persist: async () => actions.push('persist'),
  };
  await openNext([row], saved, deps);
  active = null;
  await openNext([row], saved, deps);
  assert.deepEqual(actions, ['launch 7001', 'persist']);
  assert.deepEqual(saved.opened, ['7001/g2']);
});

test('failed launch remains queued; a fully opened matching project is not reopened', async () => {
  const saved = state();
  await assert.rejects(openNext([row], saved, { current: async () => null,
    launch: async () => { throw new Error('unavailable'); }, persist: async () => {} }));
  assert.deepEqual(saved.opened, []);
  await openNext([row], saved, { current: async () => ({
    aweme: '7001', generation: '2', pagesOpened: true,
  }),
    launch: async () => assert.fail('must not reopen'), persist: async () => {} });
  assert.deepEqual(saved.opened, ['7001/g2']);
});

test('matching Studio remains queued until its pages are confirmed open', async () => {
  const saved = state();
  const deps = {
    current: async () => ({ aweme: '7001', generation: '2', pagesOpened: false }),
    launch: async () => assert.fail('must not start a second launcher'),
    persist: async () => assert.fail('must not persist an incomplete launch'),
  };

  await openNext([row], saved, deps);

  assert.deepEqual(saved.opened, []);
});

test('untrusted discovery identities and non-review states cannot reach launcher argv', () => {
  for (const change of [{ aweme_id: '$(touch x)' }, { generation: -1 },
    { generation: '2' }, { status: 'published' }])
    assert.throws(() => readyProjects([{ ...row, ...change }], state()));
});

test('failed state persistence does not suppress the project in memory', async () => {
  const saved = state();
  await assert.rejects(openNext([row], saved, {
    current: async () => ({ aweme: '7001', generation: '2', pagesOpened: true }),
    launch: async () => {}, persist: async () => { throw new Error('disk full'); },
  }));
  assert.deepEqual(saved.opened, []);
});
