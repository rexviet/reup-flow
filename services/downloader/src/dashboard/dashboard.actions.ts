import { Inject, Injectable, Logger } from '@nestjs/common';
import type { Pool, PoolClient } from 'pg';
import type { Executor } from '../db/executor';
import { DB, PG_POOL } from '../db/db.module';
import type { DashboardTemporal } from '../workspace/workspace.dashboard-reader';
import { DASHBOARD_TEMPORAL } from '../workspace/workspace.tokens';
import { DashboardRepository, stageFactsOf, type VideoRecord } from './dashboard.repository';
import type { ActionBody } from './dashboard.schemas';
import { computeStage, currentGeneration, type OpenGeneration } from './dashboard.stage';
import { localize } from './intake.repository';

export interface ActionSkip {
  aweme_id: string;
  reason: string;
}

export interface ActionResult {
  done: string[];
  skipped: ActionSkip[];
}

const log = new Logger('DashboardActions');

const AWAITING = ['ready_for_review', 'needs_human'];
const TEMPORAL_TIMEOUT_MS = 5000;

const SKIP = {
  notFound: 'Không có video này.',
  noGeneration: 'Video chưa có lượt xử lý.',
  changed: 'Trạng thái vừa đổi, hãy tải lại.',
  temporalDown: 'Temporal không đọc được',
  rejected: 'Input hỏng, chạy lại không giúp được',
} as const;

/** Thrown inside a transaction to roll it back and report why nothing was done. */
class Skip extends Error {}

function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  let timer: NodeJS.Timeout;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error('timed out')), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

/**
 * Runs each requested video in its own transaction, with the precondition in the `WHERE`
 * of the write (or under a row lock), so two clicks or two tabs cannot both take effect.
 * Every action targets the current generation computed the same way the read API does.
 */
@Injectable()
export class DashboardActionsService {
  constructor(
    @Inject(PG_POOL) private readonly pool: Pool,
    @Inject(DB) private readonly db: Executor,
    @Inject(DashboardRepository) private readonly repo: Pick<DashboardRepository, 'video'>,
    @Inject(DASHBOARD_TEMPORAL) private readonly temporal: DashboardTemporal,
  ) {}

  async run(body: ActionBody): Promise<ActionResult> {
    const ids = [...new Set(body.aweme_ids)];
    const result: ActionResult = { done: [], skipped: [] };
    // Temporal is read at most once per request, and only when an action needs it.
    let open: Promise<Map<string, OpenGeneration[]> | undefined> | undefined;
    const state = { temporalFailed: false };
    const openWorkflows = (): Promise<Map<string, OpenGeneration[]> | undefined> => {
      open ??= withTimeout(this.temporal.listOpenLocalizationIds(), TEMPORAL_TIMEOUT_MS)
        .then((listed) => {
          const byVideo = new Map<string, OpenGeneration[]>();
          for (const { workflowId } of listed) {
            const m = /^video-(\d+)-g(\d+)$/.exec(workflowId);
            if (!m) continue;
            byVideo.set(m[1], [...(byVideo.get(m[1]) ?? []), { generation: Number(m[2]), pending: undefined }]);
          }
          return byVideo;
        })
        .catch((err: unknown) => {
          log.warn(`temporal read failed: ${err instanceof Error ? err.message : 'unknown error'}`);
          return undefined;
        });
      return open;
    };

    for (const awemeId of ids) {
      try {
        const reason = await this.one(body, awemeId, openWorkflows, state);
        if (reason === null) result.done.push(awemeId);
        else result.skipped.push({ aweme_id: awemeId, reason });
      } catch (err) {
        log.error(`${body.action} ${awemeId} failed: ${err instanceof Error ? err.message : 'unknown error'}`);
        result.skipped.push({ aweme_id: awemeId, reason: 'Lỗi hệ thống, thử lại sau.' });
      }
    }
    return result;
  }

  /** `null` means done; a string is why the video was skipped. */
  private async one(
    body: ActionBody,
    awemeId: string,
    openWorkflows: () => Promise<Map<string, OpenGeneration[]> | undefined>,
    state: { temporalFailed: boolean },
  ): Promise<string | null> {
    if (body.action === 'localize') return this.localize(awemeId);
    if (body.action === 'retry_download') return this.retryDownload(awemeId);

    const record = await this.repo.video(awemeId);
    if (!record) return SKIP.notFound;
    const facts = stageFactsOf(record);
    const all = await openWorkflows();
    const open: OpenGeneration[] | undefined = all === undefined ? undefined : (all.get(awemeId) ?? []);
    const openKnown = open !== undefined;
    const gen = currentGeneration(facts, open);
    if (gen === null) return SKIP.noGeneration;
    if (body.generation !== undefined && body.generation !== gen) return SKIP.changed;

    switch (body.action) {
      case 'resume':
        return this.resume(record, gen, openKnown, open ?? [], state);
      case 'rerun':
        return this.rerun(record, gen, openKnown, open ?? [], state);
      case 'approve':
        return this.tx((c) => this.approve(c, awemeId, gen, body));
      case 'return':
        return this.tx((c) => this.returnToEditor(c, awemeId, gen, body));
      case 'rerender':
        return this.tx((c) => this.rerender(c, awemeId, gen));
      case 'mark_published':
      case 'mark_unpublished': {
        const stage = computeStage(facts, open).stage;
        if (stage !== 'rendered' && stage !== 'published') return 'Video chưa render xong.';
        return this.tx((c) => this.publication(c, awemeId, gen, body));
      }
      case 'open_studio':
        return this.tx((c) => this.openStudio(c, awemeId, gen));
      default:
        return 'Thao tác không hợp lệ.';
    }
  }

  private async tx(work: (client: PoolClient) => Promise<void>): Promise<string | null> {
    const client = await this.pool.connect();
    let broken: Error | undefined;
    try {
      await client.query('BEGIN');
      try {
        await work(client);
        await client.query('COMMIT');
        return null;
      } catch (err) {
        await client.query('ROLLBACK');
        if (err instanceof Skip) return err.message;
        throw err;
      }
    } catch (err) {
      broken = err instanceof Error ? err : new Error('transaction failed');
      throw err;
    } finally {
      // A connection that could not even roll back is discarded, not reused.
      client.release(broken);
    }
  }

  private async localize(awemeId: string): Promise<string | null> {
    const r = await localize(this.db, [awemeId]);
    if (r.localized.length > 0) return null;
    const skip = r.skipped[0];
    if (skip?.reason === 'no_outbox_row') return 'Video chưa tải xong.';
    return `Video không ở trạng thái giữ (hiện: ${skip?.state ?? 'không rõ'}).`;
  }

  /**
   * Puts a video whose download failed back in the download lane's queue, with a fresh set of
   * attempts (the same reset the channel-wide retry does). The condition is in the `WHERE`, so a
   * second click, or a video that is already downloading or done, changes nothing. When the
   * download finishes it gets its outbox row the usual way.
   */
  private async retryDownload(awemeId: string): Promise<string | null> {
    return this.tx(async (c) => {
      const moved = await c.query(
        `UPDATE videos
            SET status = 'pending', attempts = 0, last_error = NULL, lease_expires_at = NULL
          WHERE aweme_id = $1 AND status = 'failed'`,
        [awemeId],
      );
      if (moved.rowCount === 1) {
        await this.log(c, awemeId, 'retry_download', {});
        return;
      }
      const now = await c.query<{ status: string }>(`SELECT status FROM videos WHERE aweme_id = $1`, [awemeId]);
      throw new Skip(
        now.rowCount === 0 ? SKIP.notFound : `Video không ở trạng thái tải lỗi (hiện: ${now.rows[0].status}).`,
      );
    });
  }

  /** `null` when nothing of this generation is running or rendering, else why it cannot be touched. */
  private async noLiveWork(
    record: VideoRecord,
    gen: number,
    openKnown: boolean,
    open: OpenGeneration[],
    state: { temporalFailed: boolean },
  ): Promise<string | null> {
    // One failed describe makes the rest of a bulk request skip at once, not wait 5s each.
    if (!openKnown || state.temporalFailed) return SKIP.temporalDown;
    if (open.length > 0) return 'Workflow đang chạy.';

    let status: Awaited<ReturnType<DashboardTemporal['workflowStatus']>>;
    try {
      status = await withTimeout(this.temporal.workflowStatus(`video-${record.awemeId}-g${gen}`), TEMPORAL_TIMEOUT_MS);
    } catch {
      state.temporalFailed = true;
      return SKIP.temporalDown;
    }
    if (status === 'running') return 'Workflow đang chạy.';
    if (record.renders.some((r) => r.generation === gen && r.state === 'rendering')) {
      return 'Đang render bản cuối.';
    }
    return null;
  }

  /**
   * Goes on from the step that failed instead of starting over. It only puts the outbox row back
   * to `pending` under the same generation; the starter sees that the workflow id already exists
   * as a failed run and has Temporal reset it to the failed step (this service never writes to
   * Temporal). Only a run that recorded `failed` can be gone on from: it keeps the steps that
   * finished, which "Chạy lại" (a new generation) does not.
   */
  private async resume(
    record: VideoRecord,
    gen: number,
    openKnown: boolean,
    open: OpenGeneration[],
    state: { temporalFailed: boolean },
  ): Promise<string | null> {
    const outbox = record.outbox;
    if (!outbox || outbox.state !== 'dispatched') return 'Video không có lần chạy nào để chạy tiếp.';
    if (outbox.generation !== gen) return SKIP.changed;
    const failed = record.runs.some((r) => r.generation === gen && r.status === 'failed');
    if (!failed) return 'Chỉ chạy tiếp được lần chạy đã thất bại.';
    const blocked = await this.noLiveWork(record, gen, openKnown, open, state);
    if (blocked !== null) return blocked;

    return this.tx(async (c) => {
      const moved = await c.query(
        `UPDATE localization_outbox
            SET state = 'pending', next_attempt_at = now(), dispatch_attempts = 0, last_error = NULL
          WHERE aweme_id = $1 AND generation = $2 AND state = 'dispatched'`,
        [record.awemeId, gen],
      );
      if (moved.rowCount !== 1) throw new Skip(SKIP.changed);
      await this.log(c, record.awemeId, 'resume', { generation: gen });
    });
  }

  private async rerun(
    record: VideoRecord,
    gen: number,
    openKnown: boolean,
    open: OpenGeneration[],
    state: { temporalFailed: boolean },
  ): Promise<string | null> {
    const outbox = record.outbox;
    if (!outbox) return 'Video chưa vào hàng đợi localization.';
    if (outbox.state === 'rejected') return SKIP.rejected;
    if (outbox.state === 'held') return 'Video đang được giữ, hãy bấm Localize.';
    if (outbox.state === 'pending') return 'Video đã đang chờ chạy.';
    if (outbox.state !== 'dispatched' && outbox.state !== 'dead') return `Trạng thái ${outbox.state} không chạy lại được.`;
    const blocked = await this.noLiveWork(record, gen, openKnown, open, state);
    if (blocked !== null) return blocked;

    const readGeneration = outbox.generation;
    return this.tx(async (c) => {
      const moved = await c.query(
        `UPDATE localization_outbox
            SET generation = $2, state = 'pending', next_attempt_at = now(),
                dispatch_attempts = 0, last_error = NULL, workflow_id = NULL
          WHERE aweme_id = $1 AND generation = $3 AND state IN ('dispatched', 'dead')`,
        [record.awemeId, gen + 1, readGeneration],
      );
      if (moved.rowCount !== 1) throw new Skip(SKIP.changed);
      await this.log(c, record.awemeId, 'rerun', { from_generation: gen, to_generation: gen + 1 });
    });
  }

  private log(c: PoolClient, awemeId: string, action: string, detail: Record<string, unknown>): Promise<unknown> {
    return c.query(`INSERT INTO dashboard_actions (aweme_id, action, detail) VALUES ($1, $2, $3::jsonb)`, [
      awemeId,
      action,
      JSON.stringify(detail),
    ]);
  }

  /** Locks the run row of this generation, which serializes approve, return and open_studio on one video. */
  private async lockRun(c: PoolClient, awemeId: string, gen: number) {
    // Lock first, then read the rest: a join under `FOR UPDATE` would hand a waiting
    // transaction the other tables as they were before the lock holder committed.
    const locked = await c.query(
      `SELECT status, project_key FROM localization.runs WHERE aweme_id = $1 AND generation = $2 FOR UPDATE`,
      [awemeId, gen],
    );
    if (locked.rows.length === 0) return undefined;
    const { rows } = await c.query(
      `SELECT $3::text AS status, $4::text AS project_key, s.revision, s.mismatch_count, s.dirty,
              d.decision, d.approved_revision,
              f.state AS render_state, f.approved_revision AS render_revision
         FROM (SELECT $1::text AS aweme_id, $2::int AS generation) k
         LEFT JOIN localization.review_status s ON s.aweme_id = k.aweme_id AND s.generation = k.generation
         LEFT JOIN review_decisions d ON d.aweme_id = k.aweme_id AND d.generation = k.generation
         LEFT JOIN localization.final_renders f ON f.aweme_id = k.aweme_id AND f.generation = k.generation`,
      [awemeId, gen, locked.rows[0].status, locked.rows[0].project_key],
    );
    return rows[0] as
      | {
          status: string;
          project_key: string | null;
          revision: string | null;
          mismatch_count: number | null;
          dirty: boolean | null;
          decision: string | null;
          approved_revision: string | null;
          render_state: string | null;
          render_revision: string | null;
        }
      | undefined;
  }

  private async approve(c: PoolClient, awemeId: string, gen: number, body: ActionBody): Promise<void> {
    const run = await this.lockRun(c, awemeId, gen);
    if (!run || !AWAITING.includes(run.status)) throw new Skip('Video không ở trạng thái chờ duyệt.');
    if (run.project_key === null) throw new Skip('Chưa có project review.');
    if (run.revision === null) throw new Skip('Chưa có bản review để duyệt.');
    if (run.dirty === true) throw new Skip('Review còn thay đổi chưa lưu.');
    if (run.revision !== body.revision) throw new Skip('Bản review đã đổi, hãy tải lại rồi duyệt.');
    if ((run.mismatch_count ?? 0) > 0 && body.confirm_mismatch !== true) {
      throw new Skip('Còn cue lệch, cần xác nhận mismatch.');
    }
    if (run.render_state === 'rendering') throw new Skip('Đang render bản cuối.');
    if (
      run.decision === 'approved' &&
      run.approved_revision === body.revision &&
      run.render_revision === body.revision
    ) {
      throw new Skip('Bản này đã được duyệt.');
    }

    await c.query(
      `INSERT INTO review_decisions (aweme_id, generation, decision, approved_revision, confirm_mismatch, note, decided_at)
       VALUES ($1, $2, 'approved', $3, $4, $5, now())
       ON CONFLICT (aweme_id, generation) DO UPDATE
         SET decision = 'approved', approved_revision = EXCLUDED.approved_revision,
             confirm_mismatch = EXCLUDED.confirm_mismatch, note = EXCLUDED.note, decided_at = now()`,
      [awemeId, gen, body.revision, body.confirm_mismatch === true, body.note ?? null],
    );
    const queued = await c.query(
      `INSERT INTO localization.final_renders (aweme_id, generation, approved_revision, state, attempts, error)
       VALUES ($1, $2, $3, 'queued', 0, NULL)
       ON CONFLICT (aweme_id, generation) DO UPDATE
         SET approved_revision = EXCLUDED.approved_revision, state = 'queued', attempts = 0, error = NULL,
             claim = NULL, lease_expires_at = NULL
       WHERE localization.final_renders.state <> 'rendering'`,
      [awemeId, gen, body.revision],
    );
    // The render worker does not take the run lock; if it claimed the row since it was read,
    // roll the decision back too instead of leaving it on a revision the render is not using.
    if (queued.rowCount !== 1) throw new Skip('Đang render bản cuối.');
    await this.log(c, awemeId, 'approve', {
      generation: gen,
      revision: body.revision,
      confirm_mismatch: body.confirm_mismatch === true,
    });
  }

  private async returnToEditor(c: PoolClient, awemeId: string, gen: number, body: ActionBody): Promise<void> {
    const run = await this.lockRun(c, awemeId, gen);
    if (!run || !AWAITING.includes(run.status)) throw new Skip('Video không ở trạng thái chờ duyệt hoặc đã render.');
    if (run.decision === 'returned') throw new Skip('Đã trả lại rồi.');
    const published = await c.query(
      `SELECT 1 FROM publications WHERE aweme_id = $1 AND generation = $2 AND state = 'published'`,
      [awemeId, gen],
    );
    if (published.rows.length > 0) throw new Skip('Video đã đăng, hãy bỏ đánh dấu đã đăng trước.');
    if (run.render_state === 'rendering') throw new Skip('Đang render bản cuối.');

    await c.query(
      `INSERT INTO review_decisions (aweme_id, generation, decision, approved_revision, confirm_mismatch, note, decided_at)
       VALUES ($1, $2, 'returned', NULL, false, $3, now())
       ON CONFLICT (aweme_id, generation) DO UPDATE
         SET decision = 'returned', approved_revision = NULL, confirm_mismatch = false,
             note = EXCLUDED.note, decided_at = now()`,
      [awemeId, gen, body.note ?? null],
    );
    await c.query(
      `DELETE FROM localization.final_renders WHERE aweme_id = $1 AND generation = $2 AND state <> 'rendering'`,
      [awemeId, gen],
    );
    await this.log(c, awemeId, 'return', { generation: gen, note: body.note ?? null });
  }

  private async rerender(c: PoolClient, awemeId: string, gen: number): Promise<void> {
    const moved = await c.query(
      `UPDATE localization.final_renders f
          SET state = 'queued', attempts = 0, error = NULL, claim = NULL, lease_expires_at = NULL
        WHERE f.aweme_id = $1 AND f.generation = $2 AND f.state IN ('failed', 'rendered')
          AND EXISTS (SELECT 1 FROM review_decisions d
                       WHERE d.aweme_id = f.aweme_id AND d.generation = f.generation
                         AND d.decision = 'approved' AND d.approved_revision = f.approved_revision)`,
      [awemeId, gen],
    );
    if (moved.rowCount !== 1) throw new Skip('Chỉ render lại được bản đã duyệt đang lỗi hoặc đã render.');
    await this.log(c, awemeId, 'rerender', { generation: gen });
  }

  private async publication(c: PoolClient, awemeId: string, gen: number, body: ActionBody): Promise<void> {
    const rendered = `(EXISTS (SELECT 1 FROM localization.final_renders f
                                WHERE f.aweme_id = $1 AND f.generation = $2 AND f.state = 'rendered')
                      OR EXISTS (SELECT 1 FROM localization.runs r
                                  WHERE r.aweme_id = $1 AND r.generation = $2 AND r.output_key IS NOT NULL))`;
    if (body.action === 'mark_published') {
      const moved = await c.query(
        `INSERT INTO publications (aweme_id, generation, state, platform, post_url, note, updated_at)
         SELECT $1, $2, 'published', $3, $4, $5, now() WHERE ${rendered}
         ON CONFLICT (aweme_id, generation) DO UPDATE
           SET state = 'published',
               platform = COALESCE(EXCLUDED.platform, publications.platform),
               post_url = COALESCE(EXCLUDED.post_url, publications.post_url),
               note = COALESCE(EXCLUDED.note, publications.note), updated_at = now()
           WHERE publications.state <> 'published'`,
        [awemeId, gen, body.platform ?? null, body.post_url ?? null, body.note ?? null],
      );
      if (moved.rowCount !== 1) throw new Skip('Video chưa render xong hoặc đã được đánh dấu đã đăng.');
      await this.log(c, awemeId, 'mark_published', { generation: gen, platform: body.platform ?? null });
      return;
    }
    const moved = await c.query(
      `UPDATE publications SET state = 'unpublished', note = COALESCE($3, note), updated_at = now()
        WHERE aweme_id = $1 AND generation = $2 AND state <> 'unpublished' AND ${rendered}`,
      [awemeId, gen, body.note ?? null],
    );
    if (moved.rowCount !== 1) throw new Skip('Video chưa được đánh dấu đã đăng.');
    await this.log(c, awemeId, 'mark_unpublished', { generation: gen });
  }

  private async openStudio(c: PoolClient, awemeId: string, gen: number): Promise<void> {
    const run = await this.lockRun(c, awemeId, gen);
    if (!run || run.project_key === null) throw new Skip('Chưa có project review để mở Studio.');
    const inserted = await c.query(
      `INSERT INTO studio_open_requests (aweme_id, generation)
       SELECT $1, $2
        WHERE NOT EXISTS (SELECT 1 FROM studio_open_requests
                           WHERE aweme_id = $1 AND generation = $2 AND state = 'pending')`,
      [awemeId, gen],
    );
    if (inserted.rowCount !== 1) throw new Skip('Đã có yêu cầu mở Studio đang chờ.');
    await this.log(c, awemeId, 'open_studio', { generation: gen });
  }
}
