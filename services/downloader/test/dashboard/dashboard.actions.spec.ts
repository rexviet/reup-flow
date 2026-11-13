import { drizzle } from 'drizzle-orm/node-postgres';
import type { Pool } from 'pg';
import { DashboardActionsService } from '../../src/dashboard/dashboard.actions';
import { DashboardRepository } from '../../src/dashboard/dashboard.repository';
import { actionBodySchema, type ActionBody } from '../../src/dashboard/dashboard.schemas';
import * as schema from '../../src/db/schema';
import type { DashboardTemporal, WorkflowStatus } from '../../src/workspace/workspace.dashboard-reader';
import { createScratchDatabase, type ScratchDatabase } from '../support/localization-db';

jest.setTimeout(60_000);

class FakeTemporal implements DashboardTemporal {
  open: string[] = [];
  statuses = new Map<string, WorkflowStatus>();
  down = false;
  async listOpenLocalizationIds() {
    if (this.down) throw new Error('temporal unreachable');
    return this.open.map((workflowId) => ({ workflowId, startedAtMs: 0 }));
  }
  async workflowStatus(id: string): Promise<WorkflowStatus> {
    if (this.down) throw new Error('temporal unreachable');
    return this.statuses.get(id) ?? 'closed';
  }
  async pendingActivities() {
    return [];
  }
  async inspect() {
    return null;
  }
}

const body = (action: ActionBody['action'], aweme_ids: string[], rest: Partial<ActionBody> = {}): ActionBody => ({
  action,
  aweme_ids,
  ...rest,
});

describe('DashboardActionsService (real downloader and localization schemas)', () => {
  let scratch: ScratchDatabase;
  let pool: Pool;
  let temporal: FakeTemporal;
  let svc: DashboardActionsService;

  beforeAll(async () => {
    scratch = await createScratchDatabase();
    pool = scratch.pool;
  }, 60_000);
  afterAll(() => scratch.drop());

  beforeEach(async () => {
    await pool.query(
      `TRUNCATE videos, jobs, profiles, localization_outbox, review_decisions, publications,
                studio_open_requests, dashboard_actions,
                localization.runs, localization.final_renders, localization.review_status
         RESTART IDENTITY CASCADE`,
    );
    temporal = new FakeTemporal();
    svc = new DashboardActionsService(pool, drizzle(pool, { schema }), new DashboardRepository(pool), temporal);
  });

  const video = (id: string) =>
    pool.query(`INSERT INTO videos (aweme_id, sec_user_id, create_time, status) VALUES ($1, 'ch', now(), 'done')`, [id]);
  const outbox = (id: string, gen: number, state: string) =>
    pool.query(`INSERT INTO localization_outbox (aweme_id, generation, state) VALUES ($1, $2, $3)`, [id, gen, state]);
  const run = (id: string, gen: number, status: string, projectKey: string | null = 'review/p/head.json', outputKey: string | null = null) =>
    pool.query(
      `INSERT INTO localization.runs (aweme_id, generation, workflow_id, status, project_key, output_key)
       VALUES ($1, $2, $3, $4, $5, $6)`,
      [id, gen, `video-${id}-g${gen}`, status, projectKey, outputKey],
    );
  const status = (id: string, gen: number, rev: string | null, mismatch = 0, dirty: boolean | null = false) =>
    pool.query(
      `INSERT INTO localization.review_status (aweme_id, generation, revision, mismatch_count, dirty)
       VALUES ($1, $2, $3, $4, $5)`,
      [id, gen, rev, mismatch, dirty],
    );
  const one = async <T = Record<string, unknown>>(sql: string, params: unknown[] = []): Promise<T | undefined> =>
    (await pool.query(sql, params)).rows[0] as T | undefined;
  const logged = async (id: string) =>
    (await pool.query(`SELECT action FROM dashboard_actions WHERE aweme_id = $1 ORDER BY id`, [id])).rows.map((r) => r.action);

  describe('request validation', () => {
    it('caps the id array at 100 and rejects empty, bad ids and unknown actions', () => {
      const ids = (n: number) => Array.from({ length: n }, (_, i) => String(i + 1));
      expect(actionBodySchema.safeParse({ action: 'localize', aweme_ids: ids(100) }).success).toBe(true);
      expect(actionBodySchema.safeParse({ action: 'localize', aweme_ids: ids(101) }).success).toBe(false);
      expect(actionBodySchema.safeParse({ action: 'localize', aweme_ids: [] }).success).toBe(false);
      expect(actionBodySchema.safeParse({ action: 'localize', aweme_ids: ['x1'] }).success).toBe(false);
      expect(actionBodySchema.safeParse({ action: 'delete', aweme_ids: ['1'] }).success).toBe(false);
      expect(actionBodySchema.safeParse({ action: 'approve', aweme_ids: ['1'] }).success).toBe(false);
      expect(actionBodySchema.safeParse({ action: 'open_studio', aweme_ids: ['1', '2'] }).success).toBe(false);
      expect(actionBodySchema.safeParse({ action: 'approve', aweme_ids: ['1', '2'], revision: 'r' }).success).toBe(false);
    });
  });

  describe('localize', () => {
    it('moves held to pending, skips the rest with the reason, and logs once', async () => {
      // The sequential queue releases a held row at once unless a video is in flight; one is,
      // so row 1 stays held until the admin sends it.
      await pool.query(`INSERT INTO auto_queue_inflight (aweme_id, generation) VALUES ('0', 1)`);
      await video('1');
      await outbox('1', 1, 'held');
      await video('2');
      await outbox('2', 1, 'dispatched');
      await video('3');
      const res = await svc.run(body('localize', ['1', '2', '3', '4']));
      expect(res.done).toEqual(['1']);
      expect(res.skipped.map((s) => s.aweme_id)).toEqual(['2', '3', '4']);
      expect(res.skipped[0].reason).toContain('dispatched');
      expect((await one('SELECT state FROM localization_outbox WHERE aweme_id = $1', ['1']))?.state).toBe('pending');
      expect(await logged('1')).toEqual(['localize']);
      const again = await svc.run(body('localize', ['1']));
      expect(again.done).toEqual([]);
      expect(await logged('1')).toEqual(['localize']);
      await pool.query('DELETE FROM auto_queue_inflight');
    });
  });

  describe('retry_download', () => {
    const videoRow = (id: string) => one<{ status: string; attempts: number; last_error: string | null }>(
      'SELECT status, attempts, last_error FROM videos WHERE aweme_id = $1', [id]);

    it('puts a failed download back in the queue with fresh attempts, once, and logs it', async () => {
      await pool.query(
        `INSERT INTO videos (aweme_id, sec_user_id, create_time, status, attempts, last_error, lease_expires_at)
         VALUES ('81', 'ch', now(), 'failed', 5, 'cdn 403', now())`,
      );
      const res = await svc.run(body('retry_download', ['81']));
      expect(res.done).toEqual(['81']);
      expect(await videoRow('81')).toEqual({ status: 'pending', attempts: 0, last_error: null });
      expect(await logged('81')).toEqual(['retry_download']);
      const again = await svc.run(body('retry_download', ['81']));
      expect(again.done).toEqual([]);
      expect(again.skipped[0].reason).toContain('pending');
      expect(await logged('81')).toEqual(['retry_download']);
    });

    it('leaves a video that is not a failed download alone and says what it is', async () => {
      await video('82');
      await pool.query(`UPDATE videos SET status = 'done' WHERE aweme_id = '82'`);
      const res = await svc.run(body('retry_download', ['82', '83']));
      expect(res.done).toEqual([]);
      expect(res.skipped.map((s) => s.aweme_id)).toEqual(['82', '83']);
      expect(res.skipped[0].reason).toContain('done');
      expect((await videoRow('82'))?.status).toBe('done');
    });
  });

  describe('resume', () => {
    beforeEach(async () => {
      await video('20');
    });
    const outboxRow = () =>
      one('SELECT generation, state, dispatch_attempts, last_error, workflow_id FROM localization_outbox');

    it('puts a failed run back to pending under the same generation, once, and logs it', async () => {
      await outbox('20', 2, 'dispatched');
      await run('20', 2, 'failed');
      await pool.query(`UPDATE localization_outbox SET workflow_id = 'video-20-g2', dispatch_attempts = 3, last_error = 'x' WHERE aweme_id = '20'`);
      const res = await svc.run(body('resume', ['20']));
      expect(res).toEqual({ done: ['20'], skipped: [] });
      // Same generation and the workflow id kept: the starter finds that workflow failed and resets it.
      expect(await outboxRow()).toEqual({
        generation: 2,
        state: 'pending',
        dispatch_attempts: 0,
        last_error: null,
        workflow_id: 'video-20-g2',
      });
      expect(await logged('20')).toEqual(['resume']);
      const again = await svc.run(body('resume', ['20']));
      expect(again.done).toEqual([]);
      expect(await logged('20')).toEqual(['resume']);
    });

    it('refuses a run that did not fail, one that is still running, and a Temporal it cannot read', async () => {
      await outbox('20', 1, 'dispatched');
      await run('20', 1, 'ready_for_review');
      const notFailed = await svc.run(body('resume', ['20']));
      expect(notFailed.skipped[0].reason).toContain('thất bại');

      await pool.query(`UPDATE localization.runs SET status = 'failed'`);
      temporal.statuses.set('video-20-g1', 'running');
      expect((await svc.run(body('resume', ['20']))).skipped[0].reason).toContain('đang chạy');

      temporal.statuses.delete('video-20-g1');
      temporal.down = true;
      expect((await svc.run(body('resume', ['20']))).skipped[0].reason).toContain('Temporal');
      expect((await outboxRow())?.state).toBe('dispatched');
    });

    it('refuses a video whose dispatch never reached a workflow', async () => {
      await outbox('20', 1, 'rejected');
      expect((await svc.run(body('resume', ['20']))).skipped[0].reason).toContain('không có lần chạy');
    });
  });

  describe('rerun', () => {
    beforeEach(async () => {
      await video('10');
    });

    it('creates the generation after the highest known one and resets the outbox row', async () => {
      await outbox('10', 3, 'dispatched');
      await run('10', 3, 'failed');
      await run('10', 7, 'failed'); // canary generation the outbox never learned about
      await pool.query(`UPDATE localization_outbox SET workflow_id = 'video-10-g3', dispatch_attempts = 4, last_error = 'x' WHERE aweme_id = '10'`);
      const res = await svc.run(body('rerun', ['10']));
      expect(res).toEqual({ done: ['10'], skipped: [] });
      expect(await one('SELECT generation, state, dispatch_attempts, last_error, workflow_id FROM localization_outbox')).toEqual({
        generation: 8,
        state: 'pending',
        dispatch_attempts: 0,
        last_error: null,
        workflow_id: null,
      });
      expect(await logged('10')).toEqual(['rerun']);
    });

    it('starts a returned video over in a new generation', async () => {
      await outbox('10', 2, 'dispatched');
      await run('10', 2, 'ready_for_review');
      await status('10', 2, 'rev-1');
      expect((await svc.run(body('return', ['10']))).done).toEqual(['10']);
      expect((await svc.run(body('rerun', ['10']))).done).toEqual(['10']);
      expect(await one('SELECT generation, state FROM localization_outbox')).toEqual({ generation: 3, state: 'pending' });
      expect(await logged('10')).toEqual(['return', 'rerun']);
    });

    it('works from a dead outbox row with no run', async () => {
      await outbox('10', 1, 'dead');
      expect((await svc.run(body('rerun', ['10']))).done).toEqual(['10']);
      expect((await one('SELECT generation FROM localization_outbox'))?.generation).toBe(2);
    });

    it('refuses while a workflow is running, even one the outbox does not know', async () => {
      await outbox('10', 3, 'dispatched');
      temporal.open = ['video-10-g5'];
      const res = await svc.run(body('rerun', ['10']));
      expect(res.done).toEqual([]);
      expect(res.skipped[0].reason).toContain('đang chạy');
      expect((await one('SELECT generation FROM localization_outbox'))?.generation).toBe(3);
    });

    it('refuses when the current workflow is still running by id', async () => {
      await outbox('10', 3, 'dispatched');
      temporal.statuses.set('video-10-g3', 'running');
      expect((await svc.run(body('rerun', ['10']))).skipped[0].reason).toContain('đang chạy');
    });

    it('refuses when Temporal cannot be read', async () => {
      await outbox('10', 3, 'dispatched');
      temporal.down = true;
      const res = await svc.run(body('rerun', ['10']));
      expect(res.skipped).toEqual([{ aweme_id: '10', reason: 'Temporal không đọc được' }]);
    });

    it('refuses a rejected outbox row with the reason', async () => {
      await outbox('10', 1, 'rejected');
      expect((await svc.run(body('rerun', ['10']))).skipped).toEqual([
        { aweme_id: '10', reason: 'Input hỏng, chạy lại không giúp được' },
      ]);
    });

    it('refuses held, pending and a video with no outbox row', async () => {
      await outbox('10', 1, 'held');
      expect((await svc.run(body('rerun', ['10']))).done).toEqual([]);
      await pool.query(`UPDATE localization_outbox SET state = 'pending'`);
      expect((await svc.run(body('rerun', ['10']))).done).toEqual([]);
      await video('11');
      expect((await svc.run(body('rerun', ['11']))).done).toEqual([]);
    });

    it('refuses while the final render is rendering', async () => {
      await outbox('10', 1, 'dispatched');
      await run('10', 1, 'ready_for_review');
      await pool.query(
        `INSERT INTO localization.final_renders (aweme_id, generation, approved_revision, state) VALUES ('10', 1, 'r', 'rendering')`,
      );
      expect((await svc.run(body('rerun', ['10']))).skipped[0].reason).toContain('render');
    });

    it('two parallel reruns take effect once', async () => {
      await outbox('10', 3, 'dispatched');
      await run('10', 3, 'failed');
      const [a, b] = await Promise.all([svc.run(body('rerun', ['10'])), svc.run(body('rerun', ['10']))]);
      expect(a.done.length + b.done.length).toBe(1);
      expect((await one('SELECT generation FROM localization_outbox'))?.generation).toBe(4);
      expect(await logged('10')).toEqual(['rerun']);
    });

    it('does nothing when the outbox generation changed after it was read', async () => {
      await outbox('10', 3, 'dispatched');
      const repo = new DashboardRepository(pool);
      const racing = new DashboardActionsService(pool, drizzle(pool, { schema }), {
        video: async (id: string) => {
          const rec = await repo.video(id);
          await pool.query(`UPDATE localization_outbox SET generation = 4 WHERE aweme_id = $1`, [id]);
          return rec;
        },
      }, temporal);
      const res = await racing.run(body('rerun', ['10']));
      expect(res.done).toEqual([]);
      expect(res.skipped[0].reason).toContain('vừa đổi');
      expect((await one('SELECT generation, state FROM localization_outbox'))).toEqual({ generation: 4, state: 'dispatched' });
    });
  });

  describe('approve', () => {
    beforeEach(async () => {
      await video('20');
      await outbox('20', 1, 'dispatched');
      await run('20', 1, 'ready_for_review');
      await status('20', 1, 'rev-1');
    });
    const approve = (rest: Partial<ActionBody> = {}) => svc.run(body('approve', ['20'], { revision: 'rev-1', ...rest }));

    it('writes the decision and queues the final render together', async () => {
      expect((await approve({ note: 'ok' })).done).toEqual(['20']);
      expect(await one('SELECT decision, approved_revision, note FROM review_decisions')).toEqual({
        decision: 'approved',
        approved_revision: 'rev-1',
        note: 'ok',
      });
      expect(await one('SELECT state, approved_revision, attempts, error FROM localization.final_renders')).toEqual({
        state: 'queued',
        approved_revision: 'rev-1',
        attempts: 0,
        error: null,
      });
      expect(await logged('20')).toEqual(['approve']);
    });

    it('works for needs_human runs', async () => {
      await pool.query(`UPDATE localization.runs SET status = 'needs_human'`);
      expect((await approve()).done).toEqual(['20']);
    });

    it('refuses a revision other than the one in review_status', async () => {
      const res = await approve({ revision: 'rev-0' });
      expect(res.done).toEqual([]);
      expect(res.skipped[0].reason).toContain('đã đổi');
      expect(await one('SELECT 1 FROM review_decisions')).toBeUndefined();
    });

    it('refuses a dirty review but accepts a null dirty flag', async () => {
      await pool.query(`UPDATE localization.review_status SET dirty = true`);
      expect((await approve()).skipped[0].reason).toContain('chưa lưu');
      await pool.query(`UPDATE localization.review_status SET dirty = NULL`);
      expect((await approve()).done).toEqual(['20']);
    });

    it('refuses mismatches unless confirmed, and records the confirmation', async () => {
      await pool.query(`UPDATE localization.review_status SET mismatch_count = 2`);
      expect((await approve()).skipped[0].reason).toContain('mismatch');
      expect((await approve({ confirm_mismatch: true })).done).toEqual(['20']);
      expect((await one('SELECT confirm_mismatch FROM review_decisions'))?.confirm_mismatch).toBe(true);
    });

    it('refuses without a review status row, without a project, and for runs not awaiting review', async () => {
      await pool.query(`DELETE FROM localization.review_status`);
      expect((await approve()).done).toEqual([]);
      await status('20', 1, 'rev-1');
      await pool.query(`UPDATE localization.runs SET project_key = NULL`);
      expect((await approve()).skipped[0].reason).toContain('project');
      await pool.query(`UPDATE localization.runs SET project_key = 'p', status = 'failed'`);
      expect((await approve()).done).toEqual([]);
    });

    it('a second click or tab does not requeue an approved or rendered video', async () => {
      expect((await approve()).done).toEqual(['20']);
      await pool.query(`UPDATE localization.final_renders SET state = 'rendered', final_key = 'k'`);
      const again = await approve();
      expect(again.done).toEqual([]);
      expect((await one('SELECT state FROM localization.final_renders'))?.state).toBe('rendered');
      expect(await logged('20')).toEqual(['approve']);
    });

    it('parallel approvals take effect once', async () => {
      const [a, b] = await Promise.all([approve(), approve()]);
      expect(a.done.length + b.done.length).toBe(1);
      expect(await logged('20')).toEqual(['approve']);
    });

    it('does not touch a render that is already rendering', async () => {
      await pool.query(
        `INSERT INTO localization.final_renders (aweme_id, generation, approved_revision, state) VALUES ('20', 1, 'rev-0', 'rendering')`,
      );
      expect((await approve()).done).toEqual([]);
      expect((await one('SELECT state, approved_revision FROM localization.final_renders'))).toEqual({
        state: 'rendering',
        approved_revision: 'rev-0',
      });
    });
  });

  describe('review findings', () => {
    beforeEach(async () => {
      await video('80');
      await outbox('80', 1, 'dispatched');
      await run('80', 1, 'ready_for_review');
      await status('80', 1, 'rev-2');
    });

    it('approving a new revision over a rendered older one queues the new revision', async () => {
      await pool.query(
        `INSERT INTO review_decisions (aweme_id, generation, decision, approved_revision) VALUES ('80', 1, 'approved', 'rev-2')`,
      );
      await pool.query(
        `INSERT INTO localization.final_renders (aweme_id, generation, approved_revision, state, final_key)
         VALUES ('80', 1, 'rev-1', 'rendered', 'output/review/80/g1/final.mp4')`,
      );
      // Decision already says rev-2 but the render holds rev-1: a re-approve must repair it.
      const res = await svc.run(body('approve', ['80'], { revision: 'rev-2' }));
      expect(res.done).toEqual(['80']);
      expect(await one('SELECT state, approved_revision FROM localization.final_renders')).toEqual({
        state: 'queued',
        approved_revision: 'rev-2',
      });
    });

    it('rolls the decision back when the render row is claimed after the check', async () => {
      // A worker claim between the locked read and the upsert, modelled by a trigger.
      await pool.query(
        `CREATE OR REPLACE FUNCTION p4_claim() RETURNS trigger LANGUAGE plpgsql AS $$
         BEGIN
           UPDATE localization.final_renders SET state = 'rendering' WHERE aweme_id = NEW.aweme_id;
           RETURN NEW;
         END $$`,
      );
      await pool.query(
        `INSERT INTO localization.final_renders (aweme_id, generation, approved_revision, state) VALUES ('80', 1, 'rev-1', 'failed')`,
      );
      await pool.query(
        `CREATE TRIGGER p4_claim_trg AFTER INSERT ON review_decisions FOR EACH ROW EXECUTE FUNCTION p4_claim()`,
      );
      try {
        const res = await svc.run(body('approve', ['80'], { revision: 'rev-2' }));
        expect(res.skipped).toEqual([{ aweme_id: '80', reason: 'Đang render bản cuối.' }]);
        expect(await one('SELECT 1 FROM review_decisions')).toBeUndefined();
        expect((await one('SELECT state FROM localization.final_renders'))?.state).toBe('failed');
      } finally {
        await pool.query(`DROP TRIGGER p4_claim_trg ON review_decisions`);
      }
    });

    it('refuses to return a published video and leaves its render and publication alone', async () => {
      await svc.run(body('approve', ['80'], { revision: 'rev-2' }));
      await pool.query(`UPDATE localization.final_renders SET state = 'rendered', final_key = 'k'`);
      await svc.run(body('mark_published', ['80']));
      const res = await svc.run(body('return', ['80']));
      expect(res.skipped[0].reason).toContain('đã đăng');
      expect((await one('SELECT state FROM localization.final_renders'))?.state).toBe('rendered');
      expect((await one('SELECT state FROM publications'))?.state).toBe('published');
    });

    it('mark_published twice takes effect once and keeps platform and link', async () => {
      await svc.run(body('approve', ['80'], { revision: 'rev-2' }));
      await pool.query(`UPDATE localization.final_renders SET state = 'rendered', final_key = 'k'`);
      const first = await svc.run(body('mark_published', ['80'], { platform: 'tiktok', post_url: 'https://t.co/1' }));
      const second = await svc.run(body('mark_published', ['80']));
      expect(first.done).toEqual(['80']);
      expect(second.done).toEqual([]);
      expect(await one('SELECT platform, post_url FROM publications')).toEqual({ platform: 'tiktok', post_url: 'https://t.co/1' });
      expect(await logged('80')).toEqual(['approve', 'mark_published']);
    });

    it('skips an action pinned to a generation that is no longer current', async () => {
      const res = await svc.run(body('approve', ['80'], { revision: 'rev-2', generation: 0 }));
      expect(res.skipped[0].reason).toContain('vừa đổi');
      expect(await one('SELECT 1 FROM review_decisions')).toBeUndefined();
      expect((await svc.run(body('approve', ['80'], { revision: 'rev-2', generation: 1 }))).done).toEqual(['80']);
    });

    it('after one Temporal failure the rest of a bulk rerun skips at once', async () => {
      for (const id of ['81', '82']) {
        await video(id);
        await outbox(id, 1, 'dispatched');
      }
      let calls = 0;
      temporal.workflowStatus = async () => {
        calls += 1;
        throw new Error('describe hung');
      };
      const res = await svc.run(body('rerun', ['81', '82']));
      expect(calls).toBe(1);
      expect(res.skipped.map((x) => x.reason)).toEqual(['Temporal không đọc được', 'Temporal không đọc được']);
    });

    it('names the reason when the run is not awaiting review or has no review status', async () => {
      await pool.query(`DELETE FROM localization.review_status`);
      expect((await svc.run(body('approve', ['80'], { revision: 'rev-2' }))).skipped[0].reason).toBe(
        'Chưa có bản review để duyệt.',
      );
      await pool.query(`UPDATE localization.runs SET status = 'failed'`);
      expect((await svc.run(body('return', ['80']))).skipped[0].reason).toContain('không ở trạng thái chờ duyệt');
    });
  });

  describe('return', () => {
    beforeEach(async () => {
      await video('30');
      await outbox('30', 1, 'dispatched');
      await run('30', 1, 'ready_for_review');
      await status('30', 1, 'rev-1');
    });

    it('returns a video awaiting review with the note', async () => {
      expect((await svc.run(body('return', ['30'], { note: 'sửa cue 3' }))).done).toEqual(['30']);
      expect(await one('SELECT decision, note, approved_revision FROM review_decisions')).toEqual({
        decision: 'returned',
        note: 'sửa cue 3',
        approved_revision: null,
      });
    });

    it('returns an approved video and drops its queued or rendered render', async () => {
      await svc.run(body('approve', ['30'], { revision: 'rev-1' }));
      expect((await svc.run(body('return', ['30']))).done).toEqual(['30']);
      expect(await one('SELECT 1 FROM localization.final_renders')).toBeUndefined();
    });

    it('keeps a render that is rendering and refuses', async () => {
      await svc.run(body('approve', ['30'], { revision: 'rev-1' }));
      await pool.query(`UPDATE localization.final_renders SET state = 'rendering'`);
      const res = await svc.run(body('return', ['30']));
      expect(res.done).toEqual([]);
      expect((await one('SELECT state FROM localization.final_renders'))?.state).toBe('rendering');
      expect((await one('SELECT decision FROM review_decisions'))?.decision).toBe('approved');
    });

    it('refuses a second return and a run that is not awaiting review', async () => {
      await svc.run(body('return', ['30']));
      expect((await svc.run(body('return', ['30']))).done).toEqual([]);
      await pool.query(`UPDATE localization.runs SET status = 'failed'`);
      await pool.query(`DELETE FROM review_decisions`);
      expect((await svc.run(body('return', ['30']))).done).toEqual([]);
    });
  });

  describe('rerender', () => {
    beforeEach(async () => {
      await video('40');
      await outbox('40', 1, 'dispatched');
      await run('40', 1, 'ready_for_review');
      await status('40', 1, 'rev-1');
      await pool.query(
        `INSERT INTO review_decisions (aweme_id, generation, decision, approved_revision) VALUES ('40', 1, 'approved', 'rev-1')`,
      );
    });
    const render = (state: string) =>
      pool.query(
        `INSERT INTO localization.final_renders (aweme_id, generation, approved_revision, state, attempts, error)
         VALUES ('40', 1, 'rev-1', $1, 3, 'boom')
         ON CONFLICT (aweme_id, generation) DO UPDATE SET state = $1`,
        [state],
      );

    it.each(['failed', 'rendered'])('re-queues a %s render and clears the error', async (state) => {
      await render(state);
      expect((await svc.run(body('rerender', ['40']))).done).toEqual(['40']);
      expect(await one('SELECT state, attempts, error FROM localization.final_renders')).toEqual({
        state: 'queued',
        attempts: 0,
        error: null,
      });
    });

    it.each(['queued', 'rendering'])('refuses a %s render', async (state) => {
      await render(state);
      expect((await svc.run(body('rerender', ['40']))).done).toEqual([]);
    });

    it('refuses when the decision is no longer approved or no render exists', async () => {
      expect((await svc.run(body('rerender', ['40']))).done).toEqual([]);
      await render('failed');
      await pool.query(`UPDATE review_decisions SET decision = 'returned'`);
      expect((await svc.run(body('rerender', ['40']))).done).toEqual([]);
    });
  });

  describe('mark_published / mark_unpublished', () => {
    beforeEach(async () => {
      await video('50');
      await outbox('50', 1, 'dispatched');
    });
    const rendered = async () => {
      await run('50', 1, 'ready_for_review');
      await pool.query(
        `INSERT INTO review_decisions (aweme_id, generation, decision, approved_revision) VALUES ('50', 1, 'approved', 'r')`,
      );
      await pool.query(
        `INSERT INTO localization.final_renders (aweme_id, generation, approved_revision, state, final_key)
         VALUES ('50', 1, 'r', 'rendered', 'output/review/50/g1/final.mp4')`,
      );
    };

    it('refuses before the video is rendered', async () => {
      await run('50', 1, 'ready_for_review');
      const res = await svc.run(body('mark_published', ['50']));
      expect(res.done).toEqual([]);
      expect(await one('SELECT 1 FROM publications')).toBeUndefined();
    });

    it('publishes and unpublishes a rendered video, keeping platform and link', async () => {
      await rendered();
      const published = await svc.run(
        body('mark_published', ['50'], { platform: 'tiktok', post_url: 'https://tiktok.com/@a/video/1', note: 'đã đăng' }),
      );
      expect(published.done).toEqual(['50']);
      expect(await one('SELECT state, platform, post_url, note FROM publications')).toEqual({
        state: 'published',
        platform: 'tiktok',
        post_url: 'https://tiktok.com/@a/video/1',
        note: 'đã đăng',
      });
      expect((await svc.run(body('mark_unpublished', ['50']))).done).toEqual(['50']);
      expect(await one('SELECT state, platform FROM publications')).toEqual({ state: 'unpublished', platform: 'tiktok' });
      expect((await svc.run(body('mark_unpublished', ['50']))).done).toEqual([]);
      expect(await logged('50')).toEqual(['mark_published', 'mark_unpublished']);
    });

    it('accepts a legacy run that has an output key and no render row', async () => {
      await run('50', 1, 'published', null, 'output/50.mp4');
      expect((await svc.run(body('mark_published', ['50']))).done).toEqual(['50']);
    });
  });

  describe('open_studio', () => {
    beforeEach(async () => {
      await video('60');
      await outbox('60', 1, 'dispatched');
    });

    it('queues one request for a video with a review project and not a duplicate', async () => {
      await run('60', 1, 'ready_for_review');
      expect((await svc.run(body('open_studio', ['60']))).done).toEqual(['60']);
      expect(await one('SELECT aweme_id, generation, state FROM studio_open_requests')).toEqual({
        aweme_id: '60',
        generation: 1,
        state: 'pending',
      });
      expect((await svc.run(body('open_studio', ['60']))).done).toEqual([]);
      expect((await pool.query('SELECT 1 FROM studio_open_requests')).rowCount).toBe(1);
    });

    it('refuses a video with no review project', async () => {
      await run('60', 1, 'failed', null);
      const res = await svc.run(body('open_studio', ['60']));
      expect(res.done).toEqual([]);
      expect(await one('SELECT 1 FROM studio_open_requests')).toBeUndefined();
    });
  });

  describe('bulk', () => {
    it('handles many videos independently and reports each one', async () => {
      // A video is in flight, so the sequential queue leaves both held rows alone.
      await pool.query(`INSERT INTO auto_queue_inflight (aweme_id, generation) VALUES ('0', 1)`);
      for (const id of ['71', '72', '73']) await video(id);
      await outbox('71', 1, 'held');
      await outbox('72', 1, 'dispatched');
      await outbox('73', 1, 'held');
      const res = await svc.run(body('localize', ['71', '72', '73', '71']));
      expect(res.done).toEqual(['71', '73']);
      expect(res.skipped.map((s) => s.aweme_id)).toEqual(['72']);
      await pool.query('DELETE FROM auto_queue_inflight');
    });

    it('reports an unknown video as skipped', async () => {
      const res = await svc.run(body('approve', ['999'], { revision: 'r' }));
      expect(res.skipped).toEqual([{ aweme_id: '999', reason: 'Không có video này.' }]);
    });
  });
});
