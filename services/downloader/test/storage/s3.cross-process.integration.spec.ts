/**
 * Two PROCESSES, one bucket.
 *
 * The in-flight registry lives in memory, so a sweep in process B cannot see an
 * upload that process A is writing. Only the age threshold protects it, and
 * that is what this proves: A uploads slowly; B sweeps with a lease-sized
 * cutoff at a clock that makes A's upload look ten minutes old; A must still
 * be able to complete and the object must be whole.
 *
 * The clock is injected into B only. Waiting out a real 24 hours -- or even a
 * real 5 minute lease -- is not a test anyone would run.
 */
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { once } from 'node:events';
import { join } from 'node:path';
import type { S3Client } from '@aws-sdk/client-s3';
import { S3Storage } from '../../src/storage/s3.storage';
import {
  getObjectBytes,
  listUploads,
  makeS3Client,
  readS3TestEnv,
  uniqueTestPrefix,
  wipePrefix,
} from '../support/s3-test-env';

const MIB = 1024 * 1024;
const HEAD = 6 * MIB;
const TAIL = 1234;
const LEASE_MS = 5 * 60 * 1000;
const KEY = 'douyin/two-process/7372484719365099001.mp4';

const env = readS3TestEnv();

describe('S3Storage across two processes', () => {
  let client: S3Client;
  let prefix: string;
  let child: ChildProcessWithoutNullStreams | undefined;

  beforeAll(() => {
    client = makeS3Client(env);
  });

  afterAll(() => {
    client.destroy();
  });

  beforeEach(() => {
    prefix = uniqueTestPrefix('two-process');
  });

  afterEach(async () => {
    if (child && child.exitCode === null && child.signalCode === null) {
      child.kill('SIGKILL');
      await once(child, 'exit');
    }
    child = undefined;
    await wipePrefix(client, env.bucket, prefix);
  });

  function spawnSlowUploader(): ChildProcessWithoutNullStreams {
    return spawn(
      process.execPath,
      ['-r', 'ts-node/register', join(__dirname, '..', 'support', 's3-slow-upload-child.ts'), prefix, KEY, String(HEAD), String(TAIL)],
      { env: { ...process.env, TS_NODE_TRANSPILE_ONLY: 'true' }, stdio: 'pipe' },
    );
  }

  /** Resolves with the first stdout line equal to / starting with `marker`. */
  function nextLine(proc: ChildProcessWithoutNullStreams, marker: string, timeoutMs = 60_000): Promise<string> {
    return new Promise((resolve, reject) => {
      let buffered = '';
      let stderr = '';
      const timer = setTimeout(() => reject(new Error(`timed out waiting for ${marker}; stderr: ${stderr}`)), timeoutMs);
      proc.stderr.on('data', (d: Buffer) => (stderr += d.toString()));
      proc.stdout.on('data', (d: Buffer) => {
        buffered += d.toString();
        const hit = buffered.split('\n').find((line) => line.startsWith(marker));
        if (hit) {
          clearTimeout(timer);
          resolve(hit);
        }
      });
      proc.once('exit', (code) => {
        clearTimeout(timer);
        reject(new Error(`child exited (${code}) before ${marker}; stderr: ${stderr}`));
      });
    });
  }

  it('lets a slow upload in process A complete while process B sweeps with a lease-sized cutoff', async () => {
    child = spawnSlowUploader();
    await nextLine(child, 'READY');
    expect(await listUploads(client, env.bucket, prefix)).toHaveLength(1);

    const sweeper = new S3Storage(client, {
      bucket: env.bucket,
      keyPrefix: prefix,
      staleUploadMs: 24 * 60 * 60 * 1000,
      now: () => Date.now() + 10 * 60_000,
    });
    const removed = await sweeper.removeStaleTemp(LEASE_MS);

    expect(removed).toEqual([]);
    expect(await listUploads(client, env.bucket, prefix)).toHaveLength(1);

    const done = nextLine(child, 'DONE');
    child.stdin.write('go\n');
    expect(await done).toBe(`DONE ${HEAD + TAIL}`);

    const stored = await getObjectBytes(client, env.bucket, `${prefix}${KEY}`);
    expect(stored.length).toBe(HEAD + TAIL);
    expect(stored.subarray(0, HEAD).every((b) => b === 0x41)).toBe(true);
    expect(stored.subarray(HEAD).every((b) => b === 0x42)).toBe(true);
    expect(await listUploads(client, env.bucket, prefix)).toEqual([]);
  }, 120_000);

  it('does reclaim the same upload once the writer is dead and the stale threshold has passed', async () => {
    child = spawnSlowUploader();
    await nextLine(child, 'READY');
    child.kill('SIGKILL');
    await once(child, 'exit');
    expect(await listUploads(client, env.bucket, prefix)).toHaveLength(1);

    const sweeper = new S3Storage(client, {
      bucket: env.bucket,
      keyPrefix: prefix,
      staleUploadMs: 24 * 60 * 60 * 1000,
      now: () => Date.now() + 25 * 60 * 60 * 1000,
    });
    const removed = await sweeper.removeStaleTemp(LEASE_MS);

    expect(removed).toHaveLength(1);
    expect(await listUploads(client, env.bucket, prefix)).toEqual([]);
  }, 120_000);
});
