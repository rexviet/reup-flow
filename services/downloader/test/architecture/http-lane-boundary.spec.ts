/**
 * Phase 3 §3.4 -- boundary enforcement, layer 2 of 2.
 *
 * Layer 1 is `.eslintrc.json`'s `no-restricted-imports` override on
 * `src/jobs/**`, `src/videos/**`, `src/cookie/**`, which forbids a static
 * `import ... from '.../lanes/...'` and is caught at lint time, i.e. while
 * typing. It does NOT see a `require(...)` call or a dynamic `import(...)`
 * expression -- both are ordinary function calls to ESLint's import-rule
 * machinery, not `ImportDeclaration` nodes, so a lane reached that way
 * would sail straight through lint clean. This file is what catches that.
 *
 * Two independent proofs, kept both per plan §3.4 ("giữ cả hai, không chọn
 * một"):
 *
 *   1. A raw-SQL round trip: a `jobs` row inserted with plain `pg`, no
 *      controller, no service, no Nest at all, is claimable by the exact
 *      repository function the phase-4 lane will call. This demonstrates
 *      the lane side needs NOTHING from the HTTP layer beyond the row
 *      itself -- Postgres is the only channel (plan §0 principle 1). The
 *      full version of this proof -- a real running lane picking up a real
 *      HTTP-created job -- arrives in phase 4 once a lane exists to run;
 *      this is the strongest version available today, and it is
 *      deliberately written against `jobsRepository.claimOne`, the actual
 *      function the lane will call, not a reimplementation of its query.
 *      PHASE 4 completes it: a real `ReaperLane`, driven by `runOnce()`, now
 *      acts on a row this file inserted with plain `pg` -- see the second
 *      test in that block.
 *
 *   2. A static scan of every `.ts` file under `src/jobs`, `src/videos`,
 *      and `src/cookie` (tolerated absent -- another agent creates it in
 *      parallel) for the literal string `lanes` appearing in a `require(`,
 *      a dynamic `import(`, or a static `import ... from` position.
 */
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import type { Pool } from 'pg';
import { claimOne } from '../../src/db/jobs.repository';
import { ReaperLane } from '../../src/lanes/reaper.lane';
import { LocalDiskStorage } from '../../src/storage/local-disk.storage';
import {
  applyForwardMigration,
  createEmptySchema,
  createTestPool,
  dropSchema,
  testDb,
  testSchemaName,
  truncateAll,
} from '../support/db';

jest.setTimeout(30_000);

const SCHEMA = testSchemaName('arch_boundary');

let pool: Pool;
let db: ReturnType<typeof testDb>;

beforeAll(async () => {
  await createEmptySchema(SCHEMA);
  pool = createTestPool(SCHEMA);
  await applyForwardMigration(pool);
  db = testDb(pool);
});

afterAll(async () => {
  await pool.end();
  await dropSchema(SCHEMA);
});

afterEach(async () => {
  await truncateAll(pool);
});

describe('raw-SQL round trip (plan §0 principle 1: Postgres is the only channel between the HTTP layer and a lane)', () => {
  it('a job row inserted by plain SQL, with no controller or service involved, is claimable by jobsRepository.claimOne -- the exact function the phase-4 lane will call', async () => {
    await pool.query('INSERT INTO profiles (sec_user_id) VALUES ($1)', ['arch-profile']);
    const inserted = await pool.query('INSERT INTO jobs (sec_user_id) VALUES ($1) RETURNING id', [
      'arch-profile',
    ]);
    const insertedId: string = inserted.rows[0].id;

    const claimed = await claimOne(db, { leaseSeconds: 300 });

    expect(claimed?.id).toBe(insertedId);
    expect(claimed?.status).toBe('enumerating');
  });

  it('a REAL lane acts on a row inserted by plain SQL, with no HTTP layer anywhere in the picture', async () => {
    // Phase 4's half of this DoD item, and the stronger version of the test
    // above: not "the function a lane will call", but an actual lane instance,
    // ticked by hand.
    //
    // The whole causal chain here is `INSERT` -> lane. There is no controller,
    // no service, no Nest container, and nothing told the lane the row existed.
    // If the HTTP layer were the thing that woke lanes up -- a direct call, an
    // event, an injected reference -- this test could not pass, because none of
    // it is present. That is what plan §0 principle 1 buys: moving the lanes
    // into their own process later changes no logic, because the lanes already
    // learn about work the only way they ever will, by reading Postgres.
    //
    // `runOnce()` called directly, with no scheduler, is reason 3 for choosing
    // Approach 1 (solution design §4).
    const root = await mkdtemp(join(tmpdir(), 'reup-arch-'));
    try {
      await pool.query('INSERT INTO profiles (sec_user_id) VALUES ($1)', ['arch-lane-profile']);
      // Straight into `enumerating` with a lease already in the past: a job
      // whose lane died mid-enumerate (F5), written by hand rather than by
      // getting a lane to crash.
      const inserted = await pool.query(
        `INSERT INTO jobs (sec_user_id, status, lease_expires_at, max_cursor)
         VALUES ($1, 'enumerating', now() - interval '1 hour', 999) RETURNING id`,
        ['arch-lane-profile'],
      );
      const jobId: string = inserted.rows[0].id;

      const lane = new ReaperLane(
        { error: () => undefined, warn: () => undefined, log: () => undefined },
        db,
        new LocalDiskStorage(root),
        { leaseSeconds: 300, maxAttempts: 3 },
      );
      await lane.runOnce(new AbortController().signal);

      const after = await pool.query('SELECT status, max_cursor FROM jobs WHERE id = $1', [jobId]);
      expect(after.rows[0].status).toBe('pending');
      // Checkpoint intact, so enumeration resumes rather than re-paging.
      expect(Number(after.rows[0].max_cursor)).toBe(999);
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });
});

const SCAN_DIR_NAMES = ['jobs', 'videos', 'cookie', 'workspace', 'temporal'];

/** Every literal-string-containing spot ESLint's `no-restricted-imports`
 * does NOT check: a `require(...)` call and a dynamic `import(...)`
 * expression, plus (redundantly, but cheaply) the static `import ... from`
 * form lint already covers -- belt and suspenders costs nothing here. */
const LANE_REFERENCE_PATTERN = /(?:\bfrom\s+|\brequire\s*\(|\bimport\s*\()\s*['"`][^'"`]*\blanes\b[^'"`]*['"`]/;

function listTsFilesRecursive(dir: string): string[] {
  if (!existsSync(dir)) return [];
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...listTsFilesRecursive(full));
    else if (entry.name.endsWith('.ts')) out.push(full);
  }
  return out;
}

describe('static scan for a lane reference (plan §0 principle 1)', () => {
  it('no file under src/jobs, src/videos, or src/cookie references "lanes" in a require(), a dynamic import(), or a static import', () => {
    const srcRoot = resolve(__dirname, '../../src');
    const offenders: string[] = [];

    for (const dirName of SCAN_DIR_NAMES) {
      const dir = join(srcRoot, dirName);
      // src/cookie is owned and created by another agent working in
      // parallel on this same phase -- tolerate it not existing yet rather
      // than failing this suite on a race with their work landing.
      for (const file of listTsFilesRecursive(dir)) {
        const text = readFileSync(file, 'utf8');
        if (LANE_REFERENCE_PATTERN.test(text)) offenders.push(file);
      }
    }

    expect(offenders).toEqual([]);
  });
});

/**
 * FIX 7 (review): `LANE_REFERENCE_PATTERN` above is a fast, precise
 * tripwire, but it is still a regex over source TEXT, and a determined
 * bypass does not have to look like any of the three forms it checks for --
 * `const L = '../lanes/x'; await import(L)` (the string never contains the
 * literal substring `from`/`require(`/`import(` next to `lanes`), string
 * concatenation (`'../la' + 'nes/x'`), a re-export through a barrel file one
 * level removed from `jobs`/`videos`/`cookie`, or a template literal. Every
 * one of those slips past a text scan while still loading the module at
 * runtime.
 *
 * This is the second proof, and it is not a regex at all: it requires the
 * three HTTP-layer feature modules in a FRESH module registry and inspects
 * `require.cache` afterwards -- i.e. it asks Node what actually got loaded,
 * not what the source text looks like. Nothing under `src/jobs`,
 * `src/videos`, or `src/cookie` can make a lane module execute without that
 * module ending up in `require.cache` under a `lanes/` path, regardless of
 * HOW it got there (indirection, string-building, a barrel re-export all
 * still have to actually `require()`/`import()` the file to run it).
 *
 * PHASE 4 UPDATE. When this was written `src/lanes/` did not exist, so the
 * assertion was trivially satisfiable -- the filter returned an empty array
 * because there was nothing under `lanes/` for anything to load. It exists
 * now, and so this is load-bearing for the first time.
 *
 * It is worth being precise about what the regex above does and does not cover,
 * because the phase-3 mutation that "proved" this block was actually caught by
 * the regex, not by this. `test/mutations/phase-04.sh` fixes that with a
 * mutation shaped to slip past a text scan entirely: a module-scope
 * `require(computedPath)` where the path is built by concatenation, so no
 * string literal containing `lanes` ever sits next to `require(`. The regex
 * cannot see it; `require.cache` cannot miss it, because running the module is
 * what puts it there.
 */
describe('Temporal is read only by the workspace view', () => {
  it('no file outside src/workspace and src/temporal imports @temporalio', () => {
    const srcRoot = resolve(__dirname, '../../src');
    const offenders = listTsFilesRecursive(srcRoot)
      .filter((file) => !/[\\/]src[\\/](workspace|temporal)[\\/]/.test(file))
      .filter((file) => /@temporalio/.test(readFileSync(file, 'utf8')));
    expect(offenders).toEqual([]);
  });

  it('nothing under src/workspace or src/temporal can write: no start, signal, cancel, terminate or update call', () => {
    const srcRoot = resolve(__dirname, '../../src');
    const writers = [...listTsFilesRecursive(join(srcRoot, 'workspace')), ...listTsFilesRecursive(join(srcRoot, 'temporal'))]
      .filter((file) => /\.(start|signalWithStart|signal|cancel|terminate|update|execute)\s*\(/.test(readFileSync(file, 'utf8')));
    expect(writers).toEqual([]);
  });
});

describe('module-graph proof (plan §0 principle 1): requiring the HTTP-layer feature modules never pulls in a lanes/ module', () => {
  const FEATURE_MODULES = [
    '../../src/jobs/jobs.module',
    '../../src/videos/videos.module',
    '../../src/cookie/cookie.module',
    '../../src/workspace/workspace.module',
    '../../src/reviews/reviews.module',
  ];

  it('no entry in require.cache resolves under a lanes/ path after requiring jobs.module, videos.module, and cookie.module', () => {
    // A fresh registry per assertion: without this, a lane module `require`d
    // by an EARLIER test in this process (there is none today, but the
    // whole point of this test is to still be correct once one exists)
    // could already be cached, and this test would see it regardless of
    // whether requiring the feature modules below is what put it there.
    jest.resetModules();

    for (const modulePath of FEATURE_MODULES) {
      // eslint-disable-next-line @typescript-eslint/no-require-imports
      require(modulePath);
    }

    const laneEntries = Object.keys(require.cache).filter((resolvedPath) =>
      /[\\/]lanes[\\/]/.test(resolvedPath),
    );

    expect(laneEntries).toEqual([]);
  });
});

describe('review discovery is a database query only', () => {
  it('imports neither object storage nor Temporal nor lane writers', () => {
    const sources = listTsFilesRecursive(resolve(__dirname, '../../src/reviews'));
    expect(sources.length).toBeGreaterThan(0);
    for (const file of sources) {
      expect(readFileSync(file, 'utf8')).not.toMatch(/@temporalio|@aws-sdk|from ['"].*(?:storage|lanes)\//);
    }
  });
});
