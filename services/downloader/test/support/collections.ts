import type { Pool, PoolClient } from 'pg';

type Queryable = Pick<Pool | PoolClient, 'query'>;

/**
 * The channel collection a video of `secUserId` belongs to, created on first
 * use together with its profile.
 *
 * `videos.collection_id` is NOT NULL, so every fixture row needs one. Production
 * never writes a video without a profile (enumerate runs under a job, which
 * references the profile; the single-video path upserts the profile first) and
 * gives every profile a channel collection, so a fixture does the same instead
 * of inventing an ownerless collection.
 */
export async function channelCollection(db: Queryable, secUserId: string): Promise<string> {
  await db.query(`INSERT INTO profiles (sec_user_id) VALUES ($1) ON CONFLICT DO NOTHING`, [
    secUserId,
  ]);
  await db.query(
    `INSERT INTO collections (kind, external_id, sec_user_id, name)
     VALUES ('channel', $1, $1, left($1, 200))
     ON CONFLICT (kind, external_id) DO NOTHING`,
    [secUserId],
  );
  const { rows } = await db.query<{ id: string }>(
    `SELECT id FROM collections WHERE kind = 'channel' AND external_id = $1`,
    [secUserId],
  );
  return rows[0]!.id;
}
