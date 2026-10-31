import type { Client } from '@temporalio/client';

/**
 * What the workspace needs from Temporal, in the shape the rest of the module
 * uses. `TemporalWorkflowReader` fills it from a live server; tests fill it
 * with the same values the real server returned (see test/workspace/fixtures).
 */
export interface PendingActivity {
  activityId: string;
  name: string;
  attempt: number;
  /** When this attempt started, else when the activity was scheduled. */
  atMs: number;
  /** Error class of the previous attempt, from `applicationFailureInfo.type`. */
  failureType?: string;
}

export interface ActivityRecord {
  eventId: number;
  name: string;
  /** Task queue the activity was scheduled on (`stt` and `tts` are the GPU ones). */
  queue: string;
  scheduledAtMs: number;
  completedAtMs?: number;
  failedAtMs?: number;
  /** Error class, never the message. */
  failureType?: string;
}

export interface WorkflowInspection {
  workflowId: string;
  startedAtMs?: number;
  pending: PendingActivity[];
  activities: ActivityRecord[];
}

export interface TemporalReader {
  listOpen(limit: number): Promise<string[]>;
  inspect(workflowId: string): Promise<WorkflowInspection>;
  /** Like `inspect`, but `null` when no workflow has this id (open or closed). */
  find(workflowId: string): Promise<WorkflowInspection | null>;
}

/** The loose shape of a protobuf Timestamp: `seconds` may be a number, a string or a Long. */
type Ts = { seconds?: unknown; nanos?: number | null } | null | undefined;

export function tsToMs(ts: Ts): number | undefined {
  if (!ts || ts.seconds === undefined || ts.seconds === null) return undefined;
  const seconds = Number(typeof ts.seconds === 'object' ? String(ts.seconds) : ts.seconds);
  if (!Number.isFinite(seconds)) return undefined;
  return seconds * 1000 + Math.floor((ts.nanos ?? 0) / 1e6);
}

function num(value: unknown): number | undefined {
  if (value === undefined || value === null) return undefined;
  const n = Number(typeof value === 'object' ? String(value) : value);
  return Number.isFinite(n) ? n : undefined;
}

interface FailureLike {
  applicationFailureInfo?: { type?: string | null } | null;
}

/** An error class name is an identifier; anything else is free text and is dropped. */
const IDENTIFIER = /^[A-Za-z0-9_.]{1,60}$/;

export function failureTypeOf(failure: FailureLike | null | undefined): string | undefined {
  const type = failure?.applicationFailureInfo?.type;
  return type && IDENTIFIER.test(type) ? type : undefined;
}

export interface HistoryEventLike {
  eventId?: unknown;
  eventTime?: Ts;
  activityTaskScheduledEventAttributes?: {
    activityType?: { name?: string | null } | null;
    taskQueue?: { name?: string | null } | null;
  } | null;
  activityTaskCompletedEventAttributes?: { scheduledEventId?: unknown } | null;
  activityTaskFailedEventAttributes?: {
    scheduledEventId?: unknown;
    failure?: FailureLike | null;
  } | null;
  activityTaskTimedOutEventAttributes?: { scheduledEventId?: unknown } | null;
}

/**
 * Folds a workflow's history into one record per activity. An activity that
 * was retried appears once: the history holds the scheduled event and the final
 * outcome, and the attempts in between live only on the pending activity.
 */
export function activitiesFromHistory(events: readonly HistoryEventLike[]): ActivityRecord[] {
  const byScheduledId = new Map<number, ActivityRecord>();
  for (const event of events) {
    const at = tsToMs(event.eventTime);
    const eventId = num(event.eventId);
    if (at === undefined) continue;
    const scheduled = event.activityTaskScheduledEventAttributes;
    if (scheduled && eventId !== undefined) {
      byScheduledId.set(eventId, {
        eventId,
        name: scheduled.activityType?.name ?? '',
        queue: scheduled.taskQueue?.name ?? '',
        scheduledAtMs: at,
      });
      continue;
    }
    const completed = event.activityTaskCompletedEventAttributes;
    if (completed) {
      const record = byScheduledId.get(num(completed.scheduledEventId) ?? -1);
      if (record) record.completedAtMs = at;
      continue;
    }
    const failed = event.activityTaskFailedEventAttributes;
    if (failed) {
      const record = byScheduledId.get(num(failed.scheduledEventId) ?? -1);
      if (record) {
        record.failedAtMs = at;
        record.failureType = failureTypeOf(failed.failure) ?? 'ActivityFailure';
      }
      continue;
    }
    const timedOut = event.activityTaskTimedOutEventAttributes;
    if (timedOut) {
      const record = byScheduledId.get(num(timedOut.scheduledEventId) ?? -1);
      if (record) {
        record.failedAtMs = at;
        record.failureType = 'TimeoutError';
      }
    }
  }
  return [...byScheduledId.values()].sort((a, b) => a.scheduledAtMs - b.scheduledAtMs);
}

interface PendingActivityLike {
  activityId?: string | null;
  activityType?: { name?: string | null } | null;
  attempt?: number | null;
  scheduledTime?: Ts;
  lastStartedTime?: Ts;
  lastFailure?: FailureLike | null;
}

export function pendingFromDescribe(
  pending: readonly PendingActivityLike[] | null | undefined,
): PendingActivity[] {
  const out: PendingActivity[] = [];
  for (const item of pending ?? []) {
    const atMs = tsToMs(item.lastStartedTime) ?? tsToMs(item.scheduledTime);
    if (atMs === undefined) continue;
    out.push({
      activityId: item.activityId ?? '',
      name: item.activityType?.name ?? '',
      attempt: Math.max(item.attempt ?? 1, 1),
      atMs,
      failureType: failureTypeOf(item.lastFailure),
    });
  }
  return out;
}

/**
 * Live reader over `@temporalio/client`. A workflow's history is fetched again
 * only when `describe` reports a longer one, because most polls land between
 * two workflow tasks and the history has not moved.
 */
export class TemporalWorkflowReader implements TemporalReader {
  private readonly history = new Map<string, { length: number; activities: ActivityRecord[] }>();

  constructor(private readonly client: Client) {}

  async listOpen(limit: number): Promise<string[]> {
    const ids: string[] = [];
    const open = this.client.workflow.list({ query: 'ExecutionStatus = "Running"' });
    for await (const info of open) {
      ids.push(info.workflowId);
      if (ids.length >= limit) break;
    }
    for (const known of this.history.keys()) {
      if (!ids.includes(known)) this.history.delete(known);
    }
    return ids;
  }

  async find(workflowId: string): Promise<WorkflowInspection | null> {
    try {
      return await this.inspect(workflowId);
    } catch (err) {
      if (err instanceof Error && err.name === 'WorkflowNotFoundError') return null;
      throw err;
    }
  }

  async inspect(workflowId: string): Promise<WorkflowInspection> {
    const handle = this.client.workflow.getHandle(workflowId);
    const description = await handle.describe();
    const length = description.historyLength;
    let cached = this.history.get(workflowId);
    if (!cached || cached.length !== length) {
      const { events } = await handle.fetchHistory();
      cached = { length, activities: activitiesFromHistory((events ?? []) as HistoryEventLike[]) };
      this.history.set(workflowId, cached);
    }
    return {
      workflowId,
      startedAtMs: description.startTime.getTime(),
      pending: pendingFromDescribe(description.raw.pendingActivities as PendingActivityLike[]),
      activities: cached.activities,
    };
  }
}
