import { Inject, Injectable, Logger, NotFoundException } from '@nestjs/common';
import type { DashboardTemporal } from '../workspace/workspace.dashboard-reader';
import { stepsOf } from '../workspace/workspace.service';
import { DASHBOARD_TEMPORAL } from '../workspace/workspace.tokens';
import { stepOf } from '../workspace/workspace.steps';
import type { WorkflowInspection } from '../workspace/workspace.temporal';
import {
  type BlockedJobRow,
  type ChannelRow,
  type FolderExportRow,
  type RenderRow,
  DashboardRepository,
  stageFactsOf,
  type VideoRecord,
} from './dashboard.repository';
import {
  attentionOf,
  computeStage,
  type AttentionItem,
  type OpenGeneration,
  type Stage,
  type StageFacts,
  type StageResult,
  STAGES,
} from './dashboard.stage';
import type { VideoFilter, VideosQuery } from './dashboard.schemas';

export type DashboardStore = Pick<
  DashboardRepository,
  | 'videos'
  | 'video'
  | 'llmCosts'
  | 'runDetails'
  | 'recentActions'
  | 'latestStudioRequest'
  | 'latestFolderExport'
  | 'channels'
  | 'blockedJobs'
>;

export interface Sources {
  postgres: 'ok' | 'down';
  temporal: 'ok' | 'down';
}

export const SOURCE_TIMEOUT_MS = 3000;
/** One slow workflow describe must not turn the whole Temporal read into `down`. */
export const OPEN_READ_TIMEOUT_MS = 8000;
export const DESCRIBE_TIMEOUT_MS = 1500;
const DESCRIBE_CONCURRENCY = 8;
/** Temporal is read once per window however many tabs poll. */
export const OPEN_CACHE_MS = 2000;
const MAX_DESC_CHARS = 200;

const roundUsd = (n: number): number => Math.round(n * 1e8) / 1e8;

function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  let timer: NodeJS.Timeout;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error('source timed out')), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

/** A source that fails is `undefined`, never an exception: the page must still load. */
const log = new Logger('Dashboard');

async function attempt<T>(read: () => Promise<T>, timeoutMs = SOURCE_TIMEOUT_MS): Promise<T | undefined> {
  try {
    return await withTimeout(read(), timeoutMs);
  } catch (err) {
    // Class and message only: never the SQL parameters. A missing grant or a column typo
    // otherwise shows up as nothing but `sources.postgres: down`.
    log.warn(`source read failed: ${err instanceof Error ? `${err.name}: ${err.message}` : 'unknown error'}`);
    return undefined;
  }
}

/** Runs `work` over `items` with at most `limit` in flight. */
async function mapLimited<T>(items: readonly T[], limit: number, work: (item: T) => Promise<void>): Promise<void> {
  let next = 0;
  const lanes = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (next < items.length) await work(items[next++]);
  });
  await Promise.all(lanes);
}

function parseWorkflowId(workflowId: string): { awemeId: string; generation: number } | null {
  const match = /^video-(\d+)-g(\d+)$/.exec(workflowId);
  return match ? { awemeId: match[1], generation: Number(match[2]) } : null;
}

interface Assessed {
  record: VideoRecord;
  facts: StageFacts;
  result: StageResult;
  attention: AttentionItem[];
}

interface Assessment {
  nowMs: number;
  /** `undefined` when Postgres could not be read. */
  videos: Assessed[] | undefined;
  open: Map<string, OpenGeneration[]> | undefined;
  sources: Sources;
}

function emptyCounts(): Record<Stage, number> {
  return Object.fromEntries(STAGES.map((s) => [s, 0])) as Record<Stage, number>;
}

const latestOf = (times: Array<string | null | undefined>): string | null => {
  const ms = times.flatMap((t) => (t ? [Date.parse(t)] : [])).filter((n) => !Number.isNaN(n));
  return ms.length === 0 ? null : new Date(Math.max(...ms)).toISOString();
};

/**
 * `stage_since`: the newest write among the facts that decide the stage, so it moves when
 * the stage does. `updated_at` also counts the review status check, which the worker refreshes.
 */
function timesOf(record: VideoRecord, gen: number | null): { stage_since: string | null; updated_at: string | null } {
  if (gen === null) return { stage_since: null, updated_at: null };
  const forGen = <T extends { generation: number }>(rows: T[]): T | undefined => rows.find((r) => r.generation === gen);
  const stageTimes = [
    record.outbox?.generation === gen ? record.outbox.dispatchedAt : null,
    forGen(record.runs)?.updatedAt,
    forGen(record.decisions)?.decidedAt,
    forGen(record.renders)?.renderedAt,
    forGen(record.publications)?.updatedAt,
  ];
  return {
    stage_since: latestOf(stageTimes),
    updated_at: latestOf([...stageTimes, forGen(record.reviewStatus)?.checkedAt]),
  };
}

function listItem(a: Assessed) {
  const { record, result } = a;
  const run = record.runs.find((r) => r.generation === result.currentGen);
  return {
    aweme_id: record.awemeId,
    sec_user_id: record.secUserId,
    channel: record.nickname,
    collection: record.collection,
    mix_episode: record.mixEpisode,
    desc: record.desc === null ? null : record.desc.slice(0, MAX_DESC_CHARS),
    create_time: record.createTime,
    duration_ms: record.durationMs,
    current_gen: result.currentGen,
    stage: result.stage,
    ...(result.step ? { step: result.step } : {}),
    ...(result.unknown ? { unknown: true } : {}),
    outbox_state: record.outbox && record.outbox.generation === result.currentGen ? record.outbox.state : null,
    run_status: run?.status ?? null,
    ...timesOf(record, result.currentGen),
    revision: record.reviewStatus.find((r) => r.generation === result.currentGen)?.revision ?? null,
    mismatch_count: record.reviewStatus.find((r) => r.generation === result.currentGen)?.mismatchCount ?? null,
    attention: a.attention,
  };
}

@Injectable()
export class DashboardService {
  private openCache?: { atMs: number; value: Promise<Map<string, OpenGeneration[]> | undefined> };

  constructor(
    @Inject(DashboardRepository) private readonly store: DashboardStore,
    @Inject(DASHBOARD_TEMPORAL) private readonly temporal: DashboardTemporal,
    private readonly stuckMinutes: number,
    private readonly clock: () => number = Date.now,
  ) {}

  /** Every open localization workflow, grouped by video, with what each is running. */
  private openWorkflows(nowMs: number): Promise<Map<string, OpenGeneration[]> | undefined> {
    if (this.openCache && nowMs - this.openCache.atMs < OPEN_CACHE_MS) return this.openCache.value;
    const value = attempt(async () => {
      const listed = await this.temporal.listOpenLocalizationIds();
      const byVideo = new Map<string, OpenGeneration[]>();
      await mapLimited(
        listed,
        DESCRIBE_CONCURRENCY,
        async ({ workflowId }) => {
          const ref = parseWorkflowId(workflowId);
          if (!ref) return;
          const pending = await attempt(() => this.temporal.pendingActivities(workflowId), DESCRIBE_TIMEOUT_MS);
          const entry: OpenGeneration = {
            generation: ref.generation,
            pending: pending?.map((p) => ({ name: p.name, attempt: p.attempt, atMs: p.atMs })),
          };
          byVideo.set(ref.awemeId, [...(byVideo.get(ref.awemeId) ?? []), entry]);
        },
      );
      return byVideo;
    }, OPEN_READ_TIMEOUT_MS);
    this.openCache = { atMs: nowMs, value };
    return value;
  }

  private async assess(filter: VideoFilter): Promise<Assessment> {
    const nowMs = this.clock();
    const [records, open] = await Promise.all([
      attempt(() => this.store.videos(filter)),
      this.openWorkflows(nowMs),
    ]);
    const stuckMs = this.stuckMinutes * 60_000;
    const videos = records?.map((record): Assessed => {
      const facts = stageFactsOf(record);
      const openGens = open === undefined ? undefined : (open.get(record.awemeId) ?? []);
      const result = computeStage(facts, openGens);
      return { record, facts, result, attention: attentionOf(facts, result, { nowMs, stuckMs, open: openGens }) };
    });
    return {
      nowMs,
      videos,
      open,
      sources: { postgres: videos === undefined ? 'down' : 'ok', temporal: open === undefined ? 'down' : 'ok' },
    };
  }

  async list(query: VideosQuery) {
    const { videos, sources } = await this.assess({
      channel: query.channel,
      collection: query.collection,
      q: query.q,
      from: query.from,
      to: query.to,
    });
    const base = videos ?? [];
    const counts = emptyCounts();
    for (const v of base) counts[v.result.stage] += 1;
    const filtered = base.filter(
      (v) =>
        (query.stage === undefined || v.result.stage === query.stage) &&
        (query.attention === undefined || (v.attention.length > 0) === query.attention),
    );
    return {
      items: filtered.slice(query.offset, query.offset + query.limit).map(listItem),
      total: filtered.length,
      counts_by_stage: counts,
      sources,
    };
  }

  async attention() {
    const [{ videos, sources }, jobs] = await Promise.all([
      this.assess({}),
      attempt(() => this.store.blockedJobs()),
    ]);
    const blocked: BlockedJobRow[] = jobs ?? [];
    return {
      jobs: blocked.map((j) => ({
        id: j.id,
        sec_user_id: j.secUserId,
        channel: j.nickname,
        status: j.status,
        error: j.error,
        updated_at: j.updatedAt,
        reason:
          j.status === 'blocked'
            ? 'Job tải bị chặn, thường do cookie Douyin hết hạn.'
            : 'Job tải thất bại.',
        action: j.status === 'blocked' ? 'Đẩy cookie mới rồi chạy lại job.' : 'Xem lỗi rồi chạy lại job.',
      })),
      items: (videos ?? []).filter((v) => v.attention.length > 0).map(listItem),
      sources: { ...sources, postgres: sources.postgres === 'ok' && jobs !== undefined ? 'ok' : 'down' },
    };
  }

  async channels() {
    const [{ videos, sources }, channels] = await Promise.all([
      this.assess({}),
      attempt(() => this.store.channels()),
    ]);
    const byChannel = new Map<string, Record<Stage, number>>();
    for (const v of videos ?? []) {
      const counts = byChannel.get(v.record.secUserId) ?? emptyCounts();
      counts[v.result.stage] += 1;
      byChannel.set(v.record.secUserId, counts);
    }
    const rows: ChannelRow[] = channels ?? [];
    return {
      items: rows.map((c) => {
        const counts = byChannel.get(c.secUserId) ?? emptyCounts();
        return {
          sec_user_id: c.secUserId,
          nickname: c.nickname,
          auto_localize: c.autoLocalize,
          last_synced_at: c.lastSyncedAt,
          latest_job: c.latestJob && {
            id: c.latestJob.id,
            status: c.latestJob.status,
            created_at: c.latestJob.createdAt,
            total_videos: c.latestJob.totalVideos,
            downloaded_count: c.latestJob.downloadedCount,
            failed_count: c.latestJob.failedCount,
            error: c.latestJob.error,
          },
          total: Object.values(counts).reduce((a, b) => a + b, 0),
          counts_by_stage: counts,
        };
      }),
      sources: { ...sources, postgres: sources.postgres === 'ok' && channels !== undefined ? 'ok' : 'down' },
    };
  }

  /**
   * 404 only when Postgres answered and has no such video. With Postgres down the
   * video is `null` and `sources` says why, so the page can still render.
   */
  async detail(awemeId: string) {
    const { videos, sources, open: openMap } = await this.assess({ awemeId });
    if (videos === undefined) return { video: null, sources };
    const found = videos[0];
    if (!found) throw new NotFoundException(`video ${awemeId} not found`);
    const { record, result } = found;
    const gen = result.currentGen;

    const [costs, runDetails, actions, studio, folderExport] = await Promise.all([
      attempt(() => this.store.llmCosts(awemeId)),
      attempt(() => this.store.runDetails(awemeId)),
      attempt(() => this.store.recentActions(awemeId)),
      attempt(() => this.store.latestStudioRequest(awemeId)),
      attempt(() => this.store.latestFolderExport(awemeId)),
    ]);
    const runRow = record.runs.find((r) => r.generation === gen);
    const workflowId = runRow?.workflowId ?? `video-${awemeId}-g${gen}`;
    let timeline: ReturnType<typeof timelineOf> | null = null;
    let temporalDown = sources.temporal === 'down';
    if (gen !== null && !temporalDown) {
      const inspection = await attempt(() => this.temporal.inspect(workflowId));
      if (inspection === undefined) temporalDown = true;
      else timeline = timelineOf(inspection);
    }

    const open = openMap?.get(awemeId) ?? [];
    const generations = [
      ...new Set([
        ...(record.outbox ? [record.outbox.generation] : []),
        ...record.runs.map((r) => r.generation),
        ...open.map((o) => o.generation),
        ...record.decisions.map((d) => d.generation),
        ...record.renders.map((r) => r.generation),
        ...record.publications.map((p) => p.generation),
      ]),
    ].sort((a, b) => b - a);

    const costRows = costs ?? [];
    const costOf = (g: number): number =>
      roundUsd(costRows.filter((c) => c.generation === g).reduce((s, c) => s + c.costUsd, 0));
    const forGen = <T extends { generation: number }>(rows: T[], g: number): T | null =>
      rows.find((r) => r.generation === g) ?? null;

    return {
      video: {
        aweme_id: record.awemeId,
        desc: record.desc,
        create_time: record.createTime,
        duration_ms: record.durationMs,
        download_status: record.videoStatus,
        download_attempts: record.downloadAttempts,
        download_error: record.downloadError,
        mix_episode: record.mixEpisode,
      },
      collection: record.collection,
      channel: { sec_user_id: record.secUserId, nickname: record.nickname, auto_localize: record.autoLocalize },
      current_gen: gen,
      stage: result.stage,
      ...(result.step ? { step: result.step } : {}),
      ...(result.unknown ? { unknown: true } : {}),
      attention: found.attention,
      generations: generations.map((g) => {
        const run = forGen(record.runs, g);
        return {
          generation: g,
          is_current: g === gen,
          outbox: record.outbox && record.outbox.generation === g ? record.outbox : null,
          run: run && {
            status: run.status,
            updated_at: run.updatedAt,
            workflow_id: run.workflowId,
            review_rounds: run.reviewRounds,
            has_project: run.projectKey !== null,
          },
          open_workflow: open.some((o) => o.generation === g),
          decision: forGen(record.decisions, g),
          render: renderView(forGen(record.renders, g)),
          publication: forGen(record.publications, g),
          review_status: forGen(record.reviewStatus, g),
          llm_cost_usd: costOf(g),
        };
      }),
      timeline,
      verdict: gen === null ? null : (runDetails?.find((r) => r.generation === gen)?.verdict ?? null),
      llm_cost:
        gen === null
          ? { total_usd: 0, by_stage: [] }
          : {
              total_usd: costOf(gen),
              by_stage: costRows
                .filter((c) => c.generation === gen)
                .map((c) => ({ stage: c.stage, calls: c.calls, cost_usd: roundUsd(c.costUsd) })),
            },
      review_status: gen === null ? null : forGen(record.reviewStatus, gen),
      decision: gen === null ? null : forGen(record.decisions, gen),
      render: gen === null ? null : renderView(forGen(record.renders, gen)),
      publication: gen === null ? null : forGen(record.publications, gen),
      studio_request: studio ?? null,
      folder_export: folderExportView(folderExport ?? null),
      dashboard_actions: (actions ?? []).map((a) => ({
        id: a.id,
        action: a.action,
        detail: a.detail,
        created_at: a.createdAt,
      })),
      sources: {
        postgres: [costs, runDetails, actions, studio, folderExport].includes(undefined) ? 'down' : 'ok',
        temporal: temporalDown ? 'down' : 'ok',
      } satisfies Sources,
    };
  }
}

/** Storage keys stay server side; the page only learns whether the files exist. */
function renderView(r: RenderRow | null) {
  return (
    r && {
      state: r.state,
      approved_revision: r.approvedRevision,
      attempts: r.attempts,
      error: r.error,
      rendered_at: r.renderedAt,
      final_ready: r.finalKey !== null,
      srt_ready: r.srtKey !== null,
    }
  );
}

/** The newest save-to-folder request: waiting, saving, saved (with its files) or failed (with why). */
function folderExportView(f: FolderExportRow | null) {
  return (
    f && {
      generation: f.generation,
      state: f.state,
      target_subdir: f.targetSubdir,
      file_stem: f.fileStem,
      files: f.files,
      error: f.error,
      requested_at: f.requestedAt,
      handled_at: f.handledAt,
    }
  );
}

/** The workspace's step list, each step tagged with the dashboard step it belongs to. */
function timelineOf(inspection: WorkflowInspection | null) {
  return stepsOf(inspection).map((s) => ({ ...s, step: stepOf(s.activity) ?? 'unknown' }));
}
