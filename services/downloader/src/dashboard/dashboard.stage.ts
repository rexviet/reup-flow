import { stepOf, type Step } from '../workspace/workspace.steps';
import { renderFailureOf } from './render-failure';

/**
 * Where a video is on its way from download to publication. Pure: every input
 * is a plain value the service already read, so the same facts always give the
 * same stage and the table in the phase plan is testable row by row.
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

export type LocalizingStep = Step | 'unknown';

/** Outbox `dispatched` with no workflow and no run for this long means the run vanished. */
export const LOST_AFTER_MS = 10 * 60 * 1000;

export interface PendingActivityFact {
  name: string;
  attempt: number;
  atMs: number;
}

/** A workflow of this video that Temporal reports as open. */
export interface OpenGeneration {
  generation: number;
  /** `undefined` when the activities could not be read; the step is then unknown. */
  pending: PendingActivityFact[] | undefined;
}

export interface RunFact {
  generation: number;
  status: string;
  hasOutputKey: boolean;
  /** A review project exists; `reup-review-auto-open` only opens runs that have one. */
  hasProject?: boolean;
}

export interface StageFacts {
  videoStatus: string;
  outbox: { generation: number; state: string; dispatchedAtMs: number | null } | null;
  runs: RunFact[];
  decisions: Array<{ generation: number; decision: string }>;
  /** `error` is the worker's raw reason for a failed render. */
  renders: Array<{ generation: number; state: string; error?: string | null }>;
  publications: Array<{ generation: number; state: string }>;
  reviewStatus: Array<{ generation: number; mismatchCount: number | null; dirty: boolean | null }>;
}

export interface StageResult {
  /** `null` while the video has no outbox row and no run (still downloading, or never queued). */
  currentGen: number | null;
  stage: Stage;
  step?: LocalizingStep;
  /** True when a source needed to decide was unreadable, so the stage is a best guess. */
  unknown?: boolean;
}

const AWAITING = new Set(['ready_for_review', 'needs_human']);
const SKIPPED = new Set(['skipped_ad', 'skipped_no_video']);
const FINISHED_LEGACY = new Set(['published', 'published_original']);

/**
 * `GREATEST(outbox.generation, MAX(runs.generation), MAX(open workflow generation))`.
 * The canary creates generations that the outbox row never learns about, so the
 * outbox alone under-reports.
 */
export function currentGeneration(
  facts: Pick<StageFacts, 'outbox' | 'runs'>,
  open: readonly OpenGeneration[] | undefined,
): number | null {
  const gens = [
    ...(facts.outbox ? [facts.outbox.generation] : []),
    ...facts.runs.map((r) => r.generation),
    ...(open ?? []).map((o) => o.generation),
  ];
  return gens.length === 0 ? null : Math.max(...gens);
}

function runStage(
  run: RunFact,
  gen: number,
  facts: StageFacts,
): Stage {
  const published = facts.publications.find((p) => p.generation === gen)?.state === 'published';
  if (published) return 'published';
  if (run.status === 'failed') return 'run_failed';
  if (SKIPPED.has(run.status)) return 'skipped';
  if (FINISHED_LEGACY.has(run.status)) return run.hasOutputKey ? 'rendered' : 'run_failed';
  if (!AWAITING.has(run.status)) return 'run_failed';

  const decision = facts.decisions.find((d) => d.generation === gen)?.decision;
  if (decision === undefined) return 'awaiting_review';
  if (decision === 'returned') return 'returned';
  // Approved: the render row says how far the final render got. No row yet means
  // the approval is committed and the render has not been picked up.
  const render = facts.renders.find((r) => r.generation === gen)?.state;
  if (render === 'rendering') return 'rendering';
  if (render === 'failed') return 'render_failed';
  if (render === 'rendered') return 'rendered';
  return 'render_queued';
}

export function computeStage(
  facts: StageFacts,
  open: readonly OpenGeneration[] | undefined,
): StageResult {
  if (facts.videoStatus === 'pending' || facts.videoStatus === 'downloading') {
    return { currentGen: null, stage: 'downloading' };
  }
  if (facts.videoStatus === 'failed' || facts.videoStatus === 'skipped') {
    return { currentGen: null, stage: 'download_failed' };
  }

  const currentGen = currentGeneration(facts, open);
  const result = decide(facts, open, currentGen);
  // Without Temporal a canary generation that is still running is invisible, so any
  // answer past the download stage is a best guess.
  return open === undefined && result.currentGen !== null && !result.unknown ? { ...result, unknown: true } : result;
}

function decide(
  facts: StageFacts,
  open: readonly OpenGeneration[] | undefined,
  currentGen: number | null,
): StageResult {
  if (currentGen === null) {
    // Downloaded but never queued (a video from before the outbox existed): nothing moves it on.
    return { currentGen, stage: 'held' };
  }

  // A run row means the workflow ended; it wins over an open workflow that is
  // only still visible because the two sources were read a moment apart.
  const run = facts.runs.find((r) => r.generation === currentGen);
  if (run) return { currentGen, stage: runStage(run, currentGen, facts) };

  const openNow = open?.find((o) => o.generation === currentGen);
  if (openNow) {
    const newest = [...(openNow.pending ?? [])].sort((a, b) => b.atMs - a.atMs)[0];
    const step = newest ? (stepOf(newest.name) ?? 'unknown') : 'unknown';
    return { currentGen, stage: 'localizing', step };
  }

  // The outbox only speaks for the generation it holds.
  const outbox = facts.outbox && facts.outbox.generation === currentGen ? facts.outbox : null;
  switch (outbox?.state) {
    case 'held':
      return { currentGen, stage: 'held' };
    case 'pending':
      return { currentGen, stage: 'queued' };
    case 'rejected':
    case 'dead':
      return { currentGen, stage: 'dispatch_failed' };
    case 'dispatched':
      // Without Temporal this could be a running workflow: say so instead of "idle".
      if (open === undefined) return { currentGen, stage: 'localizing', step: 'unknown', unknown: true };
      return { currentGen, stage: 'queued' };
    default:
      return { currentGen, stage: 'queued' };
  }
}

export type AttentionCode =
  | 'download_failed'
  | 'dispatch_failed'
  | 'dispatch_dead'
  | 'run_failed'
  | 'render_failed'
  | 'render_stale'
  | 'needs_human'
  | 'mismatch'
  | 'dirty'
  | 'retrying'
  | 'stuck'
  | 'lost'
  | 'old_generation_awaiting_review';

export interface AttentionItem {
  code: AttentionCode;
  /** Vietnamese, shown as is. */
  reason: string;
  /** Vietnamese suggested action. */
  action: string;
}

export interface AttentionContext {
  nowMs: number;
  stuckMs: number;
  open: readonly OpenGeneration[] | undefined;
}

/** Why a video needs the admin, newest concern first in the order below. Empty when it does not. */
export function attentionOf(
  facts: StageFacts,
  result: StageResult,
  ctx: AttentionContext,
): AttentionItem[] {
  const items: AttentionItem[] = [];
  const { stage, currentGen } = result;

  if (stage === 'download_failed') {
    items.push({ code: 'download_failed', reason: 'Tải video thất bại.', action: 'Xem lỗi tải và thử tải lại.' });
  }
  if (stage === 'dispatch_failed') {
    const rejected = facts.outbox?.state === 'rejected';
    items.push(
      rejected
        ? { code: 'dispatch_failed', reason: 'Đầu vào bị từ chối, chạy lại không giúp được.', action: 'Kiểm tra file video gốc.' }
        : { code: 'dispatch_dead', reason: 'Gửi sang localization thất bại nhiều lần.', action: 'Chạy lại video.' },
    );
  }
  if (stage === 'run_failed') {
    items.push({ code: 'run_failed', reason: 'Lần chạy localization thất bại.', action: 'Chạy lại video.' });
  }
  if (stage === 'render_failed') {
    const failure = renderFailureOf(facts.renders.find((r) => r.generation === currentGen)?.error);
    // Rendering again cannot help once the review changed after approval: it needs a new approval.
    items.push({ code: failure.kind === 'stale' ? 'render_stale' : 'render_failed', reason: failure.reason, action: failure.action });
  }
  const run = facts.runs.find((r) => r.generation === currentGen);
  if (stage === 'awaiting_review' && run?.status === 'needs_human') {
    items.push({ code: 'needs_human', reason: 'Pipeline đánh dấu cần người xem.', action: 'Mở Studio kiểm tra rồi duyệt hoặc trả lại.' });
  }
  if (stage === 'awaiting_review' || stage === 'returned') {
    const status = facts.reviewStatus.find((r) => r.generation === currentGen);
    if ((status?.mismatchCount ?? 0) > 0) {
      items.push({
        code: 'mismatch',
        reason: `Còn ${status?.mismatchCount} chỗ lời đọc lệch phụ đề.`,
        action: 'Mở Studio sửa, hoặc xác nhận lệch khi duyệt.',
      });
    }
    if (status?.dirty === true) {
      items.push({ code: 'dirty', reason: 'Project có thay đổi chưa lưu.', action: 'Lưu project trong Studio trước khi duyệt.' });
    }
  }

  if (stage === 'localizing') {
    const openNow = ctx.open?.find((o) => o.generation === currentGen);
    for (const p of openNow?.pending ?? []) {
      if (p.attempt > 1) {
        items.push({ code: 'retrying', reason: `Bước ${p.name} đang thử lại lần ${p.attempt}.`, action: 'Xem log worker; bước tự thử lại.' });
      } else if (ctx.nowMs - p.atMs > ctx.stuckMs) {
        const minutes = Math.round((ctx.nowMs - p.atMs) / 60_000);
        items.push({ code: 'stuck', reason: `Bước ${p.name} chạy đã ${minutes} phút.`, action: 'Xem worker và Temporal UI.' });
      }
    }
  }

  // Dispatched, but Temporal has no workflow and no run was written: cancelled in the Temporal UI or lost.
  if (
    stage === 'queued' &&
    facts.outbox?.state === 'dispatched' &&
    facts.outbox.dispatchedAtMs !== null &&
    ctx.open !== undefined &&
    ctx.nowMs - facts.outbox.dispatchedAtMs > LOST_AFTER_MS
  ) {
    items.push({ code: 'lost', reason: 'Đã gửi đi nhưng không có workflow đang chạy và chưa có kết quả.', action: 'Chạy lại video.' });
  }

  // `reup-review-auto-open` still opens a generation that is no longer the current one.
  const stale = facts.runs.filter(
    (r) =>
      r.generation !== currentGen &&
      AWAITING.has(r.status) &&
      r.hasProject !== false &&
      !facts.decisions.some((d) => d.generation === r.generation),
  );
  if (stale.length > 0) {
    const gens = stale.map((r) => `g${r.generation}`).join(', ');
    items.push({
      code: 'old_generation_awaiting_review',
      reason: `Generation cũ ${gens} vẫn đang chờ duyệt.`,
      action: 'Trả lại hoặc bỏ qua bản cũ để Studio không mở nhầm.',
    });
  }
  return items;
}
