import assert from 'node:assert/strict';
import test from 'node:test';
import { hostDemucsDir, ingestEnv } from '../lib/host-ingest-env.mjs';

const config = {
  services: {
    'worker-agents': {
      environment: {
        TEMPORAL_ADDRESS: 'temporal:7233',
        S3_ENDPOINT: 'http://minio:9000',
        DEMUCS_BIN: '/usr/local/bin/demucs-safe',
      },
    },
    temporal: { ports: [{ target: 7233, published: '7233' }] },
    minio: { ports: [{ target: 9000, published: '9000' }] },
  },
};

test('the worker reaches Temporal and the object store on loopback', () => {
  const env = ingestEnv(config, '/repo', { PATH: '/usr/bin' });
  assert.equal(env.TEMPORAL_ADDRESS, '127.0.0.1:7233');
  assert.equal(env.S3_ENDPOINT, 'http://127.0.0.1:9000');
});

test('Demucs is the host command set up by setup-host-demucs.sh, not the container one', () => {
  const env = ingestEnv(config, '/repo', { PATH: '/usr/bin' });
  assert.equal(env.DEMUCS_BIN, `${hostDemucsDir('/repo')}/demucs-host`);
  assert.equal(env.DEMUCS_BIN, '/repo/services/localization/.host-demucs/demucs-host');
});
