import { Test } from '@nestjs/testing';
import type { Pool } from 'pg';
import { ConfigModule } from '../../src/config/config.module';
import { DbModule, PG_POOL, DB } from '../../src/db/db.module';
import { fixedEnv } from '../support/fixed-env';

/**
 * Integration suite: exercises DbModule against the real docker-compose
 * Postgres (localhost:5432), because the thing under test IS "does a
 * lifecycle-hook probe against a real connection fail fast" -- no mock
 * Pool can stand in for that without reimplementing pg's own connection
 * failure behaviour.
 */
describe('DbModule', () => {
  it('boots successfully, provides a working PG_POOL/DB, and ends the pool on destroy', async () => {
    const moduleRef = await Test.createTestingModule({
      imports: [ConfigModule.withEnv(fixedEnv()), DbModule],
    }).compile();
    const app = moduleRef.createNestApplication();

    await app.init();

    const pool = app.get<Pool>(PG_POOL);
    const db = app.get(DB);
    expect(db).toBeDefined();

    const result = await pool.query('select 1 as one');
    expect(result.rows[0].one).toBe(1);

    await app.close();

    // OnModuleDestroy must have ended the pool -- a query against an ended
    // pg Pool always rejects, which is how "the pool got closed" is
    // observable from outside the module.
    await expect(pool.query('select 1')).rejects.toThrow();
  });

  it('fails fast at boot with the DSN embedded in the error when Postgres is unreachable', async () => {
    // Port 5599: nothing listens there in the compose stack, so this is a
    // genuine ECONNREFUSED, not a mocked failure.
    const badEnv = fixedEnv({
      DATABASE_URL: 'postgres://reup:reup@localhost:5599/reup',
    });
    const moduleRef = await Test.createTestingModule({
      imports: [ConfigModule.withEnv(badEnv), DbModule],
    }).compile();
    const app = moduleRef.createNestApplication();

    // Deliberately including the DSN in the thrown message is the point
    // (plan §0.3: the defence is the redaction layer, not call-site
    // discipline) -- this assertion is what proves that behaviour, and
    // what would break if someone "fixed" it into a scrubbed message.
    await expect(app.init()).rejects.toThrow(/localhost:5599/);

    await app.close().catch(() => {
      /* app never finished initialising; nothing more to tear down */
    });
  }, 15000);
});
