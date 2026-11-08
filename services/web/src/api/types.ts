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
export const RETRYABLE_STATUSES: readonly JobStatus[] = ['blocked', 'failed'];

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
 * `GET /api/workspace/snapshot` and `GET /api/workspace/runs/:awemeId/:generation`.
 * Source of truth: services/downloader/src/workspace/workspace.schemas.ts
 * (`snapshotSchema`, `runDetailSchema`). Every string is built by the server
 * from structured fields; none is free text from Temporal or a prompt.
 */
export type SourceState = 'ok' | 'down';
export type AgentState = 'idle' | 'working' | 'retrying' | 'failed';
export type Zone = 'media' | 'voice' | 'llm' | 'unknown';
export type GateOutcome = 'skipped_ad' | 'skipped_no_video' | 'published_original' | 'needs_human';
export type WorkspaceEventKind =
  | 'activity_started'
  | 'activity_retrying'
  | 'activity_completed'
  | 'activity_failed'
  | 'llm_call_recorded'
  | 'gate_decision'
  | 'handoff';

export interface WorkspaceSources {
  temporal: SourceState;
  postgres: SourceState;
}

export interface WorkspaceAgent {
  id: string;
  zone: Zone;
  state: AgentState;
  awemeId?: string;
  generation?: number;
  since?: string;
  attempt?: number;
}

export interface WorkspaceEvent {
  id: string;
  at: string;
  kind: WorkspaceEventKind;
  agentId?: string;
  toAgentId?: string;
  awemeId?: string;
  generation?: number;
  detail: string;
}

export interface WorkspaceRun {
  awemeId: string;
  generation: number;
  phase: string;
  outcome?: GateOutcome;
  llmCostUsd: number;
  updatedAt?: string;
}

export interface WorkspaceSnapshot {
  generatedAt: string;
  sources: WorkspaceSources;
  agents: WorkspaceAgent[];
  events: WorkspaceEvent[];
  runs: WorkspaceRun[];
}

export interface RunPhase {
  agentId: string;
  state: 'running' | 'retrying' | 'done' | 'failed';
  startedAt: string;
  endedAt?: string;
  activities: number;
}

export interface RunDetail {
  awemeId: string;
  generation: number;
  sources: WorkspaceSources;
  status?: string;
  outcome?: GateOutcome;
  phases: RunPhase[];
  steps: Array<{
    id: string;
    activity: string;
    agentId: string;
    state: 'running' | 'retrying' | 'done' | 'failed' | 'cancelled';
    startedAt: string;
    endedAt?: string;
    attempt?: number;
  }>;
  llmCostUsd: number;
  costByStage: Array<{ stage: string; calls: number; costUsd: number }>;
  gpuSecondsEstimate: { stt: number; tts: number };
}
