import { drizzle } from 'drizzle-orm/node-postgres';
import type { Pool } from 'pg';
import { DashboardActionsService } from '../../src/dashboard/dashboard.actions';
import { NotFoundException } from '@nestjs/common';
import { DashboardRepository } from '../../src/dashboard/dashboard.repository';
import { actionBodySchema, moveCollectionBodySchema, type ActionBody } from '../../src/dashboard/dashboard.schemas';
import * as schema from '../../src/db/schema';
import type { DashboardTemporal, WorkflowStatus } from '../../src/workspace/workspace.dashboard-reader';
import { createScratchDatabase, type ScratchDatabase } from '../support/localization-db';
import { channelCollection } from '../support/collections';

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
                studio_open_requests, dashboard_actions, folder_exports, collections,
                localization.runs, localization.final_renders, localization.review_status
         RESTART IDENTITY CASCADE`,
    );
    temporal = new FakeTemporal();
    svc = new DashboardActionsService(pool, drizzle(pool, { schema }), new DashboardRepository(pool), temporal);
  });

  const video = async (id: string) =>
    pool.query(
      `INSERT INTO videos (aweme_id, sec_user_id, create_time, status, collection_id) VALUES ($1, 'ch', now(), 'done', $2)`,
      [id, await channelCollection(pool, 'ch')],
    );
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
        `INSERT INTO videos (aweme_id, sec_user_id, create_time, status, attempts, last_error, lease_expires_at,
                             collection_id)
         VALUES ('81', 'ch', now(), 'failed', 5, 'cdn 403', now(), $1)`,
        [await channelCollection(pool, 'ch')],
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

  describe('save_to_folder', () => {
    const FINAL = (id: string, gen: number) => `output/review/${id}/g${gen}/final.mp4`;
    const SRT = (id: string, gen: number) => `output/review/${id}/g${gen}/final.srt`;
    const collection = async (kind: 'mix' | 'manual' | 'channel', saveSubdir: string | null, name = 'Bộ sưu tập') => {
      if (kind === 'channel') {
        const id = await channelCollection(pool, 'ch');
        await pool.query(`UPDATE collections SET save_subdir = $2, name = $3 WHERE id = $1`, [id, saveSubdir, name]);
        return id;
      }
      await channelCollection(pool, 'ch');
      const { rows } = await pool.query<{ id: string }>(
        `INSERT INTO collections (kind, external_id, sec_user_id, name, save_subdir)
         VALUES ($1, $2, $3, $4, $5) RETURNING id`,
        [kind, kind === 'mix' ? `mix-${name}` : null, kind === 'mix' ? 'ch' : null, name, saveSubdir],
      );
      return rows[0].id;
    };
    const place = (id: string, collectionId: string, episode: number | null) =>
      pool.query(`UPDATE videos SET collection_id = $2, mix_episode = $3 WHERE aweme_id = $1`, [id, collectionId, episode]);
    const rendered = (id: string, gen: number, state = 'rendered', finalKey: string | null = FINAL(id, gen), srtKey: string | null = SRT(id, gen)) =>
      pool.query(
        `INSERT INTO localization.final_renders (aweme_id, generation, approved_revision, state, final_key, srt_key)
         VALUES ($1, $2, 'rev', $3, $4, $5)`,
        [id, gen, state, finalKey, srtKey],
      );
    const exports = async (id: string) =>
      (
        await pool.query(
          `SELECT generation, collection_id, target_subdir, file_stem, final_key, srt_key, state
             FROM folder_exports WHERE aweme_id = $1 ORDER BY id`,
          [id],
        )
      ).rows;
    const save = (id: string, generation?: number) =>
      svc.run(body('save_to_folder', [id], generation === undefined ? {} : { generation }));

    it('queues the rendered final and its SRT with the folder and the file name captured', async () => {
      await video('300');
      const mix = await collection('mix', 'Phim/成名的代价');
      await place('300', mix, 3);
      await outbox('300', 1, 'dispatched');
      await run('300', 1, 'rendered');
      await rendered('300', 1);
      const res = await save('300', 1);
      expect(res).toEqual({ done: ['300'], skipped: [] });
      expect(await exports('300')).toEqual([
        {
          generation: 1,
          collection_id: mix,
          target_subdir: 'Phim/成名的代价',
          file_stem: 'E03-300',
          final_key: FINAL('300', 1),
          srt_key: SRT('300', 1),
          state: 'pending',
        },
      ]);
      expect(await logged('300')).toEqual(['save_to_folder']);
      expect((await one(`SELECT detail FROM dashboard_actions WHERE aweme_id = '300'`))?.detail).toEqual({
        generation: 1,
        collection_id: mix,
        target_subdir: 'Phim/成名的代价',
        file_stem: 'E03-300',
      });
    });

    it('uses the episode prefix only for a 合集 collection, and pads to two digits but no further', async () => {
      const mix = await collection('mix', 'Phim');
      const manual = await collection('manual', 'Tay');
      const channel = await collection('channel', 'Kênh');
      const cases: Array<[string, string, number | null, string]> = [
        ['301', mix, 7, 'E07-301'],
        ['302', mix, 123, 'E123-302'],
        ['303', mix, null, '303'],
        // Moved by hand out of its 合集: keeps its episode number, but is no longer "in a 合集".
        ['304', manual, 7, '304'],
        ['305', channel, 7, '305'],
        ['306', channel, null, '306'],
      ];
      for (const [id, collectionId, episode] of cases) {
        await video(id);
        await place(id, collectionId, episode);
        await run(id, 1, 'rendered');
        await rendered(id, 1);
      }
      for (const [id, , , stem] of cases) {
        expect(await save(id, 1)).toEqual({ done: [id], skipped: [] });
        expect((await exports(id)).map((e) => e.file_stem)).toEqual([stem]);
      }
    });

    it('refuses a generation that has no rendered final', async () => {
      await video('310');
      await place('310', await collection('channel', 'Kênh'), null);
      await run('310', 1, 'ready_for_review');
      await rendered('310', 1, 'rendering', null, null);
      const res = await save('310', 1);
      expect(res.done).toEqual([]);
      expect(res.skipped[0].reason).toContain('bản cuối');
      expect(await save('310', 2)).toMatchObject({ done: [] });
      expect(await exports('310')).toEqual([]);
      expect(await logged('310')).toEqual([]);
    });

    it('accepts an older generation that was rendered while a newer one is running', async () => {
      await video('311');
      await place('311', await collection('channel', 'Kênh'), null);
      await run('311', 1, 'rendered');
      await rendered('311', 1);
      await outbox('311', 2, 'dispatched');
      await run('311', 2, 'running');
      temporal.open = ['video-311-g2'];
      expect(await save('311', 1)).toEqual({ done: ['311'], skipped: [] });
      expect((await exports('311')).map((e) => [e.generation, e.final_key])).toEqual([[1, FINAL('311', 1)]]);
    });

    it('without a generation uses the current one', async () => {
      await video('312');
      await place('312', await collection('channel', 'Kênh'), null);
      await outbox('312', 2, 'dispatched');
      await run('312', 1, 'rendered');
      await run('312', 2, 'rendered');
      await rendered('312', 1);
      await rendered('312', 2);
      expect(await save('312')).toEqual({ done: ['312'], skipped: [] });
      expect((await exports('312')).map((e) => e.generation)).toEqual([2]);
    });

    it('takes a legacy final only when that generation recorded it, and it has no SRT', async () => {
      await video('313');
      await video('314');
      const channel = await collection('channel', 'Kênh');
      await place('313', channel, null);
      await place('314', channel, null);
      await run('313', 1, 'done', null, 'output/313.mp4');
      // The output key names another file, so it is not this video's legacy final.
      await run('314', 1, 'done', null, 'output/other.mp4');
      expect(await save('313', 1)).toEqual({ done: ['313'], skipped: [] });
      expect((await exports('313')).map((e) => [e.final_key, e.srt_key, e.file_stem])).toEqual([
        ['output/313.mp4', null, '313'],
      ]);
      expect((await save('314', 1)).done).toEqual([]);
      expect(await save('313', 2)).toMatchObject({ done: [] });
      expect(await exports('314')).toEqual([]);
    });

    it('never queues a stored key outside the generation review folder', async () => {
      await video('315');
      await place('315', await collection('channel', 'Kênh'), null);
      await run('315', 1, 'rendered');
      await rendered('315', 1, 'rendered', 'output/review/999/g1/final.mp4', 'source/secret.srt');
      expect((await save('315', 1)).done).toEqual([]);
      expect(await exports('315')).toEqual([]);
    });

    it('drops an SRT key outside the review folder but keeps a valid final', async () => {
      await video('316');
      await place('316', await collection('channel', 'Kênh'), null);
      await run('316', 1, 'rendered');
      await rendered('316', 1, 'rendered', FINAL('316', 1), '../etc/passwd');
      expect((await save('316', 1)).done).toEqual(['316']);
      expect((await exports('316')).map((e) => e.srt_key)).toEqual([null]);
    });

    it('says which collection has no folder, and queues nothing', async () => {
      await video('317');
      await place('317', await collection('manual', null, 'Gom tay'), null);
      await run('317', 1, 'rendered');
      await rendered('317', 1);
      const res = await save('317', 1);
      expect(res.done).toEqual([]);
      expect(res.skipped[0].reason).toContain('Gom tay');
      expect(res.skipped[0].reason).toContain('thư mục');
      expect(await exports('317')).toEqual([]);
    });

    it('a second click while one request is open adds no row; a new one is accepted once it settled', async () => {
      await video('318');
      await place('318', await collection('channel', 'Kênh'), null);
      await run('318', 1, 'rendered');
      await rendered('318', 1);
      expect((await save('318', 1)).done).toEqual(['318']);
      const again = await save('318', 1);
      expect(again.done).toEqual([]);
      expect(again.skipped[0].reason).toContain('đang chờ');
      await pool.query(`UPDATE folder_exports SET state = 'exporting' WHERE aweme_id = '318'`);
      expect((await save('318', 1)).done).toEqual([]);
      expect(await exports('318')).toHaveLength(1);
      expect(await logged('318')).toEqual(['save_to_folder']);
      await pool.query(`UPDATE folder_exports SET state = 'done' WHERE aweme_id = '318'`);
      expect((await save('318', 1)).done).toEqual(['318']);
      expect((await exports('318')).map((e) => e.state)).toEqual(['done', 'pending']);
    });

    it('a click racing an uncommitted request waits for it and then adds no row', async () => {
      await video('319');
      const collectionId = await collection('channel', 'Kênh');
      await place('319', collectionId, null);
      await run('319', 1, 'rendered');
      await rendered('319', 1);
      // The other click has inserted its row but not committed, so a "no open row" read
      // cannot see it; only the unique index on open requests can stop the second one.
      const other = await pool.connect();
      try {
        await other.query('BEGIN');
        await other.query(
          `INSERT INTO folder_exports (aweme_id, generation, collection_id, target_subdir, file_stem, final_key)
           VALUES ('319', 1, $1, 'Kênh', '319', $2)`,
          [collectionId, FINAL('319', 1)],
        );
        const racing = save('319', 1);
        const deadline = Date.now() + 10_000;
        for (;;) {
          const waiting = await pool.query(
            `SELECT 1 FROM pg_stat_activity
              WHERE datname = current_database() AND wait_event_type = 'Lock' AND query LIKE '%folder_exports%'`,
          );
          if (waiting.rows.length > 0) break;
          if (Date.now() > deadline) throw new Error('the second click never waited on the first');
          await new Promise((r) => setTimeout(r, 20));
        }
        await other.query('COMMIT');
        const res = await racing;
        expect(res.done).toEqual([]);
        expect(res.skipped[0].reason).toContain('đang chờ');
      } finally {
        other.release();
      }
      expect(await exports('319')).toHaveLength(1);
      expect(await logged('319')).toEqual([]);
    });

    it('reports an unknown video as skipped', async () => {
      expect(await save('999', 1)).toEqual({ done: [], skipped: [{ aweme_id: '999', reason: 'Không có video này.' }] });
    });

    it('validates the body: a generation is a whole number and names one video', () => {
      expect(actionBodySchema.safeParse({ action: 'save_to_folder', aweme_ids: ['1'], generation: 1 }).success).toBe(true);
      expect(actionBodySchema.safeParse({ action: 'save_to_folder', aweme_ids: ['1', '2'] }).success).toBe(true);
      expect(actionBodySchema.safeParse({ action: 'save_to_folder', aweme_ids: ['1'], generation: 1.5 }).success).toBe(false);
      // One generation number cannot be right for several videos.
      expect(actionBodySchema.safeParse({ action: 'save_to_folder', aweme_ids: ['1', '2'], generation: 1 }).success).toBe(false);
    });
  });

  describe('moveCollection', () => {
    const target = async (name = 'Đích') =>
      (await pool.query<{ id: string }>(`INSERT INTO collections (kind, name) VALUES ('manual', $1) RETURNING id`, [name]))
        .rows[0].id;
    const placement = async (id: string) =>
      one<{ collection_id: string; collection_manual: boolean; mix_episode: number | null }>(
        `SELECT collection_id, collection_manual, mix_episode FROM videos WHERE aweme_id = $1`,
        [id],
      );

    it('moves each video, marks it placed by hand and logs where it came from', async () => {
      await video('400');
      await video('401');
      const from = await channelCollection(pool, 'ch');
      await pool.query(`UPDATE videos SET mix_episode = 2 WHERE aweme_id = '401'`);
      const to = await target();
      const res = await svc.moveCollection({ aweme_ids: ['400', '401', '400', '999'], collection_id: to });
      expect(res).toEqual({ done: ['400', '401'], skipped: [{ aweme_id: '999', reason: 'Không có video này.' }] });
      expect(await placement('400')).toEqual({ collection_id: to, collection_manual: true, mix_episode: null });
      // The episode is kept: moving back into the 合集 would need it again.
      expect(await placement('401')).toEqual({ collection_id: to, collection_manual: true, mix_episode: 2 });
      const detail = (await one(`SELECT action, detail FROM dashboard_actions WHERE aweme_id = '400'`)) as {
        action: string;
        detail: unknown;
      };
      expect(detail).toEqual({ action: 'move_collection', detail: { from_collection_id: from, to_collection_id: to } });
      expect(await logged('999')).toEqual([]);
    });

    it('answers 404 for a collection that does not exist and changes nothing', async () => {
      await video('402');
      const before = await placement('402');
      await expect(
        svc.moveCollection({ aweme_ids: ['402'], collection_id: '00000000-0000-4000-8000-000000000000' }),
      ).rejects.toBeInstanceOf(NotFoundException);
      expect(await placement('402')).toEqual(before);
      expect(await logged('402')).toEqual([]);
    });

    it('does not touch an export already queued: it keeps the folder it captured', async () => {
      await video('403');
      const from = await channelCollection(pool, 'ch');
      await pool.query(`UPDATE collections SET save_subdir = 'Cũ' WHERE id = $1`, [from]);
      await run('403', 1, 'rendered');
      await pool.query(
        `INSERT INTO localization.final_renders (aweme_id, generation, approved_revision, state, final_key)
         VALUES ('403', 1, 'rev', 'rendered', 'output/review/403/g1/final.mp4')`,
      );
      expect((await svc.run(body('save_to_folder', ['403'], { generation: 1 }))).done).toEqual(['403']);
      await svc.moveCollection({ aweme_ids: ['403'], collection_id: await target() });
      expect((await one(`SELECT target_subdir, collection_id FROM folder_exports WHERE aweme_id = '403'`))).toEqual({
        target_subdir: 'Cũ',
        collection_id: from,
      });
    });

    it('validates the body', () => {
      const id = '00000000-0000-4000-8000-000000000000';
      expect(moveCollectionBodySchema.safeParse({ aweme_ids: ['1'], collection_id: id }).success).toBe(true);
      expect(moveCollectionBodySchema.safeParse({ aweme_ids: [], collection_id: id }).success).toBe(false);
      expect(moveCollectionBodySchema.safeParse({ aweme_ids: ['1'], collection_id: 'x' }).success).toBe(false);
      expect(moveCollectionBodySchema.safeParse({ aweme_ids: ['1'] }).success).toBe(false);
      expect(moveCollectionBodySchema.safeParse({ aweme_ids: ['1'], collection_id: id, extra: 1 }).success).toBe(false);
      expect(
        moveCollectionBodySchema.safeParse({ aweme_ids: Array.from({ length: 101 }, (_, i) => String(i)), collection_id: id })
          .success,
      ).toBe(false);
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
