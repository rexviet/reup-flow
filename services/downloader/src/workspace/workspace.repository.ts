import { Inject, Injectable } from '@nestjs/common';
import type { Pool } from 'pg';
import { PG_POOL } from '../db/db.module';

export interface LlmCallRow {
  generationId: string;
  awemeId: string;
  generation: number;
  stage: string;
  model: string | null;
  promptTokens: number | null;
  completionTokens: number | null;
  costUsd: number | null;
  createdAtMs: number;
}

export interface RunRow {
  awemeId: string;
  generation: number;
  status: string;
  updatedAtMs: number;
}

export interface RunKey {
  awemeId: string;
  generation: number;
}

export const runKey = (key: RunKey): string => `${key.awemeId}:${key.generation}`;

export interface RunDetailRow {
  status: string;
  workflowId: string;
  updatedAtMs: number;
}

export interface StageCost {
  stage: string;
  calls: number;
  costUsd: number;
}

export interface WorkspaceStore {
  run(key: RunKey): Promise<RunDetailRow | null>;
  /** Cost per stage; the sum over stages is the run's total. */
  costByStage(key: RunKey): Promise<StageCost[]>;
  recentLlmCalls(sinceSeconds: number, limit: number): Promise<LlmCallRow[]>;
  recentRuns(limit: number): Promise<RunRow[]>;
  runCosts(keys: RunKey[]): Promise<Map<string, number>>;
}

/**
 * Reads `localization.llm_calls` and `localization.runs`, tables the Python
 * worker writes and `db/schema.ts` does not describe, so this goes through the
 * pool with plain SQL instead of drizzle. The downloader's role is `reup`
 * (superuser), so no grant is needed; nothing here writes.
 */
@Injectable()
export class WorkspaceRepository implements WorkspaceStore {
  constructor(@Inject(PG_POOL) private readonly pool: Pool) {}

  async run(key: RunKey): Promise<RunDetailRow | null> {
    const { rows } = await this.pool.query(
      `SELECT status, workflow_id, updated_at FROM localization.runs
        WHERE aweme_id = $1 AND generation = $2`,
      [key.awemeId, key.generation],
    );
    const r = rows[0];
    return r ? { status: r.status, workflowId: r.workflow_id, updatedAtMs: r.updated_at.getTime() } : null;
  }

  async costByStage(key: RunKey): Promise<StageCost[]> {
    const { rows } = await this.pool.query(
      `SELECT stage, COUNT(*)::int AS calls, COALESCE(SUM(cost_usd), 0) AS cost
         FROM localization.llm_calls
        WHERE aweme_id = $1 AND generation = $2
        GROUP BY stage
        ORDER BY stage`,
      [key.awemeId, key.generation],
    );
    return rows.map((r) => ({ stage: r.stage, calls: r.calls, costUsd: Number(r.cost) }));
  }

  async recentLlmCalls(sinceSeconds: number, limit: number): Promise<LlmCallRow[]> {
    const { rows } = await this.pool.query(
      `SELECT generation_id, aweme_id, generation, stage, model, prompt_tokens,
              completion_tokens, cost_usd, created_at
         FROM localization.llm_calls
        WHERE created_at > now() - make_interval(secs => $1)
        ORDER BY created_at DESC
        LIMIT $2`,
      [sinceSeconds, limit],
    );
    return rows.map((r) => ({
      generationId: r.generation_id,
      awemeId: r.aweme_id,
      generation: r.generation,
      stage: r.stage,
      model: r.model,
      promptTokens: r.prompt_tokens,
      completionTokens: r.completion_tokens,
      costUsd: r.cost_usd === null ? null : Number(r.cost_usd),
      createdAtMs: r.created_at.getTime(),
    }));
  }

  async recentRuns(limit: number): Promise<RunRow[]> {
    const { rows } = await this.pool.query(
      `SELECT aweme_id, generation, status, updated_at
         FROM localization.runs
        ORDER BY updated_at DESC
        LIMIT $1`,
      [limit],
    );
    return rows.map((r) => ({
      awemeId: r.aweme_id,
      generation: r.generation,
      status: r.status,
      updatedAtMs: r.updated_at.getTime(),
    }));
  }

  /** Total cost per run, summed over `llm_calls` (the same sum the run row stores). */
  async runCosts(keys: RunKey[]): Promise<Map<string, number>> {
    const costs = new Map<string, number>();
    if (keys.length === 0) return costs;
    const { rows } = await this.pool.query(
      `SELECT c.aweme_id, c.generation, COALESCE(SUM(c.cost_usd), 0) AS cost
         FROM localization.llm_calls c
         JOIN unnest($1::text[], $2::int[]) AS k(aweme_id, generation)
           ON c.aweme_id = k.aweme_id AND c.generation = k.generation
        GROUP BY c.aweme_id, c.generation`,
      [keys.map((k) => k.awemeId), keys.map((k) => k.generation)],
    );
    for (const r of rows) costs.set(runKey({ awemeId: r.aweme_id, generation: r.generation }), Number(r.cost));
    return costs;
  }
}
