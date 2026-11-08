import { Inject, Injectable } from '@nestjs/common';
import { AGENTS, resolveAgent, UNKNOWN_AGENT } from './workspace.catalog';
import {
  type LlmCallRow,
  type RunKey,
  type RunRow,
  runKey,
  type WorkspaceStore,
  WorkspaceRepository,
} from './workspace.repository';
import { type ActivityRecord, type TemporalReader, type WorkflowInspection } from './workspace.temporal';
import {
  isGateOutcome,
  type Snapshot,
  type SnapshotAgent,
  type SnapshotEvent,
  type RunDetail,
  type SnapshotRun,
} from './workspace.schemas';
import { TEMPORAL_READER } from './workspace.tokens';

/** Open workflows described per snapshot. More than this are not looked at. */
export const MAX_OPEN_WORKFLOWS = 10;
/** The snapshot is reused for this long, so several open tabs cost one read. */
export const CACHE_MS = 2000;
/** An event older than this is not part of the snapshot; the browser keeps its own log. */
export const EVENT_WINDOW_MS = 10 * 60 * 1000;
export const MAX_EVENTS = 50;
/** A failed desk stays red this long after the failure unless work starts on it again. */
export const FAILED_VISIBLE_MS = 30 * 1000;
/** Two activities are a handoff when the second is scheduled this soon after the first completes. */
export const HANDOFF_GAP_MS = 2000;
const MAX_RUNS = 50;
const SOURCE_TIMEOUT_MS = 3000;

/** Names and model ids are identifiers; anything else is not echoed to the page. */
const SAFE_NAME = /^[A-Za-z0-9_.:/@<>-]{1,80}$/;
const safe = (value: string | null | undefined, fallback: string): string =>
  value && SAFE_NAME.test(value) ? value : fallback;

/** `numeric(14,8)` is exact in Postgres; summing doubles is not, so round to the column's scale. */
const roundUsd = (n: number): number => Math.round(n * 1e8) / 1e8;

const iso = (ms: number): string => new Date(ms).toISOString();

const GATE_LABEL: Record<string, string> = {
  skipped_ad: 'skipped: advertisement',
  skipped_no_video: 'skipped: no voice or no video stream',
  published_original: 'published the original',
  needs_human: 'needs a human',
};

function parseWorkflowId(workflowId: string): { awemeId?: string; generation?: number } {
  const match = /^video-(\d+)-g(\d+)$/.exec(workflowId);
  return match ? { awemeId: match[1], generation: Number(match[2]) } : {};
}

export interface SnapshotInput {
  nowMs: number;
  /** `undefined` when the source could not be read. */
  inspections: WorkflowInspection[] | undefined;
  calls: LlmCallRow[] | undefined;
  runs: RunRow[] | undefined;
  /** `undefined` when the cost lookup failed; that counts against Postgres. */
  costs: Map<string, number> | undefined;
}

function activityEvents(inspection: WorkflowInspection, nowMs: number): SnapshotEvent[] {
  const events: SnapshotEvent[] = [];
  const { workflowId } = inspection;
  const ref = parseWorkflowId(workflowId);

  for (const p of inspection.pending) {
    const agent = resolveAgent(p.name);
    const name = safe(p.name, 'activity');
    const base = { agentId: agent.id, ...ref };
    if (p.attempt > 1) {
      events.push({
        ...base,
        id: `retry:${workflowId}:${p.activityId}:${p.attempt}`,
        at: iso(p.atMs),
        kind: 'activity_retrying',
        detail: `${name}: attempt ${p.attempt}${p.failureType ? `, last error ${p.failureType}` : ''}`,
      });
    } else {
      events.push({
        ...base,
        id: `start:${workflowId}:${p.activityId}`,
        at: iso(p.atMs),
        kind: 'activity_started',
        detail: `${name} started`,
      });
    }
  }

  for (const a of inspection.activities) {
    const agent = resolveAgent(a.name);
    const name = safe(a.name, 'activity');
    if (a.completedAtMs !== undefined) {
      const seconds = ((a.completedAtMs - a.scheduledAtMs) / 1000).toFixed(1);
      events.push({
        id: `done:${workflowId}:${a.eventId}`,
        at: iso(a.completedAtMs),
        kind: 'activity_completed',
        agentId: agent.id,
        ...ref,
        detail: `${name} done in ${seconds}s`,
      });
    } else if (a.failedAtMs !== undefined) {
      events.push({
        id: `fail:${workflowId}:${a.eventId}`,
        at: iso(a.failedAtMs),
        kind: 'activity_failed',
        agentId: agent.id,
        ...ref,
        detail: `${name} failed: ${a.failureType ?? 'ActivityFailure'}`,
      });
    }
  }

  events.push(...handoffEvents(inspection, ref));
  return events.filter((e) => nowMs - Date.parse(e.at) <= EVENT_WINDOW_MS);
}

/** For each activity, the activity that completed just before it, when their agents differ. */
function handoffEvents(
  inspection: WorkflowInspection,
  ref: { awemeId?: string; generation?: number },
): SnapshotEvent[] {
  const done = inspection.activities.filter((a): a is ActivityRecord & { completedAtMs: number } => a.completedAtMs !== undefined);
  const events: SnapshotEvent[] = [];
  for (const next of inspection.activities) {
    const nextAgent = resolveAgent(next.name);
    let from: (ActivityRecord & { completedAtMs: number }) | undefined;
    for (const a of done) {
      const gap = next.scheduledAtMs - a.completedAtMs;
      if (a !== next && gap >= 0 && gap <= HANDOFF_GAP_MS && (!from || a.completedAtMs > from.completedAtMs)) from = a;
    }
    if (!from) continue;
    const fromAgent = resolveAgent(from.name);
    if (fromAgent.id === nextAgent.id) continue;
    events.push({
      id: `handoff:${inspection.workflowId}:${next.eventId}`,
      at: iso(next.scheduledAtMs),
      kind: 'handoff',
      agentId: fromAgent.id,
      toAgentId: nextAgent.id,
      ...ref,
      detail: `${fromAgent.id} handed over to ${nextAgent.id}`,
    });
  }
  return events;
}

function agentStates(inspections: WorkflowInspection[], nowMs: number): SnapshotAgent[] {
  const agents: SnapshotAgent[] = [...AGENTS, UNKNOWN_AGENT].map((a) => ({
    id: a.id,
    zone: a.zone,
    state: 'idle' as const,
  }));
  const byId = new Map(agents.map((a) => [a.id, a]));

  for (const inspection of inspections) {
    const ref = parseWorkflowId(inspection.workflowId);
    for (const a of inspection.activities) {
      if (a.failedAtMs === undefined || nowMs - a.failedAtMs > FAILED_VISIBLE_MS) continue;
      const desk = byId.get(resolveAgent(a.name).id);
      if (desk && desk.state === 'idle') {
        Object.assign(desk, { state: 'failed', since: iso(a.failedAtMs), ...ref });
      }
    }
  }
  for (const inspection of inspections) {
    const ref = parseWorkflowId(inspection.workflowId);
    for (const p of inspection.pending) {
      const desk = byId.get(resolveAgent(p.name).id);
      if (!desk) continue;
      const next = p.attempt > 1 ? 'retrying' : 'working';
      const held = desk.state === 'working' || desk.state === 'retrying';
      // A retrying desk outranks a working one; among equals the newest start shows.
      const takes =
        !held ||
        (next === 'retrying' && desk.state === 'working') ||
        (next === desk.state && p.atMs > Date.parse(desk.since ?? '1970-01-01T00:00:00Z'));
      if (takes) {
        Object.assign(desk, { state: next, since: iso(p.atMs), attempt: p.attempt, ...ref });
      }
    }
  }
  // `unknown` is listed only when something landed on it.
  return agents.filter((a) => a.id !== UNKNOWN_AGENT.id || a.state !== 'idle');
}

function llmEvents(calls: LlmCallRow[], nowMs: number): SnapshotEvent[] {
  return calls
    .filter((c) => nowMs - c.createdAtMs <= EVENT_WINDOW_MS)
    .map((c) => {
      const tokens = (c.promptTokens ?? 0) + (c.completionTokens ?? 0);
      const cost = c.costUsd === null ? 'cost unknown' : `$${c.costUsd.toFixed(4)}`;
      return {
        id: `llm:${c.generationId}`,
        at: iso(c.createdAtMs),
        kind: 'llm_call_recorded' as const,
        agentId: resolveAgent(c.stage).id,
        awemeId: c.awemeId,
        generation: c.generation,
        detail: `${safe(c.model, 'unknown model')}, ${tokens} tokens, ${cost}`,
      };
    });
}

function gateEvents(runs: RunRow[], nowMs: number): SnapshotEvent[] {
  return runs
    .filter((r) => isGateOutcome(r.status) && nowMs - r.updatedAtMs <= EVENT_WINDOW_MS)
    .map((r) => ({
      id: `gate:${r.awemeId}:${r.generation}:${r.status}`,
      at: iso(r.updatedAtMs),
      kind: 'gate_decision' as const,
      awemeId: r.awemeId,
      generation: r.generation,
      detail: GATE_LABEL[r.status] ?? r.status,
    }));
}

function runsOf(input: SnapshotInput): SnapshotRun[] {
  const out = new Map<string, SnapshotRun>();
  for (const r of input.runs ?? []) {
    out.set(runKey(r), {
      awemeId: r.awemeId,
      generation: r.generation,
      phase: r.status,
      outcome: isGateOutcome(r.status) ? r.status : undefined,
      llmCostUsd: roundUsd(input.costs?.get(runKey(r)) ?? 0),
      updatedAt: iso(r.updatedAtMs),
    });
  }
  for (const inspection of input.inspections ?? []) {
    const ref = parseWorkflowId(inspection.workflowId);
    if (ref.awemeId === undefined || ref.generation === undefined) continue;
    const key = runKey({ awemeId: ref.awemeId, generation: ref.generation });
    const newest = [...inspection.pending].sort((a, b) => b.atMs - a.atMs)[0];
    // A workflow that is still open is in the phase of its newest pending activity,
    // whatever an older row of the same run says.
    out.set(key, {
      awemeId: ref.awemeId,
      generation: ref.generation,
      phase: newest ? resolveAgent(newest.name).id : 'starting',
      llmCostUsd: roundUsd(input.costs?.get(key) ?? 0),
    });
  }
  return [...out.values()];
}

/** Pure: the same inputs always give the same snapshot. */
export function buildSnapshot(input: SnapshotInput): Snapshot {
  const { nowMs } = input;
  const inspections = input.inspections ?? [];
  const events = [
    ...inspections.flatMap((i) => activityEvents(i, nowMs)),
    ...llmEvents(input.calls ?? [], nowMs),
    ...gateEvents(input.runs ?? [], nowMs),
  ]
    .sort((a, b) => Date.parse(b.at) - Date.parse(a.at) || a.id.localeCompare(b.id))
    .slice(0, MAX_EVENTS);
  return {
    generatedAt: iso(nowMs),
    sources: {
      temporal: input.inspections === undefined ? 'down' : 'ok',
      postgres:
        input.calls === undefined || input.runs === undefined || input.costs === undefined ? 'down' : 'ok',
    },
    agents: agentStates(inspections, nowMs),
    events,
    runs: runsOf(input),
  };
}

/** One phase per agent, in the order the workflow first reached it. */
export function phasesOf(inspection: WorkflowInspection | null): RunDetail['phases'] {
  const groups = new Map<string, ActivityRecord[]>();
  for (const a of inspection?.activities ?? []) {
    const agentId = resolveAgent(a.name).id;
    groups.set(agentId, [...(groups.get(agentId) ?? []), a]);
  }
  return [...groups].map(([agentId, activities]) => {
    const unfinished = activities.filter((a) => a.completedAtMs === undefined && a.failedAtMs === undefined);
    const retrying = unfinished.some(
      (a) => (inspection?.pending.find((p) => p.name === a.name)?.attempt ?? 1) > 1,
    );
    const failed = activities.some((a) => a.failedAtMs !== undefined);
    const ends = activities.map((a) => a.completedAtMs ?? a.failedAtMs);
    const finished = unfinished.length === 0;
    return {
      agentId,
      state: failed ? ('failed' as const) : retrying ? ('retrying' as const) : finished ? ('done' as const) : ('running' as const),
      startedAt: iso(Math.min(...activities.map((a) => a.scheduledAtMs))),
      endedAt: finished ? iso(Math.max(...(ends as number[]))) : undefined,
      activities: activities.length,
    };
  });
}

/** Individual steps rather than grouping all visits to the same desk together. */
export function stepsOf(inspection: WorkflowInspection | null): RunDetail['steps'] {
  return (inspection?.activities ?? []).map<RunDetail['steps'][number]>((a) => {
    const pending = inspection?.pending.find((p) =>
      a.activityId ? p.activityId === a.activityId : p.name === a.name,
    );
    const endedAtMs = a.completedAtMs ?? a.failedAtMs ?? a.cancelledAtMs ?? inspection?.closedAtMs;
    return {
      id: String(a.eventId),
      activity: safe(a.name, 'unknown'),
      agentId: resolveAgent(a.name).id,
      state: a.completedAtMs !== undefined ? 'done' : a.failedAtMs !== undefined ? 'failed' :
        endedAtMs !== undefined ? 'cancelled' :
        (pending?.attempt ?? 1) > 1 ? 'retrying' : 'running',
      startedAt: iso(a.scheduledAtMs),
      endedAt: endedAtMs === undefined ? undefined : iso(endedAtMs),
      attempt: endedAtMs === undefined ? pending?.attempt : undefined,
    };
  });
}

export function gpuSeconds(inspection: WorkflowInspection | null): RunDetail['gpuSecondsEstimate'] {
  const total = { stt: 0, tts: 0 };
  for (const a of inspection?.activities ?? []) {
    if (a.completedAtMs === undefined) continue;
    if (a.queue === 'stt' || a.queue === 'tts') total[a.queue] += (a.completedAtMs - a.scheduledAtMs) / 1000;
  }
  return { stt: Math.round(total.stt * 10) / 10, tts: Math.round(total.tts * 10) / 10 };
}

function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  let timer: NodeJS.Timeout;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error('source timed out')), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

/** A source that fails is `undefined`, never an exception: the page must still load. */
async function attempt<T>(read: () => Promise<T>): Promise<T | undefined> {
  try {
    return await withTimeout(read(), SOURCE_TIMEOUT_MS);
  } catch {
    return undefined;
  }
}

/** A run could not be found, but a source that might hold it could not be read. */
export class RunLookupError extends Error {
  constructor(readonly sources: { temporal: 'ok' | 'down'; postgres: 'ok' | 'down' }) {
    super('a data source is down, so the run could not be looked up');
  }
}

@Injectable()
export class WorkspaceService {
  private cached?: { atMs: number; value: Promise<Snapshot> };

  constructor(
    @Inject(TEMPORAL_READER) private readonly temporal: TemporalReader,
    @Inject(WorkspaceRepository) private readonly store: WorkspaceStore,
    private readonly clock: () => number = Date.now,
  ) {}

  /** Callers inside the cache window share one in-flight or finished read. */
  snapshot(): Promise<Snapshot> {
    const nowMs = this.clock();
    if (this.cached && nowMs - this.cached.atMs < CACHE_MS) return this.cached.value;
    const value = this.read(nowMs);
    this.cached = { atMs: nowMs, value };
    return value;
  }

  /**
   * `null` when neither Temporal nor Postgres knows the run. A run that a down
   * source might hold is not reported as missing: that is `RunLookupError`.
   */
  async runDetail(key: RunKey): Promise<RunDetail | null> {
    const row = await attempt(() => this.store.run(key));
    const workflowId = row?.workflowId ?? `video-${key.awemeId}-g${key.generation}`;
    const found = await attempt(async () => ({ inspection: await this.temporal.find(workflowId) }));
    const stages = await attempt(() => this.store.costByStage(key));
    const sources = {
      temporal: found === undefined ? ('down' as const) : ('ok' as const),
      postgres: row === undefined || stages === undefined ? ('down' as const) : ('ok' as const),
    };
    const inspection = found?.inspection ?? null;
    if (!row && !inspection) {
      if (sources.temporal === 'down' || sources.postgres === 'down') throw new RunLookupError(sources);
      return null;
    }
    const costByStage = stages ?? [];
    return {
      awemeId: key.awemeId,
      generation: key.generation,
      sources,
      status: row?.status,
      outcome: row && isGateOutcome(row.status) ? row.status : undefined,
      phases: phasesOf(inspection),
      steps: stepsOf(inspection),
      llmCostUsd: roundUsd(costByStage.reduce((sum, s) => sum + s.costUsd, 0)),
      costByStage,
      gpuSecondsEstimate: gpuSeconds(inspection),
    };
  }

  private async read(nowMs: number): Promise<Snapshot> {
    const [inspections, calls, runs] = await Promise.all([
      attempt(async () => {
        const ids = await this.temporal.listOpen(MAX_OPEN_WORKFLOWS);
        return Promise.all(ids.map((id) => this.temporal.inspect(id)));
      }),
      attempt(() => this.store.recentLlmCalls(EVENT_WINDOW_MS / 1000, MAX_EVENTS * 2)),
      attempt(() => this.store.recentRuns(MAX_RUNS)),
    ]);
    const keys = new Map<string, RunKey>();
    for (const r of runs ?? []) keys.set(runKey(r), r);
    for (const i of inspections ?? []) {
      const ref = parseWorkflowId(i.workflowId);
      if (ref.awemeId !== undefined && ref.generation !== undefined) {
        keys.set(runKey({ awemeId: ref.awemeId, generation: ref.generation }), {
          awemeId: ref.awemeId,
          generation: ref.generation,
        });
      }
    }
    const costs = await attempt(() => this.store.runCosts([...keys.values()]));
    return buildSnapshot({ nowMs, inspections, calls, runs, costs });
  }
}
