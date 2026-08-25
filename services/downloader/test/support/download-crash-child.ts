/**
 * Child process for the "kill it mid-download" half of `download-e2e.spec.ts`.
 *
 * Runs ONE real `DownloadLane` against the parent's schema, the parent's
 * `DATA_DIR`, and the parent's fake CDN, then prints a line and keeps going
 * until it is killed.
 *
 * WHY A REAL PROCESS, KILLED FOR REAL. The DoD line is "Kill process giữa
 * download → restart → không tải lại file đã done". An in-process "simulated
 * crash" always runs some cleanup path of ours -- a `finally`, a `catch`, an
 * abort handler -- and every one of those is code that would not run on a
 * `SIGKILL`, an OOM kill, or a yanked power cable. The question is what the
 * DATABASE and the DISK look like at an arbitrary instant with no cooperation
 * at all, and only a real kill asks it.
 *
 * Deliberately NOT booting `AppModule`: this needs no HTTP server, no Redis and
 * no parser, and each of those is another way for the child to fail to start
 * for reasons that have nothing to do with what is being measured. The lane and
 * the storage are the production classes.
 */
import { Pool } from 'pg';
import { drizzle } from 'drizzle-orm/node-postgres';
import { UndiciCdnClient } from '../../src/cdn/cdn.client';
import * as schema from '../../src/db/schema';
import { DownloadLane } from '../../src/lanes/download.lane';
import type { LaneLogger } from '../../src/lanes/lane.runner';
import { LocalDiskStorage } from '../../src/storage/local-disk.storage';

const DSN = required('CHILD_DATABASE_URL');
const SEARCH_PATH = required('CHILD_SCHEMA');
const DATA_DIR = required('CHILD_DATA_DIR');
const LEASE_SECONDS = Number(process.env.CHILD_LEASE_SECONDS ?? '300');
const MAX_ATTEMPTS = Number(process.env.CHILD_MAX_ATTEMPTS ?? '3');

function required(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required`);
  return value;
}

const logger: LaneLogger = {
  // stdout is the parent's control channel; everything the lane says goes to
  // stderr so the two never interleave.
  error: (m) => process.stderr.write(`${m}\n`),
  warn: (m) => process.stderr.write(`${m}\n`),
  log: (m) => process.stderr.write(`${m}\n`),
};

const pool = new Pool({ connectionString: DSN, options: `-c search_path=${SEARCH_PATH}`, max: 4 });
const db = drizzle(pool, { schema });

const lane = new DownloadLane(
  logger,
  db,
  {
    // Every video in this fixture has a fresh cached URL, so re-resolving is a
    // bug rather than a fallback -- say so loudly instead of hiding it.
    async fetchOneVideo() {
      throw new Error('the crash-child must never need to re-resolve');
    },
  },
  new UndiciCdnClient({ timeoutMs: 10_000 }),
  new LocalDiskStorage(DATA_DIR),
  {
    index: 1,
    leaseSeconds: LEASE_SECONDS,
    maxAttempts: MAX_ATTEMPTS,
    urlTtlSeconds: 7200,
    heartbeatMs: 1_000,
  },
);

lane.start();
process.stdout.write('CHILD_RUNNING\n');
