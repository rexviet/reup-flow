import { Pool } from 'pg';
import { ReviewsRepository } from '../../src/reviews/reviews.repository';
import { TEST_DATABASE_URL } from '../support/db';

/** This suite owns a separate database; it never writes the application's runs. */
describe('real review discovery SQL', () => {
  const name = `reup_reviews_test_${process.pid}`;
  const admin = new Pool({ connectionString: TEST_DATABASE_URL, max: 1 });
  let pool: Pool;
  let repo: ReviewsRepository;
  let created = false;
  beforeAll(async () => {
    const url = new URL(TEST_DATABASE_URL);
    if (!['localhost', '127.0.0.1'].includes(url.hostname)) throw new Error('local test database required');
    await admin.query(`CREATE DATABASE "${name}"`);
    created = true;
    url.pathname = name;
    pool = new Pool({ connectionString: url.toString(), max: 1 });
    await pool.query(`CREATE SCHEMA localization;
      CREATE TABLE localization.runs (aweme_id text, generation integer, status text, project_key text, updated_at timestamptz);
      INSERT INTO localization.runs VALUES
        ('7001',1,'ready_for_review','review/7001/g1/head.json','2026-11-10T00:00:00Z'),
        ('7001',2,'needs_human','review/7001/g2/head.json','2026-11-10T00:00:00Z'),
        ('7002',1,'published',NULL,now()), ('7003',1,'needs_human',NULL,now());`);
    repo = new ReviewsRepository(pool);
  });
  afterAll(async () => {
    await pool?.end();
    if (created) await admin.query(`DROP DATABASE "${name}" WITH (FORCE)`);
    await admin.end();
  });
  it('filters legacy rows, orders tied generations, and returns total on an empty page', async () => {
    const first = await repo.list({ limit: 1, offset: 0 });
    expect(first.total).toBe(2);
    expect(first.items[0]).toMatchObject({ awemeId: '7001', generation: 2, status: 'needs_human' });
    expect((await repo.list({ limit: 1, offset: 1 })).items[0].generation).toBe(1);
    expect(await repo.list({ limit: 1, offset: 50 })).toEqual({ items: [], total: 2, limit: 1, offset: 50 });
  });
});
