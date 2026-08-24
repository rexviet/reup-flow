/**
 * Refuses to run destructive DDL (DROP/CREATE TABLE, DROP SCHEMA) unless the
 * connection string plainly targets a local test database.
 *
 * Promoted here from test/spike-skip-locked/db.ts in phase 1: phase 0 only
 * ever dropped its own `spike_items` table, but phase 1's migration test
 * drops and recreates `profiles`, `jobs` and `videos` -- the real schema. The
 * connection string is fully operator-controlled and defaults to the same
 * `reup` database the running compose stack uses, so an errant env var
 * pointing this at a shared staging host must fail loudly here rather than
 * silently DROP the tables that host depends on.
 */
export function assertSafeDdlTarget(connectionString: string, envVarName: string): void {
  let parsed: URL;
  try {
    parsed = new URL(connectionString);
  } catch (err) {
    throw new Error(`refusing to run test DDL: ${envVarName} is not a valid URL (${String(err)})`);
  }
  const host = parsed.hostname;
  const database = parsed.pathname.replace(/^\//, '');
  const isLocalHost = ['localhost', '127.0.0.1', '::1'].includes(host);
  const looksLikeTestDb = database === 'reup' || /test|spike/i.test(database);
  if (!isLocalHost || !looksLikeTestDb) {
    throw new Error(
      `refusing to run test DDL against database "${database}" on host "${host}". ` +
        'Tests only ever run DDL against a local test database (host ' +
        'localhost/127.0.0.1/::1, db name "reup" or containing "test"/"spike"). ' +
        `Point ${envVarName} at your local docker-compose postgres, not a shared ` +
        'or remote environment.',
    );
  }
}
