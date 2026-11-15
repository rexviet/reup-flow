import { Body, Controller, Get, Inject, Put } from '@nestjs/common';
import type { Pool } from 'pg';
import { z } from 'zod';
import { PG_POOL } from '../db/db.module';
import { ZodValidationPipe } from '../jobs/zod-validation.pipe';

export const autoQueueBodySchema = z.object({ enabled: z.boolean() }).strict();
export type AutoQueueBody = z.infer<typeof autoQueueBodySchema>;

export interface AutoQueueStatus {
  enabled: boolean;
  updated_at: string;
  /** The video the queue released and whose run has not ended yet. */
  in_flight: { aweme_id: string; generation: number; released_at: string } | null;
  held: number;
}

/**
 * The switch on the sequential queue (localization migration 261114-auto-queue-switch):
 * while it is on, the end of a run (it reached review, or failed) starts the oldest held
 * video. Off, held videos stay held; the video in flight still runs to its end.
 */
@Controller('api/dashboard/auto-queue')
export class AutoQueueController {
  constructor(@Inject(PG_POOL) private readonly pool: Pool) {}

  @Get()
  status(): Promise<AutoQueueStatus> {
    return this.read(this.pool);
  }

  /** Turning it on releases the next held video at once when nothing is in flight. */
  @Put()
  async set(@Body(new ZodValidationPipe(autoQueueBodySchema)) body: AutoQueueBody): Promise<AutoQueueStatus> {
    const client = await this.pool.connect();
    try {
      await client.query('BEGIN');
      try {
        await client.query('UPDATE auto_queue_settings SET enabled = $1, updated_at = now()', [body.enabled]);
        if (body.enabled) await client.query('SELECT auto_queue_release_next()');
        await client.query('COMMIT');
      } catch (err) {
        await client.query('ROLLBACK');
        throw err;
      }
      return await this.read(client);
    } finally {
      client.release();
    }
  }

  private async read(db: Pick<Pool, 'query'>): Promise<AutoQueueStatus> {
    const { rows } = await db.query<{
      enabled: boolean;
      updated_at: Date;
      aweme_id: string | null;
      generation: number | null;
      released_at: Date | null;
      held: number;
    }>(
      `SELECT s.enabled, s.updated_at, i.aweme_id, i.generation, i.released_at,
              (SELECT count(*)::int FROM localization_outbox WHERE state = 'held') AS held
         FROM auto_queue_settings s
         LEFT JOIN auto_queue_inflight i ON true`,
    );
    const row = rows[0];
    return {
      enabled: row.enabled,
      updated_at: row.updated_at.toISOString(),
      in_flight:
        row.aweme_id !== null && row.generation !== null && row.released_at !== null
          ? { aweme_id: row.aweme_id, generation: row.generation, released_at: row.released_at.toISOString() }
          : null,
      held: row.held,
    };
  }
}
