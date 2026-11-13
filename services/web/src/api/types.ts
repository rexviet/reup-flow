/**
 * Wire types, mirrored byte-for-byte off the real API mappers -- NOT
 * guessed. Source of truth:
 *   services/downloader/src/jobs/jobs.mapper.ts   (JobResponse / JobDetailResponse)
 *   services/downloader/src/cookie/cookie.service.ts (CookieStatus)
 *   services/downloader/src/db/jobs.repository.ts (CANCELLABLE_STATUSES / RETRYABLE_STATUSES)
 *
 * `total_videos` is `number | null` on purpose (see JobResponse there): it is
 * NULL until the enumerate lane finishes at least one full pass
 * (`finishEnumerate` in jobs.repository.ts). Any fixture or mock in this
 * package that always fills it in with a number is testing an easier world
 * than the real API -- see phase-08 instructions §"Bài học đã trả giá".
 */
export interface JobResponse {
  job_id: string;
  sec_user_id: string;
  status: JobStatus;
  total_videos: number | null;
  enumerated_count: number;
  downloaded_count: number;
  failed_count: number;
  max_cursor: number;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface JobDetailResponse extends JobResponse {
  profile_video_counts: Record<string, number>;
}

/**
 * `videos.status` / `jobs.status` are plain `text` columns on the backend
 * (db/schema.ts's deliberate choice -- new statuses were still being
 * discovered through phases 4-7), so this is documentation, not a runtime
 * enum the API enforces. Treat any string not in this list as "unknown" in
 * the UI rather than crashing.
 */
export type JobStatus =
  | 'pending'
  | 'enumerating'
  | 'downloading'
  | 'blocked'
  | 'failed'
  | 'cancelled'
  | 'completed';

/**
 * jobs.repository.ts's CANCELLABLE_STATUSES, copied here so the UI can grey
 * out a Cancel button before round-tripping to the API for a 409. This is a
 * UI convenience only -- the backend re-checks and is the actual authority;
 * if this list drifts from jobs.repository.ts the worst case is a stale
 * button state, not a wrong cancellation.
 */
export const CANCELLABLE_STATUSES: readonly JobStatus[] = ['pending', 'enumerating', 'downloading', 'blocked'];

/** jobs.repository.ts's RETRYABLE_STATUSES, same caveat as above. */
export const RETRYABLE_STATUSES: readonly JobStatus[] = ['blocked', 'failed', 'cancelled'];

export interface JobListResponse {
  items: JobResponse[];
  total: number;
  limit: number;
  offset: number;
}

/** cookie.service.ts's CookieStatus, returned by GET /internal/cookie/status. */
export interface CookieStatus {
  present: boolean;
  age_seconds: number | null;
  last_ok_at: string | null;
}

/**
 * `GET/POST /api/dashboard/*`. Source of truth:
 *   services/downloader/src/dashboard/{dashboard.service,dashboard.stage,dashboard.health,dashboard.actions,dashboard.media}.ts
 * Vietnamese strings (attention reasons, health reasons, skip reasons) are built by the server and shown as is.
 */
export const STAGES = [
  'downloading',
  'download_failed',
  'held',
  'queued',
  'localizing',
  'dispatch_failed',
  'run_failed',
  'skipped',
  'awaiting_review',
  'returned',
  'render_queued',
  'rendering',
  'render_failed',
  'rendered',
  'published',
] as const;
export type Stage = (typeof STAGES)[number];

export type LocalizingStep = 'prepare' | 'translate' | 'dub' | 'review' | 'unknown';

export interface DashboardSources {
  postgres: 'ok' | 'down';
  temporal: 'ok' | 'down';
}

export interface AttentionItem {
  code: string;
  reason: string;
  action: string;
}

export interface VideoListItem {
  aweme_id: string;
  sec_user_id: string;
  channel: string | null;
  desc: string | null;
  create_time: string;
  duration_ms: number | null;
  current_gen: number | null;
  stage: Stage;
  step?: LocalizingStep;
  unknown?: boolean;
  outbox_state: string | null;
  run_status: string | null;
  stage_since: string | null;
  updated_at: string | null;
  /** Revision of the review project being looked at; `approve` must send exactly this. */
  revision: string | null;
  mismatch_count: number | null;
  attention: AttentionItem[];
}

export interface VideoListResponse {
  items: VideoListItem[];
  total: number;
  counts_by_stage: Record<Stage, number>;
  sources: DashboardSources;
}

export interface VideoListQuery {
  stage?: Stage;
  channel?: string;
  q?: string;
  from?: string;
  to?: string;
  attention?: boolean;
  limit?: number;
  offset?: number;
}

export interface BlockedJobItem {
  id: string;
  sec_user_id: string;
  channel: string | null;
  status: string;
  error: string | null;
  updated_at: string;
  reason: string;
  action: string;
}

export interface AttentionResponse {
  jobs: BlockedJobItem[];
  items: VideoListItem[];
  sources: DashboardSources;
}

export interface ChannelItem {
  sec_user_id: string;
  nickname: string | null;
  auto_localize: boolean;
  last_synced_at: string | null;
  latest_job: {
    id: string;
    status: string;
    created_at: string;
    total_videos: number | null;
    downloaded_count: number;
    failed_count: number;
    error: string | null;
  } | null;
  total: number;
  counts_by_stage: Record<Stage, number>;
}

export interface ChannelListResponse {
  items: ChannelItem[];
  sources: DashboardSources;
}

export type HealthState = 'ok' | 'degraded' | 'down' | 'unknown';
export interface HealthItem {
  status: HealthState;
  reason: string;
  pollers?: number;
}
export interface HealthReport {
  generatedAt: string;
  postgres: HealthItem;
  redis: HealthItem;
  cookie: HealthItem;
  temporal: HealthItem;
  minio: HealthItem;
  voicestudio: HealthItem;
  queues: Record<'stt' | 'tts' | 'ingest' | 'media' | 'render' | 'llm' | 'io' | 'workflows', HealthItem>;
  host_worker: HealthItem;
  disk: HealthItem;
}

export interface TimelineStep {
  id: string;
  activity: string;
  agentId: string;
  state: 'running' | 'retrying' | 'done' | 'failed' | 'cancelled';
  startedAt: string;
  endedAt?: string;
  attempt?: number;
  step: LocalizingStep;
}

export interface ReviewStatusView {
  generation: number;
  revision: string | null;
  cueCount: number | null;
  mismatchCount: number | null;
  dirty: boolean | null;
  checkedAt: string;
}

export interface RenderView {
  state: string;
  approved_revision: string;
  attempts: number;
  error: string | null;
  rendered_at: string | null;
  final_ready: boolean;
  srt_ready: boolean;
}

export interface PublicationView {
  generation: number;
  state: string;
  platform: string | null;
  postUrl: string | null;
  note: string | null;
  updatedAt: string;
}

export interface DecisionView {
  generation: number;
  decision: string;
  approvedRevision: string | null;
  confirmMismatch: boolean;
  note: string | null;
  decidedAt: string;
}

export interface GenerationView {
  generation: number;
  is_current: boolean;
  run: { status: string; updated_at: string; workflow_id: string; review_rounds: number; has_project: boolean } | null;
  open_workflow: boolean;
  decision: DecisionView | null;
  render: RenderView | null;
  publication: PublicationView | null;
  review_status: ReviewStatusView | null;
  llm_cost_usd: number;
}

export interface VideoDetailResponse {
  /** `null` when Postgres could not be read; `sources` says why. */
  video: {
    aweme_id: string;
    desc: string | null;
    create_time: string;
    duration_ms: number | null;
    download_status: string;
    download_attempts: number;
    download_error: string | null;
  } | null;
  channel?: { sec_user_id: string; nickname: string | null; auto_localize: boolean };
  current_gen?: number | null;
  stage?: Stage;
  step?: LocalizingStep;
  unknown?: boolean;
  attention?: AttentionItem[];
  generations?: GenerationView[];
  timeline?: TimelineStep[] | null;
  /** Reviewer verdict as the pipeline stored it; shape is the Reviewer's, shown loosely. */
  verdict?: unknown;
  llm_cost?: { total_usd: number; by_stage: Array<{ stage: string; calls: number; cost_usd: number }> };
  review_status?: ReviewStatusView | null;
  decision?: DecisionView | null;
  render?: RenderView | null;
  publication?: PublicationView | null;
  studio_request?: { generation: number; state: string; error: string | null; requestedAt: string; handledAt: string | null } | null;
  dashboard_actions?: Array<{ id: number; action: string; detail: unknown; created_at: string }>;
  sources: DashboardSources;
}

export type DashboardActionName =
  | 'localize'
  | 'retry_download'
  | 'resume'
  | 'rerun'
  | 'approve'
  | 'return'
  | 'rerender'
  | 'mark_published'
  | 'mark_unpublished'
  | 'open_studio';

export interface ActionRequest {
  action: DashboardActionName;
  aweme_ids: string[];
  note?: string;
  post_url?: string;
  platform?: string;
  revision?: string;
  generation?: number;
  confirm_mismatch?: boolean;
}

export interface ActionResult {
  done: string[];
  skipped: Array<{ aweme_id: string; reason: string }>;
}

export type MediaKind = 'source' | 'final' | 'srt' | 'poster';
export interface MediaLink {
  kind: MediaKind;
  available: boolean;
  url?: string;
  expires_at?: string;
  /** Set by the server for a file that predates generations (the old `output/<aweme>.mp4`). */
  note?: string;
}
