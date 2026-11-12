import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { openProject, launchDetached } from '../review-studio-request.mjs';
import { workerEnv } from '../lib/review-worker-env.mjs';

const deps = (active, actions) => ({
  current: async () => active,
  replace: async a => actions.push(`replace ${a.aweme}`),
  launch: async (aweme, generation, same) => actions.push(`launch ${aweme}/${generation} ${same}`),
});

test('an empty Studio just launches the project', async () => {
  const actions = [];
  await openProject('7001', '2', deps(null, actions));
  assert.deepEqual(actions, ['launch 7001/2 false']);
});

test('another project open is backed up and replaced before launching', async () => {
  const actions = [];
  await openProject('7001', '2',
    deps({ aweme: '8000', generation: '1', pagesOpened: true }, actions));
  assert.deepEqual(actions, ['replace 8000', 'launch 7001/2 false']);
});

test('the same project open is reopened without replacing it', async () => {
  const actions = [];
  await openProject('7001', '2',
    deps({ aweme: '7001', generation: '2', pagesOpened: true }, actions));
  assert.deepEqual(actions, ['launch 7001/2 true']);
});

test('a Studio that is still starting, or a failed replace, never launches', async () => {
  const actions = [];
  await assert.rejects(openProject('7001', '2',
    deps({ aweme: '8000', generation: '1', pagesOpened: false }, actions)), /still starting/);
  await assert.rejects(openProject('7001', '2', {
    current: async () => ({ aweme: '8000', generation: '1', pagesOpened: true }),
    replace: async () => { throw new Error('Studio ownership changed'); },
    launch: async () => assert.fail('must not launch over a Studio it could not stop'),
  }), /ownership changed/);
  await assert.rejects(openProject('7001; rm', '2', deps(null, actions)));
  assert.deepEqual(actions, []);
});

const launcher = () => Object.assign(new EventEmitter(), {});

test('a new launch is confirmed once the Studio record shows its pages open', async () => {
  const child = launcher();
  let polls = 0;
  await launchDetached('7001', '2', false, {
    spawnLauncher: () => child,
    wait: async () => {},
    current: async () => (++polls < 3 ? null
      : { aweme: '7001', generation: '2', pagesOpened: true }),
  });
  assert.equal(polls, 3);
});

test('a launcher that exits before opening reports failure', async () => {
  const child = launcher();
  await assert.rejects(launchDetached('7001', '2', false, {
    spawnLauncher: () => { queueMicrotask(() => child.emit('exit', 1)); return child; },
    wait: async () => {},
    current: async () => null,
  }), /exited with 1/);
});

test('reopening an already running project waits for the pages launcher to exit', async () => {
  const child = launcher();
  await launchDetached('7001', '2', true, {
    spawnLauncher: () => { queueMicrotask(() => child.emit('exit', 0)); return child; },
    wait: async () => {},
    current: async () => assert.fail('same project is confirmed by the exit code'),
  });
});

test('worker environment adds the render settings and drops nothing the host needs', () => {
  const config = { services: { 'worker-agents': { environment: {
    LOC_DATABASE_URL: 'postgres://u:p@postgres:5432/db', VOICESTUDIO_URL: 'http://x:3900',
  } }, postgres: { ports: [{ target: 5432, published: 5433 }] } } };
  const env = workerEnv(config, '/repo', { PATH: '/bin', HOME: '/h', FINAL_RENDER_WORKERS: '3' },
    { node: '/n/node', hyperframes: '/h/hyperframes.mjs' });
  assert.equal(env.REVIEW_WORKER_DATA_ROOT, '/repo/data/review');
  assert.equal(env.FINAL_RENDER_WORKERS, '3');
  assert.equal(env.REVIEW_WORKER_MIN_FREE_GB, '5');
  assert.equal(env.REVIEW_WORKER_NODE, '/n/node');
  assert.equal(env.HYPERFRAMES_BIN, '/h/hyperframes.mjs');
  assert.equal(env.REVIEW_WORKER_STUDIO_SCRIPT, '/repo/scripts/review-studio-request.mjs');
  assert.match(env.LOC_DATABASE_URL, /@127\.0\.0\.1:5433\//);
  assert.equal(env.VOICESTUDIO_URL, 'http://127.0.0.1:3900');
});
