import assert from 'node:assert/strict';
import test from 'node:test';
import { hostEnv, publishedPort } from '../lib/host-voice-env.mjs';

const config = {
  services: {
    'worker-agents': {
      environment: {
        TEMPORAL_ADDRESS: 'temporal:7233',
        S3_ENDPOINT: 'http://minio:9000',
        LOC_DATABASE_URL: 'postgresql://localization:s3cr3t@postgres:5432/reup',
        VOICESTUDIO_URL: 'http://192.168.65.2:3900',
        VOICESTUDIO_ALLOW_PRIVATE_HTTP_CIDRS: '100.64.0.0/10,192.168.65.2/32',
        LANGFUSE_HOST: 'http://langfuse-web:3000',
        CAPTION_FONT_PATH: '/assets/fonts/BeVietnamPro-SemiBold.ttf',
        OPENROUTER_API_KEY: 'sk-test',
        TTS_MODEL: 'omnivoice',
        UNSET: null,
      },
    },
    temporal: { ports: [{ target: 7233, published: '7233' }] },
    minio: { ports: [{ target: 9000, published: '9000' }] },
    postgres: { ports: [{ target: 5432, published: '5433' }] },
    'langfuse-web': { ports: [{ target: 3000, published: '3100' }] },
  },
};

test('addresses that only exist inside Docker are replaced by the published loopback ones', () => {
  const env = hostEnv(config, '/repo', { PATH: '/usr/bin' });
  assert.equal(env.TEMPORAL_ADDRESS, '127.0.0.1:7233');
  assert.equal(env.S3_ENDPOINT, 'http://127.0.0.1:9000');
  assert.equal(env.LANGFUSE_HOST, 'http://127.0.0.1:3100');
  assert.equal(env.VOICESTUDIO_URL, 'http://127.0.0.1:3900');
  const db = new URL(env.LOC_DATABASE_URL);
  assert.equal(`${db.hostname}:${db.port}`, '127.0.0.1:5433');
  assert.equal(db.password, 's3cr3t');
  assert.equal(db.pathname, '/reup');
});

test('VoiceStudio over plain http to loopback is allow-listed, keeping the existing entries', () => {
  const env = hostEnv(config, '/repo');
  assert.deepEqual(env.VOICESTUDIO_ALLOW_PRIVATE_HTTP_CIDRS.split(','), [
    '100.64.0.0/10',
    '192.168.65.2/32',
    '127.0.0.1/32',
  ]);
  const again = hostEnv(
    { ...config, services: { ...config.services, 'worker-agents': { environment: { ...config.services['worker-agents'].environment, VOICESTUDIO_ALLOW_PRIVATE_HTTP_CIDRS: '127.0.0.1/32' } } } },
    '/repo',
  );
  assert.equal(again.VOICESTUDIO_ALLOW_PRIVATE_HTTP_CIDRS, '127.0.0.1/32');
});

test('the caption font is read from the checkout and the queues are marked as on the host', () => {
  const env = hostEnv(config, '/repo');
  assert.equal(env.CAPTION_FONT_PATH, '/repo/assets/fonts/BeVietnamPro-SemiBold.ttf');
  assert.equal(env.VOICE_QUEUES_ON_HOST, 'true');
});

test('the rest of the container settings pass through, null ones are dropped', () => {
  const env = hostEnv(config, '/repo', { PATH: '/opt/homebrew/bin:/usr/bin', HOME: '/Users/x' });
  assert.equal(env.OPENROUTER_API_KEY, 'sk-test');
  assert.equal(env.TTS_MODEL, 'omnivoice');
  assert.equal(env.PATH, '/opt/homebrew/bin:/usr/bin');
  assert.equal(env.HOME, '/Users/x');
  assert.equal('UNSET' in env, false);
});

test('a missing worker-agents service is an error, not an empty environment', () => {
  assert.throws(() => hostEnv({ services: {} }, '/repo'), /worker-agents/);
});

test('a port that is not published falls back to the default', () => {
  assert.equal(publishedPort({ services: { x: { ports: [] } } }, 'x', 7233, 7233), '7233');
  assert.equal(publishedPort(config, 'postgres', 5432, 1), '5433');
});
