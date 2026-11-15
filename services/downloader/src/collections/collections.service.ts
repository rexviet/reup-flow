import { BadRequestException, Inject, Injectable, NotFoundException } from '@nestjs/common';
import type { Pool } from 'pg';
import { PG_POOL } from '../db/db.module';
import {
  SETTING_KEYS,
  type CreateCollectionBody,
  type UpdateCollectionBody,
} from './collections.schemas';
import type { GlossaryTerm } from './glossary-term';

/** What the dashboard sees of one collection. */
export interface CollectionView {
  id: string;
  kind: string;
  external_id: string | null;
  name: string;
  /** The channel the collection belongs to; null for a manual one made without a channel. */
  channel: { sec_user_id: string; nickname: string | null } | null;
  video_count: number;
  bgm_offset_db: number | null;
  caption_text_color: string | null;
  cover_color: string | null;
  cover_opacity_pct: number | null;
  /** Cover box centre shift, % of frame height (positive = down); null = automatic. */
  cover_offset_pct: number | null;
  /** Cover box width, % of frame width; null = automatic. */
  cover_width_pct: number | null;
  /** Cover box height, % of frame height; null = automatic. */
  cover_height_pct: number | null;
  save_subdir: string | null;
  created_at: string;
  updated_at: string;
}

export interface GlossaryTermView {
  term_zh: string;
  term_vi: string;
  note: string | null;
  /** `dashboard` for a term a person set, `agent` for one the Localizer wrote. */
  source: 'dashboard' | 'agent';
  /** The video the agent learnt the term from; null for a dashboard term. */
  source_aweme_id: string | null;
  created_at: string;
}

/** `workflow_id` and `source_aweme_id` of a term set on the dashboard (no column marks it otherwise). */
export const DASHBOARD_SOURCE = 'dashboard';

const FOREIGN_KEY_VIOLATION = '23503';

/** True for a foreign key violation on `constraint` (any constraint when omitted). */
export function isForeignKeyViolation(err: unknown, constraint?: string): boolean {
  if (typeof err !== 'object' || err === null) return false;
  const e = err as { code?: unknown; constraint?: unknown };
  return e.code === FOREIGN_KEY_VIOLATION && (constraint === undefined || e.constraint === constraint);
}

const SELECT_COLLECTIONS = `
  SELECT c.id, c.kind, c.external_id, c.name, c.sec_user_id, p.nickname,
         c.bgm_offset_db, c.caption_text_color, c.cover_color, c.cover_opacity_pct,
         c.cover_offset_pct, c.cover_width_pct, c.cover_height_pct, c.save_subdir,
         c.created_at, c.updated_at,
         (SELECT count(*)::int FROM videos v WHERE v.collection_id = c.id) AS video_count
    FROM collections c
    LEFT JOIN profiles p ON p.sec_user_id = c.sec_user_id`;

// A pg row of SELECT_COLLECTIONS.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function toCollection(r: Record<string, any>): CollectionView {
  return {
    id: r.id,
    kind: r.kind,
    external_id: r.external_id,
    name: r.name,
    channel: r.sec_user_id === null ? null : { sec_user_id: r.sec_user_id, nickname: r.nickname },
    video_count: r.video_count,
    bgm_offset_db: r.bgm_offset_db,
    caption_text_color: r.caption_text_color,
    cover_color: r.cover_color,
    cover_opacity_pct: r.cover_opacity_pct,
    cover_offset_pct: r.cover_offset_pct,
    cover_width_pct: r.cover_width_pct,
    cover_height_pct: r.cover_height_pct,
    save_subdir: r.save_subdir,
    created_at: r.created_at.toISOString(),
    updated_at: r.updated_at.toISOString(),
  };
}

// A pg row of localization.glossary.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function toTerm(r: Record<string, any>): GlossaryTermView {
  const fromDashboard = r.workflow_id === DASHBOARD_SOURCE;
  return {
    term_zh: r.term_zh,
    term_vi: r.term_vi,
    note: r.note,
    source: fromDashboard ? 'dashboard' : 'agent',
    source_aweme_id: fromDashboard ? null : r.source_aweme_id,
    created_at: r.created_at.toISOString(),
  };
}

/**
 * Collections and their glossary, for the dashboard. Plain SQL on the pool like
 * `DashboardRepository`: `localization.glossary` is written by the Python side and
 * `db/schema.ts` does not describe it.
 *
 * Nothing here deletes a collection. A write that names a collection or channel
 * that does not exist is answered 404 or 400 from the foreign key violation, not
 * from a check made earlier, so a row removed in between cannot turn into a 500.
 */
@Injectable()
export class CollectionsService {
  constructor(@Inject(PG_POOL) private readonly pool: Pool) {}

  async list(): Promise<{ items: CollectionView[] }> {
    const { rows } = await this.pool.query(`${SELECT_COLLECTIONS} ORDER BY c.name, c.id`);
    return { items: rows.map(toCollection) };
  }

  private async one(id: string): Promise<CollectionView> {
    const { rows } = await this.pool.query(`${SELECT_COLLECTIONS} WHERE c.id = $1`, [id]);
    if (rows.length === 0) throw new NotFoundException(`collection ${id} not found`);
    return toCollection(rows[0]);
  }

  async create(body: CreateCollectionBody): Promise<CollectionView> {
    let id: string;
    try {
      const { rows } = await this.pool.query<{ id: string }>(
        `INSERT INTO collections (kind, name, sec_user_id, bgm_offset_db, caption_text_color, cover_color,
                                  cover_opacity_pct, cover_offset_pct, cover_width_pct, cover_height_pct,
                                  save_subdir)
         VALUES ('manual', $1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id`,
        [
          body.name,
          body.sec_user_id ?? null,
          body.bgm_offset_db ?? null,
          body.caption_text_color ?? null,
          body.cover_color ?? null,
          body.cover_opacity_pct ?? null,
          body.cover_offset_pct ?? null,
          body.cover_width_pct ?? null,
          body.cover_height_pct ?? null,
          body.save_subdir ?? null,
        ],
      );
      id = rows[0].id;
    } catch (err) {
      if (isForeignKeyViolation(err)) {
        throw new BadRequestException([`sec_user_id: channel ${body.sec_user_id} has no profile`]);
      }
      throw err;
    }
    return this.one(id);
  }

  async update(id: string, body: UpdateCollectionBody): Promise<CollectionView> {
    const sets: string[] = [];
    const params: unknown[] = [id];
    // Column names come from this fixed list, never from the request.
    for (const key of ['name', ...SETTING_KEYS] as const) {
      const value = body[key];
      if (value === undefined) continue;
      params.push(value);
      sets.push(`${key} = $${params.length}`);
    }
    const moved = await this.pool.query(
      `UPDATE collections SET ${sets.join(', ')}, updated_at = now() WHERE id = $1`,
      params,
    );
    if (moved.rowCount !== 1) throw new NotFoundException(`collection ${id} not found`);
    return this.one(id);
  }

  async glossary(collectionId: string): Promise<{ items: GlossaryTermView[] }> {
    const [exists, terms] = await Promise.all([
      this.pool.query(`SELECT 1 FROM collections WHERE id = $1`, [collectionId]),
      this.pool.query(
        `SELECT term_zh, term_vi, note, source_aweme_id, workflow_id, created_at
           FROM localization.glossary WHERE collection_id = $1 ORDER BY term_zh`,
        [collectionId],
      ),
    ]);
    if (exists.rows.length === 0) throw new NotFoundException(`collection ${collectionId} not found`);
    return { items: terms.rows.map(toTerm) };
  }

  /**
   * Adds or replaces a term. A person's choice wins over the agent's: the row
   * becomes a dashboard term, and the worker's `ON CONFLICT DO NOTHING` never
   * overwrites it.
   */
  async putTerm(collectionId: string, term: GlossaryTerm): Promise<GlossaryTermView> {
    try {
      const { rows } = await this.pool.query(
        `INSERT INTO localization.glossary (collection_id, term_zh, term_vi, note, source_aweme_id, workflow_id)
         VALUES ($1, $2, $3, $4, $5, $5)
         ON CONFLICT (collection_id, term_zh) DO UPDATE
           SET term_vi = EXCLUDED.term_vi, note = EXCLUDED.note,
               source_aweme_id = EXCLUDED.source_aweme_id, workflow_id = EXCLUDED.workflow_id
         RETURNING term_zh, term_vi, note, source_aweme_id, workflow_id, created_at`,
        [collectionId, term.term_zh, term.term_vi, term.note, DASHBOARD_SOURCE],
      );
      return toTerm(rows[0]);
    } catch (err) {
      if (isForeignKeyViolation(err)) throw new NotFoundException(`collection ${collectionId} not found`);
      throw err;
    }
  }

  async deleteTerm(collectionId: string, termZh: string): Promise<void> {
    const removed = await this.pool.query(
      `DELETE FROM localization.glossary WHERE collection_id = $1 AND term_zh = $2`,
      [collectionId, termZh],
    );
    if (removed.rowCount === 0) throw new NotFoundException(`term not found in collection ${collectionId}`);
  }
}
