/**
 * Phase 4 §4.2 DoD -- "`SIGTERM` giữa lúc có việc → lease được nhả, restart
 * nhặt lại NGAY (không chờ 5 phút)."
 *
 * Observed from outside a real child process, because that is the only place
 * the claim can be checked. Every in-process test of shutdown -- and there are
 * several, in lane.supervisor.spec.ts and lease-release.spec.ts -- calls the
 * hook or `app.close()` directly, so all of them keep passing with
 * `app.enableShutdownHooks()` deleted from `src/bootstrap.ts`. Without that
 * call Nest installs no signal listeners: `SIGTERM` kills the process where it
 * stands, no lane stops, no lease is released, and the restarted process sits
 * next to work it may not touch until `LEASE_SECONDS` elapses. That is a
 * one-line deletion with a five-minute blast radius and no in-process test can
 * see it. Hence a real signal at a real process.
 *
 * The child runs the real `AppModule` with ONE provider swapped (`LANES`), and
 * releases through the real `videosRepository.release`. See
 * test/support/lane-shutdown-child.ts.
 */
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { join } from 'node:path';
import type { Pool } from 'pg';
import { claimOne } from '../../src/db/videos.repository';
import {
  applyForwardMigration,
  createEmptySchema,
  createTestPool,
  dropSchema,
  testDb,
  testSchemaName,
} from '../support/db';

jest.setTimeout(120_000);

const SCHEMA = testSchemaName('lane_sigterm');
const PROFILE = 'MS4wLjABAAAA_sigterm';
const AWEME = 'sigterm-video-1';
const LEASE_SECONDS = 300;
const MAX_ATTEMPTS = 3;

let pool: Pool;
let db: ReturnType<typeof testDb>;
let child: ChildProcessWithoutNullStreams | undefined;

/**
 * The child needs its own connection into the SAME per-run schema, and it only
 * gets a DSN. Postgres's `options` startup parameter, which `pg` forwards from
 * the connection string, is what pins `search_path` for a process this test
 * cannot hand a `Pool` to.
 */
function schemaScopedDsn(schema: string): string {
  const base = process.env.TEST_DATABASE_URL ?? 'postgres://reup:reup@localhost:5432/reup';
  const url = new URL(base);
  url.searchParams.set('options', `-c search_path=${schema}`);
  return url.toString();
}

beforeAll(async () => {
  await createEmptySchema(SCHEMA);
  pool = createTestPool(SCHEMA);
  await applyForwardMigration(pool);
  db = testDb(pool);
});

afterAll(async () => {
  if (child && child.exitCode === null) child.kill('SIGKILL');
  await pool.end();
  await dropSchema(SCHEMA);
});

/** Resolves when the child's stdout has produced a line containing `marker`. */
function waitForLine(
  proc: ChildProcessWithoutNullStreams,
  marker: string,
  timeoutMs = 60_000,
): Promise<void> {
  return new Promise((resolve, reject) => {
    let seen = '';
    const timer = setTimeout(() => {
      cleanup();
      reject(new Error(`child never printed ${marker}. stdout so far:\n${seen}\n`));
    }, timeoutMs);
    const onData = (chunk: Buffer): void => {
      seen += chunk.toString();
      if (seen.includes(marker)) {
        cleanup();
        resolve();
      }
    };
    // A child that dies before printing the marker must fail fast, not wait out
    // the whole timeout with a message that says nothing about why.
    const onExit = (code: number | null): void => {
      cleanup();
      reject(new Error(`child exited (${code}) before printing ${marker}. stdout:\n${seen}\n`));
    };
    function cleanup(): void {
      clearTimeout(timer);
      proc.stdout.off('data', onData);
      proc.off('exit', onExit);
    }
    proc.stdout.on('data', onData);
    proc.on('exit', onExit);
  });
}

function waitForExit(
  proc: ChildProcessWithoutNullStreams,
): Promise<{ code: number | null; signal: NodeJS.Signals | null }> {
  return new Promise((resolve) => {
    proc.on('exit', (code, signal) => resolve({ code, signal }));
  });
}

describe('SIGTERM releases a held lease before the process exits', () => {
  let exit: { code: number | null; signal: NodeJS.Signals | null };
  let stderr = '';
  // The child logs from two places on purpose, and they land on different
  // pipes. The lane's own logger is wired to stderr by the harness; the
  // SUPERVISOR uses the real production `RedactingLogger`, which goes to
  // stdout through Nest. Both are asserted below, because they prove different
  // halves: the supervisor's line says Nest's signal listener fired at all,
  // the lane's line says the release actually completed.
  let stdout = '';

  beforeAll(async () => {
    await pool.query('INSERT INTO profiles (sec_user_id) VALUES ($1)', [PROFILE]);
    await pool.query(
      'INSERT INTO videos (aweme_id, sec_user_id, create_time, status) VALUES ($1, $2, now(), $3)',
      [AWEME, PROFILE, 'pending'],
    );

    child = spawn(
      process.execPath,
      ['-r', 'ts-node/register/transpile-only', join(__dirname, '..', 'support', 'lane-shutdown-child.ts')],
      {
        cwd: join(__dirname, '..', '..'),
        env: {
          ...process.env,
          API_KEY: 'sigterm-harness-key-not-real',
          DATABASE_URL: schemaScopedDsn(SCHEMA),
          // Redis logical database 9, never the default 0: the compose stack's
          // parser reads `douyin:cookie:current` from db 0 on this same server,
          // and RedisModule's boot probe must not go anywhere near it.
          REDIS_URL: 'redis://localhost:6379/9',
          PARSER_BASE_URL: 'http://parser:80',
          PROBE_SEC_USER_ID: 'probe-sec-user-id',
          LANES_ENABLED: 'true',
          CHILD_LEASE_SECONDS: String(LEASE_SECONDS),
          CHILD_MAX_ATTEMPTS: String(MAX_ATTEMPTS),
        },
      },
    );
    child.stderr.on('data', (c: Buffer) => {
      stderr += c.toString();
    });
    child.stdout.on('data', (c: Buffer) => {
      stdout += c.toString();
    });

    // Wait for the lane to actually be HOLDING the claim. Signalling before
    // that would test nothing: an idle lane has no lease to release, so the
    // assertions below would pass with the whole release path deleted.
    await waitForLine(child, 'CHILD_CLAIMED');

    const held = await pool.query('SELECT status, lease_expires_at FROM videos WHERE aweme_id = $1', [
      AWEME,
    ]);
    expect(held.rows[0].status).toBe('downloading');
    expect(held.rows[0].lease_expires_at).not.toBeNull();

    const exited = waitForExit(child);
    child.kill('SIGTERM');
    exit = await exited;
  });

  it('runs its shutdown hooks -- the supervisor saw the signal', () => {
    // The first version of this test asserted `{ code: 0, signal: null }`, on
    // the theory that a handled signal means a normal exit. It does not: Nest's
    // shutdown handler re-raises the signal at itself once the hooks are done
    // (`process.kill(process.pid, signal)`), which is correct POSIX behaviour
    // and means the exit status is `signal: 'SIGTERM'` whether the hooks ran or
    // not. The exit status DISTINGUISHES NOTHING here, so asserting on it was
    // theatre that happened to be red.
    //
    // What does distinguish: the supervisor's own line, which only exists on
    // the path through `beforeApplicationShutdown`. With
    // `enableShutdownHooks()` removed from `configureApp`, Nest installs no
    // listener, nothing below is printed, and the row assertions that follow
    // fail too.
    expect(stdout).toContain('received SIGTERM');
    expect(exit.signal).toBe('SIGTERM');
  });

  it('hands the video back to pending, with no lease left behind', async () => {
    const row = await pool.query(
      'SELECT status, lease_expires_at, attempts FROM videos WHERE aweme_id = $1',
      [AWEME],
    );
    expect(row.rows[0].status).toBe('pending');
    expect(row.rows[0].lease_expires_at).toBeNull();
    // The attempt taken at claim time is still spent -- the F6 tradeoff, not an
    // oversight. See videos.repository.ts's `release`.
    expect(Number(row.rows[0].attempts)).toBe(1);
  });

  it('makes the video claimable IMMEDIATELY, not after LEASE_SECONDS', async () => {
    // The DoD line, measured. `LEASE_SECONDS` is 300 here and this claim
    // happens within a second of the child exiting, so a green assertion can
    // only mean the lease was released rather than expired. Nothing in this
    // suite ever waits for a lease to time out.
    const reclaimed = await claimOne(db, {
      leaseSeconds: LEASE_SECONDS,
      maxAttempts: MAX_ATTEMPTS,
    });

    expect(reclaimed?.awemeId).toBe(AWEME);
  });

  it('says what it released, on the way out', () => {
    expect(stderr).toContain('released video');
  });
});
