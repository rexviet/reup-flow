import { z } from 'zod';

export const EVENT_KINDS = [
  'activity_started',
  'activity_retrying',
  'activity_completed',
  'activity_failed',
  'llm_call_recorded',
  'gate_decision',
  'handoff',
] as const;
export type EventKind = (typeof EVENT_KINDS)[number];

/** The run statuses that put a video in the gate area instead of on a desk. */
export const GATE_OUTCOMES = [
  'skipped_ad',
  'skipped_no_video',
  'published_original',
  'needs_human',
] as const;
export type GateOutcome = (typeof GATE_OUTCOMES)[number];

export function isGateOutcome(value: string): value is GateOutcome {
  return (GATE_OUTCOMES as readonly string[]).includes(value);
}

const iso = z.string().datetime({ offset: true });

/**
 * The shape of `GET /api/workspace/snapshot`. Every string a client renders is
 * built by the server from structured fields (a model name, a token count, an
 * error class); nothing here forwards free text from Temporal or a prompt.
 */
export const snapshotSchema = z.object({
  generatedAt: iso,
  sources: z.object({
    temporal: z.enum(['ok', 'down']),
    postgres: z.enum(['ok', 'down']),
  }),
  agents: z.array(
    z.object({
      id: z.string(),
      zone: z.enum(['media', 'voice', 'llm', 'unknown']),
      state: z.enum(['idle', 'working', 'retrying', 'failed']),
      awemeId: z.string().optional(),
      generation: z.number().int().optional(),
      since: iso.optional(),
      attempt: z.number().int().optional(),
    }),
  ),
  events: z.array(
    z.object({
      id: z.string(),
      at: iso,
      kind: z.enum(EVENT_KINDS),
      agentId: z.string().optional(),
      toAgentId: z.string().optional(),
      awemeId: z.string().optional(),
      generation: z.number().int().optional(),
      detail: z.string(),
    }),
  ),
  runs: z.array(
    z.object({
      awemeId: z.string(),
      generation: z.number().int(),
      phase: z.string(),
      outcome: z.enum(GATE_OUTCOMES).optional(),
      llmCostUsd: z.number(),
      updatedAt: iso.optional(),
    }),
  ),
});

export type Snapshot = z.infer<typeof snapshotSchema>;
export type SnapshotAgent = Snapshot['agents'][number];
export type SnapshotEvent = Snapshot['events'][number];
export type SnapshotRun = Snapshot['runs'][number];

/** Route params of `GET /api/workspace/runs/:awemeId/:generation`. */
export const runParamsSchema = z.object({
  awemeId: z.string().regex(/^\d{1,32}$/, 'awemeId must be digits'),
  generation: z.coerce.number().int().min(0).max(1_000_000),
});

export const runDetailSchema = z.object({
  awemeId: z.string(),
  generation: z.number().int(),
  sources: z.object({ temporal: z.enum(['ok', 'down']), postgres: z.enum(['ok', 'down']) }),
  /** `runs.status`; absent while the run has no row yet. */
  status: z.string().optional(),
  outcome: z.enum(GATE_OUTCOMES).optional(),
  /** Individual activities in scheduling order, preserving revisits to a desk. */
  steps: z.array(z.object({
    id: z.string(),
    activity: z.string(),
    agentId: z.string(),
    state: z.enum(['running', 'retrying', 'done', 'failed', 'cancelled']),
    startedAt: iso,
    endedAt: iso.optional(),
    attempt: z.number().int().optional(),
  })),
  phases: z.array(
    z.object({
      agentId: z.string(),
      state: z.enum(['running', 'retrying', 'done', 'failed']),
      startedAt: iso,
      endedAt: iso.optional(),
      activities: z.number().int(),
    }),
  ),
  llmCostUsd: z.number(),
  costByStage: z.array(z.object({ stage: z.string(), calls: z.number().int(), costUsd: z.number() })),
  /**
   * Seconds the `stt` and `tts` activities took, scheduling to completion. An
   * estimate of GPU time, not an invoice: it includes time waiting for a worker.
   */
  gpuSecondsEstimate: z.object({ stt: z.number(), tts: z.number() }),
});
export type RunDetail = z.infer<typeof runDetailSchema>;
