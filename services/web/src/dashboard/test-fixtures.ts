import type {
  AttentionResponse,
  ChannelListResponse,
  HealthReport,
  Stage,
  VideoDetailResponse,
  VideoListItem,
  VideoListResponse,
} from '../api/types';
import { STAGES } from '../api/types';

export function item(over: Partial<VideoListItem> = {}): VideoListItem {
  return {
    aweme_id: '7001',
    sec_user_id: 'sec1',
    channel: 'Kênh A',
    desc: 'mô tả',
    create_time: '2026-11-10T00:00:00.000Z',
    duration_ms: 12_000,
    current_gen: 1,
    stage: 'awaiting_review',
    outbox_state: 'dispatched',
    run_status: 'ready_for_review',
    stage_since: '2026-11-12T01:00:00.000Z',
    updated_at: '2026-11-12T01:05:00.000Z',
    revision: 'rev-1',
    mismatch_count: 0,
    attention: [],
    ...over,
  };
}

export function counts(over: Partial<Record<Stage, number>> = {}): Record<Stage, number> {
  return Object.fromEntries(STAGES.map((s) => [s, over[s] ?? 0])) as Record<Stage, number>;
}

export function list(items: VideoListItem[], over: Partial<VideoListResponse> = {}): VideoListResponse {
  const byStage: Partial<Record<Stage, number>> = {};
  for (const i of items) byStage[i.stage] = (byStage[i.stage] ?? 0) + 1;
  return { items, total: items.length, counts_by_stage: counts(byStage), sources: { postgres: 'ok', temporal: 'ok' }, ...over };
}

export const emptyAttention: AttentionResponse = { jobs: [], items: [], sources: { postgres: 'ok', temporal: 'ok' } };
export const emptyChannels: ChannelListResponse = { items: [], sources: { postgres: 'ok', temporal: 'ok' } };

const ok = { status: 'ok' as const, reason: 'Bình thường.' };
export function health(over: Partial<HealthReport> = {}): HealthReport {
  return {
    generatedAt: '2026-11-12T01:00:00.000Z',
    postgres: ok,
    redis: ok,
    cookie: ok,
    temporal: ok,
    minio: ok,
    voicestudio: ok,
    queues: { stt: ok, tts: ok, media: ok, render: ok, llm: ok, io: ok, workflows: ok },
    host_worker: ok,
    disk: ok,
    ...over,
  };
}

export function detail(over: Partial<VideoDetailResponse> = {}): VideoDetailResponse {
  return {
    video: {
      aweme_id: '7001',
      desc: 'mô tả',
      create_time: '2026-11-10T00:00:00.000Z',
      duration_ms: 12_000,
      download_status: 'completed',
      download_attempts: 1,
      download_error: null,
    },
    channel: { sec_user_id: 'sec1', nickname: 'Kênh A', auto_localize: false },
    current_gen: 2,
    stage: 'awaiting_review',
    attention: [],
    generations: [],
    timeline: [],
    verdict: null,
    llm_cost: { total_usd: 0.0123, by_stage: [] },
    review_status: { generation: 2, revision: 'rev-9', cueCount: 10, mismatchCount: 0, dirty: false, checkedAt: '2026-11-12T01:00:00.000Z' },
    decision: null,
    render: null,
    publication: null,
    dashboard_actions: [],
    sources: { postgres: 'ok', temporal: 'ok' },
    ...over,
  };
}
