import { Inject, Injectable } from '@nestjs/common';
import type { Pool } from 'pg';
import { PG_POOL } from '../db/db.module';
import { escapeLike, type VideoFilter } from './dashboard.schemas';
import type { StageFacts } from './dashboard.stage';

/**
 * Reads the downloader tables (through `search_path`) and `localization.*`
 * (qualified), which the Python side writes and `db/schema.ts` does not
 * describe, so this is plain SQL on the pool like `WorkspaceRepository`.
 * Nothing here writes.
 */

export interface RunSummary {
  generation: number;
  status: string;
  hasOutputKey: boolean;
  updatedAt: string;
  workflowId: string;
  projectKey: string | null;
  reviewRounds: number;
}

export interface DecisionRow {
  generation: number;
  decision: string;
  approvedRevision: string | null;
  confirmMismatch: boolean;
  note: string | null;
  decidedAt: string;
}

export interface RenderRow {
  generation: number;
  state: string;
  approvedRevision: string;
  attempts: number;
  error: string | null;
  finalKey: string | null;
  srtKey: string | null;
  renderedAt: string | null;
}

export interface PublicationRow {
  generation: number;
  state: string;
  platform: string | null;
  postUrl: string | null;
  note: string | null;
  updatedAt: string;
}

export interface ReviewStatusRow {
  generation: number;
  revision: string | null;
  cueCount: number | null;
  mismatchCount: number | null;
  dirty: boolean | null;
  checkedAt: string;
}

/** Everything the stage function and the list/detail views read about one video. */
export interface VideoRecord {
  awemeId: string;
  secUserId: string;
  nickname: string | null;
  autoLocalize: boolean;
  desc: string | null;
  createTime: string;
  durationMs: number | null;
  videoStatus: string;
  downloadAttempts: number;
  downloadError: string | null;
  outbox: {
    generation: number;
    state: string;
    dispatchedAt: string | null;
    workflowId: string | null;
    lastError: string | null;
    dispatchAttempts: number;
  } | null;
  runs: RunSummary[];
  decisions: DecisionRow[];
  renders: RenderRow[];
  publications: PublicationRow[];
  reviewStatus: ReviewStatusRow[];
  collection: { id: string; name: string; kind: string };
  /** The episode within its 合集; kept when the video is moved out of it by hand. */
  mixEpisode: number | null;
}

export function stageFactsOf(r: VideoRecord): StageFacts {
  return {
    videoStatus: r.videoStatus,
    outbox: r.outbox
      ? {
          generation: r.outbox.generation,
          state: r.outbox.state,
          dispatchedAtMs: r.outbox.dispatchedAt ? Date.parse(r.outbox.dispatchedAt) : null,
        }
      : null,
    runs: r.runs.map((x) => ({
      generation: x.generation,
      status: x.status,
      hasOutputKey: x.hasOutputKey,
      hasProject: x.projectKey !== null,
    })),
    decisions: r.decisions.map((d) => ({ generation: d.generation, decision: d.decision })),
    renders: r.renders.map((x) => ({ generation: x.generation, state: x.state })),
    publications: r.publications.map((p) => ({ generation: p.generation, state: p.state })),
    reviewStatus: r.reviewStatus.map((s) => ({
      generation: s.generation,
      mismatchCount: s.mismatchCount,
      dirty: s.dirty,
    })),
  };
}

export interface LlmCostRow {
  generation: number;
  stage: string;
  calls: number;
  costUsd: number;
}

export interface RunDetailRow {
  generation: number;
  verdict: unknown;
  needsHuman: boolean;
  outputKey: string | null;
  reviewKey: string | null;
}

export interface ActionRow {
  id: number;
  action: string;
  detail: unknown;
  createdAt: string;
}

export interface StudioRequestRow {
  generation: number;
  state: string;
  error: string | null;
  requestedAt: string;
  handledAt: string | null;
}

/** A request to save a video into its collection's folder; storage keys stay server side. */
export interface FolderExportRow {
  generation: number;
  state: string;
  targetSubdir: string;
  fileStem: string;
  /** The files written, once `done`. */
  files: unknown;
  error: string | null;
  requestedAt: string;
  handledAt: string | null;
}

export interface ChannelRow {
  secUserId: string;
  nickname: string | null;
  autoLocalize: boolean;
  lastSyncedAt: string | null;
  latestJob: {
    id: string;
    status: string;
    createdAt: string;
    totalVideos: number | null;
    downloadedCount: number;
    failedCount: number;
    error: string | null;
  } | null;
}

export interface BlockedJobRow {
  id: string;
  secUserId: string;
  nickname: string | null;
  status: string;
  error: string | null;
  updatedAt: string;
}

export interface HostWorkerRow {
  name: string;
  seenAtMs: number;
  info: Record<string, unknown>;
}

const SELECT_VIDEOS = `
  SELECT v.aweme_id, v.sec_user_id, v."desc", v.create_time, v.duration_ms,
         v.status AS video_status, v.attempts AS download_attempts, v.last_error AS download_error,
         v.mix_episode, c.id AS collection_id, c.name AS collection_name, c.kind AS collection_kind,
         p.nickname, COALESCE(p.auto_localize, false) AS auto_localize,
         CASE WHEN o.aweme_id IS NULL THEN NULL ELSE json_build_object(
           'generation', o.generation, 'state', o.state, 'dispatchedAt', o.dispatched_at,
           'workflowId', o.workflow_id, 'lastError', o.last_error,
           'dispatchAttempts', o.dispatch_attempts) END AS outbox,
         COALESCE((SELECT json_agg(json_build_object(
             'generation', r.generation, 'status', r.status,
             'hasOutputKey', r.output_key IS NOT NULL, 'updatedAt', r.updated_at,
             'workflowId', r.workflow_id, 'projectKey', r.project_key,
             'reviewRounds', r.review_rounds) ORDER BY r.generation)
           FROM localization.runs r WHERE r.aweme_id = v.aweme_id), '[]'::json) AS runs,
         COALESCE((SELECT json_agg(json_build_object(
             'generation', d.generation, 'decision', d.decision,
             'approvedRevision', d.approved_revision, 'confirmMismatch', d.confirm_mismatch,
             'note', d.note, 'decidedAt', d.decided_at) ORDER BY d.generation)
           FROM review_decisions d WHERE d.aweme_id = v.aweme_id), '[]'::json) AS decisions,
         COALESCE((SELECT json_agg(json_build_object(
             'generation', f.generation, 'state', f.state, 'approvedRevision', f.approved_revision,
             'attempts', f.attempts, 'error', f.error, 'finalKey', f.final_key,
             'srtKey', f.srt_key, 'renderedAt', f.rendered_at) ORDER BY f.generation)
           FROM localization.final_renders f WHERE f.aweme_id = v.aweme_id), '[]'::json) AS renders,
         COALESCE((SELECT json_agg(json_build_object(
             'generation', b.generation, 'state', b.state, 'platform', b.platform,
             'postUrl', b.post_url, 'note', b.note, 'updatedAt', b.updated_at) ORDER BY b.generation)
           FROM publications b WHERE b.aweme_id = v.aweme_id), '[]'::json) AS publications,
         COALESCE((SELECT json_agg(json_build_object(
             'generation', s.generation, 'revision', s.revision, 'cueCount', s.cue_count,
             'mismatchCount', s.mismatch_count, 'dirty', s.dirty, 'checkedAt', s.checked_at)
             ORDER BY s.generation)
           FROM localization.review_status s WHERE s.aweme_id = v.aweme_id), '[]'::json) AS review_status
    FROM videos v
    JOIN collections c ON c.id = v.collection_id
    LEFT JOIN profiles p ON p.sec_user_id = v.sec_user_id
    LEFT JOIN localization_outbox o ON o.aweme_id = v.aweme_id`;

// A pg row: the JSON columns arrive already parsed and are shaped by the SELECT above.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function toRecord(r: Record<string, any>): VideoRecord {
  return {
    awemeId: r.aweme_id,
    secUserId: r.sec_user_id,
    nickname: r.nickname,
    autoLocalize: r.auto_localize,
    desc: r.desc,
    createTime: r.create_time.toISOString(),
    durationMs: r.duration_ms,
    videoStatus: r.video_status,
    downloadAttempts: r.download_attempts,
    downloadError: r.download_error,
    outbox: r.outbox,
    runs: r.runs,
    decisions: r.decisions,
    renders: r.renders,
    publications: r.publications,
    reviewStatus: r.review_status,
    collection: { id: r.collection_id, name: r.collection_name, kind: r.collection_kind },
    mixEpisode: r.mix_episode,
  };
}

@Injectable()
export class DashboardRepository {
  constructor(@Inject(PG_POOL) private readonly pool: Pool) {}

  /** Videos newest first. Every filter is a bound parameter; `q` is escaped for LIKE. */
  async videos(filter: VideoFilter = {}): Promise<VideoRecord[]> {
    const where: string[] = [];
    const params: unknown[] = [];
    const bind = (value: unknown): string => {
      params.push(value);
      return `$${params.length}`;
    };
    if (filter.awemeId !== undefined) where.push(`v.aweme_id = ${bind(filter.awemeId)}`);
    if (filter.channel !== undefined) where.push(`v.sec_user_id = ${bind(filter.channel)}`);
    if (filter.collection !== undefined) where.push(`v.collection_id = ${bind(filter.collection)}::uuid`);
    if (filter.from !== undefined) where.push(`v.create_time >= ${bind(filter.from)}::timestamptz`);
    if (filter.to !== undefined) where.push(`v.create_time <= ${bind(filter.to)}::timestamptz`);
    if (filter.q !== undefined) {
      const pattern = bind(`%${escapeLike(filter.q)}%`);
      where.push(`(v.aweme_id LIKE ${pattern} ESCAPE '\\' OR v."desc" ILIKE ${pattern} ESCAPE '\\')`);
    }
    const sql = `${SELECT_VIDEOS}
      ${where.length > 0 ? `WHERE ${where.join(' AND ')}` : ''}
      ORDER BY v.create_time DESC, v.aweme_id`;
    const { rows } = await this.pool.query(sql, params);
    return rows.map(toRecord);
  }

  async video(awemeId: string): Promise<VideoRecord | null> {
    return (await this.videos({ awemeId }))[0] ?? null;
  }

  /** LLM cost per generation and stage. */
  async llmCosts(awemeId: string): Promise<LlmCostRow[]> {
    const { rows } = await this.pool.query(
      `SELECT generation, stage, COUNT(*)::int AS calls, COALESCE(SUM(cost_usd), 0) AS cost
         FROM localization.llm_calls WHERE aweme_id = $1
        GROUP BY generation, stage ORDER BY generation, stage`,
      [awemeId],
    );
    return rows.map((r) => ({
      generation: r.generation,
      stage: r.stage,
      calls: r.calls,
      costUsd: Number(r.cost),
    }));
  }

  async runDetails(awemeId: string): Promise<RunDetailRow[]> {
    const { rows } = await this.pool.query(
      `SELECT generation, verdict, needs_human, output_key, review_key
         FROM localization.runs WHERE aweme_id = $1 ORDER BY generation`,
      [awemeId],
    );
    return rows.map((r) => ({
      generation: r.generation,
      verdict: r.verdict,
      needsHuman: r.needs_human,
      outputKey: r.output_key,
      reviewKey: r.review_key,
    }));
  }

  async recentActions(awemeId: string, limit = 20): Promise<ActionRow[]> {
    const { rows } = await this.pool.query(
      `SELECT id, action, detail, created_at FROM dashboard_actions
        WHERE aweme_id = $1 ORDER BY created_at DESC, id DESC LIMIT $2`,
      [awemeId, limit],
    );
    return rows.map((r) => ({
      id: Number(r.id),
      action: r.action,
      detail: r.detail,
      createdAt: r.created_at.toISOString(),
    }));
  }

  /** The newest request to open Studio for this video, whichever generation it named. */
  async latestStudioRequest(awemeId: string): Promise<StudioRequestRow | null> {
    const { rows } = await this.pool.query(
      `SELECT generation, state, error, requested_at, handled_at FROM studio_open_requests
        WHERE aweme_id = $1 ORDER BY requested_at DESC, id DESC LIMIT 1`,
      [awemeId],
    );
    const r = rows[0];
    return r
      ? {
          generation: Number(r.generation),
          state: r.state,
          error: r.error,
          requestedAt: r.requested_at.toISOString(),
          handledAt: r.handled_at ? r.handled_at.toISOString() : null,
        }
      : null;
  }

  /** The newest request to save this video into a folder, whichever generation it named. */
  async latestFolderExport(awemeId: string): Promise<FolderExportRow | null> {
    const { rows } = await this.pool.query(
      `SELECT generation, state, target_subdir, file_stem, files, error, requested_at, handled_at
         FROM folder_exports WHERE aweme_id = $1 ORDER BY requested_at DESC, id DESC LIMIT 1`,
      [awemeId],
    );
    const r = rows[0];
    return r
      ? {
          generation: r.generation,
          state: r.state,
          targetSubdir: r.target_subdir,
          fileStem: r.file_stem,
          files: r.files,
          error: r.error,
          requestedAt: r.requested_at.toISOString(),
          handledAt: r.handled_at ? r.handled_at.toISOString() : null,
        }
      : null;
  }

  /** Every channel with its newest job. */
  async channels(): Promise<ChannelRow[]> {
    const { rows } = await this.pool.query(
      `SELECT p.sec_user_id, p.nickname, p.auto_localize, p.last_synced_at,
              j.id AS job_id, j.status AS job_status, j.created_at AS job_created_at,
              j.total_videos, j.downloaded_count, j.failed_count, j.error AS job_error
         FROM profiles p
         LEFT JOIN LATERAL (
           SELECT * FROM jobs WHERE sec_user_id = p.sec_user_id
            ORDER BY created_at DESC LIMIT 1) j ON true
        ORDER BY p.nickname NULLS LAST, p.sec_user_id`,
    );
    return rows.map((r) => ({
      secUserId: r.sec_user_id,
      nickname: r.nickname,
      autoLocalize: r.auto_localize,
      lastSyncedAt: r.last_synced_at ? r.last_synced_at.toISOString() : null,
      latestJob: r.job_id
        ? {
            id: r.job_id,
            status: r.job_status,
            createdAt: r.job_created_at.toISOString(),
            totalVideos: r.total_videos,
            downloadedCount: r.downloaded_count,
            failedCount: r.failed_count,
            error: r.job_error,
          }
        : null,
    }));
  }

  /** The newest job of each channel, when that job is blocked or failed. */
  async blockedJobs(): Promise<BlockedJobRow[]> {
    const { rows } = await this.pool.query(
      `SELECT j.id, j.sec_user_id, p.nickname, j.status, j.error, j.updated_at
         FROM (SELECT DISTINCT ON (sec_user_id) * FROM jobs
                ORDER BY sec_user_id, created_at DESC) j
         LEFT JOIN profiles p ON p.sec_user_id = j.sec_user_id
        WHERE j.status IN ('blocked', 'failed')
        ORDER BY j.updated_at DESC`,
    );
    return rows.map((r) => ({
      id: r.id,
      secUserId: r.sec_user_id,
      nickname: r.nickname,
      status: r.status,
      error: r.error,
      updatedAt: r.updated_at.toISOString(),
    }));
  }

  async hostWorker(name: string): Promise<HostWorkerRow | null> {
    const { rows } = await this.pool.query(
      `SELECT name, seen_at, info FROM localization.host_workers WHERE name = $1`,
      [name],
    );
    const r = rows[0];
    return r ? { name: r.name, seenAtMs: r.seen_at.getTime(), info: r.info ?? {} } : null;
  }

  async ping(): Promise<void> {
    await this.pool.query('SELECT 1');
  }
}
