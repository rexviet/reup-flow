import type { Pool } from 'pg';
import { DashboardRepository } from '../../src/dashboard/dashboard.repository';
import { createScratchDatabase, type ScratchDatabase } from '../support/localization-db';

describe('DashboardRepository (real downloader and localization schemas)', () => {
  let scratch: ScratchDatabase;
  let pool: Pool;
  let repo: DashboardRepository;

  beforeAll(async () => {
    scratch = await createScratchDatabase();
    pool = scratch.pool;
    repo = new DashboardRepository(pool);
  }, 60_000);
  afterAll(() => scratch.drop());

  beforeEach(async () => {
    await pool.query(
      `TRUNCATE videos, jobs, profiles, localization_outbox, review_decisions, publications,
                studio_open_requests, dashboard_actions,
                localization.runs, localization.final_renders, localization.review_status,
                localization.host_workers, localization.llm_calls RESTART IDENTITY CASCADE`,
    );
    await pool.query(
      `INSERT INTO profiles (sec_user_id, nickname, auto_localize) VALUES ('chA', 'Kênh A', true), ('chB', NULL, false)`,
    );
  });

  const video = (id: string, sec: string, desc: string | null, createTime: string, status = 'done') =>
    pool.query(
      `INSERT INTO videos (aweme_id, sec_user_id, "desc", create_time, status) VALUES ($1, $2, $3, $4, $5)`,
      [id, sec, desc, createTime, status],
    );

  it('joins every source of one video: outbox, runs, decision, render, publication, review status', async () => {
    await video('100', 'chA', 'một video', '2026-11-01T00:00:00Z');
    await pool.query(
      `INSERT INTO localization_outbox (aweme_id, generation, state, dispatched_at, workflow_id)
       VALUES ('100', 1, 'dispatched', now(), 'video-100-g1')`,
    );
    await pool.query(
      `INSERT INTO localization.runs (aweme_id, generation, workflow_id, status, project_key, output_key)
       VALUES ('100', 1, 'video-100-g1', 'needs_human', NULL, NULL),
              ('100', 3, 'video-100-g3', 'ready_for_review', 'review/100/g3/head.json', NULL)`,
    );
    await pool.query(
      `INSERT INTO review_decisions (aweme_id, generation, decision, approved_revision)
       VALUES ('100', 3, 'approved', 'rev-9')`,
    );
    await pool.query(
      `INSERT INTO localization.final_renders (aweme_id, generation, approved_revision, state, final_key)
       VALUES ('100', 3, 'rev-9', 'rendered', 'final/100/g3.mp4')`,
    );
    await pool.query(`INSERT INTO publications (aweme_id, generation, state) VALUES ('100', 3, 'unpublished')`);
    await pool.query(
      `INSERT INTO localization.review_status (aweme_id, generation, revision, mismatch_count, dirty)
       VALUES ('100', 3, 'rev-9', 2, false)`,
    );

    const rec = await repo.video('100');
    expect(rec).not.toBeNull();
    expect(rec?.nickname).toBe('Kênh A');
    expect(rec?.autoLocalize).toBe(true);
    expect(rec?.outbox).toMatchObject({ generation: 1, state: 'dispatched', workflowId: 'video-100-g1' });
    expect(rec?.runs.map((r) => [r.generation, r.status, r.projectKey])).toEqual([
      [1, 'needs_human', null],
      [3, 'ready_for_review', 'review/100/g3/head.json'],
    ]);
    expect(rec?.decisions).toMatchObject([{ generation: 3, decision: 'approved', approvedRevision: 'rev-9' }]);
    expect(rec?.renders).toMatchObject([{ generation: 3, state: 'rendered', finalKey: 'final/100/g3.mp4' }]);
    expect(rec?.publications).toMatchObject([{ generation: 3, state: 'unpublished' }]);
    expect(rec?.reviewStatus).toMatchObject([{ generation: 3, revision: 'rev-9', mismatchCount: 2, dirty: false }]);
  });

  it('returns a video with no outbox row and no runs, and null for an unknown id', async () => {
    await video('101', 'chB', null, '2026-11-02T00:00:00Z', 'downloading');
    const rec = await repo.video('101');
    expect(rec).toMatchObject({ outbox: null, runs: [], decisions: [], nickname: null, autoLocalize: false });
    expect(await repo.video('999')).toBeNull();
  });

  it('lists newest first and filters by channel and date range', async () => {
    await video('1', 'chA', 'a', '2026-11-01T00:00:00Z');
    await video('2', 'chA', 'b', '2026-11-03T00:00:00Z');
    await video('3', 'chB', 'c', '2026-11-05T00:00:00Z');
    expect((await repo.videos()).map((v) => v.awemeId)).toEqual(['3', '2', '1']);
    expect((await repo.videos({ channel: 'chA' })).map((v) => v.awemeId)).toEqual(['2', '1']);
    expect((await repo.videos({ from: '2026-11-02T00:00:00Z', to: '2026-11-04T00:00:00Z' })).map((v) => v.awemeId)).toEqual(['2']);
  });

  it('searches aweme_id and description, matching % and _ literally', async () => {
    await video('7001', 'chA', 'giảm giá 50% hôm nay', '2026-11-01T00:00:00Z');
    await video('7002', 'chA', 'tên_file chuẩn', '2026-11-02T00:00:00Z');
    await video('7003', 'chA', 'khác hẳn', '2026-11-03T00:00:00Z');
    const ids = async (q: string) => (await repo.videos({ q })).map((v) => v.awemeId);
    expect(await ids('7002')).toEqual(['7002']);
    expect(await ids('GIẢM')).toEqual(['7001']);
    expect(await ids('50%')).toEqual(['7001']);
    expect(await ids('%')).toEqual(['7001']);
    expect(await ids('_')).toEqual(['7002']);
    expect(await ids('a_b')).toEqual([]);
    expect(await ids('\\')).toEqual([]);
  });

  it('treats hostile input as data', async () => {
    await video('1', 'chA', 'x', '2026-11-01T00:00:00Z');
    expect(await repo.videos({ q: "'; DROP TABLE videos; --" })).toEqual([]);
    expect(await repo.videos({ channel: "chA' OR '1'='1" })).toEqual([]);
    expect(await repo.videos()).toHaveLength(1);
  });

  it('sums LLM cost per generation and stage', async () => {
    await pool.query(
      `INSERT INTO localization.llm_calls (generation_id, aweme_id, generation, stage, cost_usd) VALUES
         ('g-1','5',1,'agent__localizer',0.1), ('g-2','5',1,'agent__localizer',0.2),
         ('g-3','5',3,'agent__dubber',0.5), ('g-4','6',1,'agent__dubber',9)`,
    );
    expect(await repo.llmCosts('5')).toEqual([
      { generation: 1, stage: 'agent__localizer', calls: 2, costUsd: expect.closeTo(0.3, 8) },
      { generation: 3, stage: 'agent__dubber', calls: 1, costUsd: 0.5 },
    ]);
  });

  it('returns the verdict of each run', async () => {
    await pool.query(
      `INSERT INTO localization.runs (aweme_id, generation, workflow_id, status, verdict)
       VALUES ('5', 2, 'video-5-g2', 'failed', '{"ok": false}')`,
    );
    expect(await repo.runDetails('5')).toMatchObject([{ generation: 2, verdict: { ok: false } }]);
  });

  it('shows the newest job of each channel and blocked or failed ones only', async () => {
    await pool.query(
      `INSERT INTO jobs (sec_user_id, status, error, created_at, updated_at) VALUES
         ('chA', 'failed', 'old failure', now() - interval '2 day', now() - interval '2 day'),
         ('chA', 'completed', NULL, now() - interval '1 day', now() - interval '1 day'),
         ('chB', 'blocked', 'cookie expired', now(), now())`,
    );
    const blocked = await repo.blockedJobs();
    expect(blocked).toMatchObject([{ secUserId: 'chB', status: 'blocked', error: 'cookie expired', nickname: null }]);
    const channels = await repo.channels();
    expect(channels.find((c) => c.secUserId === 'chA')?.latestJob?.status).toBe('completed');
    expect(channels.find((c) => c.secUserId === 'chB')).toMatchObject({ autoLocalize: false });
  });

  it('records actions newest first and reads the host worker heartbeat', async () => {
    await pool.query(
      `INSERT INTO dashboard_actions (aweme_id, action, created_at) VALUES
         ('5', 'localize', now() - interval '1 hour'), ('5', 'rerun', now()), ('6', 'localize', now())`,
    );
    expect((await repo.recentActions('5')).map((a) => a.action)).toEqual(['rerun', 'localize']);
    expect(await repo.latestStudioRequest('5')).toBeNull();
    await pool.query(
      `INSERT INTO studio_open_requests (aweme_id, generation, state, requested_at) VALUES
         ('5', 1, 'done', now() - interval '1 hour'), ('5', 2, 'pending', now())`,
    );
    expect(await repo.latestStudioRequest('5')).toMatchObject({ generation: 2, state: 'pending', handledAt: null });
    expect(await repo.hostWorker('review-worker')).toBeNull();
    await pool.query(
      `INSERT INTO localization.host_workers (name, info) VALUES ('review-worker', '{"disk_free_bytes": 5}')`,
    );
    expect(await repo.hostWorker('review-worker')).toMatchObject({ info: { disk_free_bytes: 5 } });
  });

  it('lists 2000 videos with all joins comfortably inside the 500 ms budget', async () => {
    await pool.query(
      `INSERT INTO videos (aweme_id, sec_user_id, create_time, status)
       SELECT g::text, 'chA', now() - g * interval '1 minute', 'done' FROM generate_series(1, 2000) g`,
    );
    await pool.query(
      `INSERT INTO localization_outbox (aweme_id, generation, state) SELECT g::text, 1, 'held' FROM generate_series(1, 2000) g`,
    );
    await pool.query(
      `INSERT INTO localization.runs (aweme_id, generation, workflow_id, status)
       SELECT g::text, 1, 'video-' || g || '-g1', 'ready_for_review' FROM generate_series(1, 1000) g`,
    );
    const started = Date.now();
    const all = await repo.videos();
    expect(all).toHaveLength(2000);
    expect(Date.now() - started).toBeLessThan(500);
  });
});
