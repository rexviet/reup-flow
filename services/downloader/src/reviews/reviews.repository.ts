import { Inject, Injectable } from '@nestjs/common';
import type { Pool } from 'pg';
import { PG_POOL } from '../db/db.module';
import { reviewsSchema, type ReviewList, type ReviewQuery } from './reviews.schemas';

/** DB discovery only; no object-store or Temporal access. Uses the existing DB role. */
@Injectable()
export class ReviewsRepository {
  constructor(@Inject(PG_POOL) private readonly pool: Pool) {}

  async list({ limit, offset }: ReviewQuery): Promise<ReviewList> {
    // A single statement keeps count and page on the same MVCC snapshot, even for an empty page.
    const { rows } = await this.pool.query(`
      WITH available AS (
        SELECT aweme_id, generation, status, project_key, updated_at
        FROM localization.runs
        WHERE status IN ('ready_for_review', 'needs_human') AND project_key IS NOT NULL
      ), page AS (
        SELECT * FROM available ORDER BY updated_at DESC, aweme_id, generation DESC
        LIMIT $1 OFFSET $2
      )
      SELECT (SELECT COUNT(*)::int FROM available) AS total,
        COALESCE((SELECT json_agg(json_build_object(
          'awemeId', aweme_id, 'generation', generation, 'status', status,
          'projectKey', project_key, 'updatedAt', updated_at
        ) ORDER BY updated_at DESC, aweme_id, generation DESC) FROM page), '[]'::json) AS items
    `, [limit, offset]);
    return reviewsSchema.parse({ ...rows[0], limit, offset });
  }
}
