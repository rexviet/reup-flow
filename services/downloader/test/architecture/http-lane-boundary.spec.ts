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
 *
 *   2. A static scan of every `.ts` file under `src/jobs`, `src/videos`,
 *      and `src/cookie` (tolerated absent -- another agent creates it in
 *      parallel) for the literal string `lanes` appearing in a `require(`,
 *      a dynamic `import(`, or a static `import ... from` position.
 */
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import type { Pool } from 'pg';
import { claimOne } from '../../src/db/jobs.repository';
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
});

const SCAN_DIR_NAMES = ['jobs', 'videos', 'cookie'];

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
 * `src/lanes/` does not exist yet -- that is phase 4's job -- so today this
 * assertion is trivially satisfiable (the filter always returns an empty
 * array because there is nothing under `lanes/` for ANYTHING to load). It is
 * written now, rather than deferred to phase 4, so it becomes load-bearing
 * the moment a lane module exists: any future bypass of the regex above
 * still has to explain why its loaded module shows up here.
 */
describe('module-graph proof (plan §0 principle 1): requiring the HTTP-layer feature modules never pulls in a lanes/ module', () => {
  const FEATURE_MODULES = [
    '../../src/jobs/jobs.module',
    '../../src/videos/videos.module',
    '../../src/cookie/cookie.module',
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
