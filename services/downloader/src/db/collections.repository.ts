import { and, eq, inArray, sql } from 'drizzle-orm';
import type { Executor } from './executor';
import { collections, profiles } from './schema';
import * as videosRepository from './videos.repository';
import type { UpsertOptions, VideoUpsert } from './videos.repository';

/**
 * What an enumerated item says about where it belongs. `toVideoUpsert`
 * (parser/aweme.mapper.ts) returns exactly this alongside the row; the mix
 * name and nickname arrive already cleaned for display.
 */
export interface CollectionHints {
  /** The 合集 from `mix_info`; null when the item names none. */
  mix: { mixId: string; mixName: string; episode: number | null } | null;
  /** The channel name from `author.nickname`; null when Douyin sent none. */
  authorNickname?: string | null;
}

export interface ResolveCollectionsInput {
  /** The channel the items were read from (enumerate) or posted by (single video). */
  secUserId: string;
  /** Display name for the channel collection and an empty `profiles.nickname`. */
  channelName: string | null;
  /** A collection a person picked for these videos; it must exist. */
  manualCollectionId: string | null;
  items: readonly CollectionHints[];
}

/** One item's placement, in the shape `upsertMany` takes. */
export interface CollectionAssignment {
  collectionId: string;
  mixEpisode: number | null;
  collectionManual: boolean;
}

export interface ResolvedCollections {
  /** The channel's default collection, which always exists after resolving. */
  channelCollectionId: string;
  /** Where each new item goes, in input order: the hand pick when there is one. */
  assignments: CollectionAssignment[];
  /**
   * Where each item belongs ignoring any hand pick (its 合集, else the
   * channel), in input order. Used to move videos that were already stored.
   */
  automatic: CollectionAssignment[];
}

/** A hand-picked collection id names no collection. Callers answer 400. */
export class CollectionNotFoundError extends Error {
  constructor(readonly collectionId: string) {
    super(`collection ${collectionId} does not exist`);
    this.name = 'CollectionNotFoundError';
  }
}

export async function collectionExists(exec: Executor, id: string): Promise<boolean> {
  const rows = await exec
    .select({ id: collections.id })
    .from(collections)
    .where(eq(collections.id, id))
    .limit(1);
  return rows.length > 0;
}

/**
 * Decides the collection of each item, in this order: the collection a person
 * picked, then the item's 合集, then the channel's default collection.
 *
 * Every collection is created with `INSERT ... ON CONFLICT (kind, external_id)
 * DO NOTHING` and then read back with a SELECT, never with `RETURNING`: when
 * two writers create the same collection at once, the second one's insert
 * waits for the first, does nothing, and must still learn the id. Under READ
 * COMMITTED the following SELECT sees the row the other writer committed.
 *
 * No transaction is required. Each statement is idempotent, so a failure part
 * way leaves at most an empty collection that the next run reuses.
 *
 * The channel collection is created even when every item goes elsewhere: a
 * channel's glossary is keyed to it.
 *
 * Names: a 合集 takes its cleaned `mix_name`, a channel its nickname. Either
 * is renamed later only while its name still equals its `external_id` -- the
 * placeholder used when no name was known -- so a name a person chose is kept.
 */
export async function resolveCollections(
  exec: Executor,
  input: ResolveCollectionsInput,
): Promise<ResolvedCollections> {
  const { secUserId, channelName, manualCollectionId, items } = input;

  if (manualCollectionId !== null && !(await collectionExists(exec, manualCollectionId))) {
    throw new CollectionNotFoundError(manualCollectionId);
  }

  const channelCollectionId = await ensureChannelCollection(exec, secUserId, channelName);

  // Created even under a hand pick: a video already stored can still move
  // into its 合集 (only videos a person placed are exempt).
  const mixIds = await ensureMixCollections(exec, secUserId, items);
  const automatic = items.map(
    (item): CollectionAssignment =>
      item.mix === null
        ? { collectionId: channelCollectionId, mixEpisode: null, collectionManual: false }
        : {
            collectionId: mixIds.get(item.mix.mixId)!,
            mixEpisode: item.mix.episode,
            collectionManual: false,
          },
  );

  return {
    channelCollectionId,
    automatic,
    assignments:
      manualCollectionId === null
        ? automatic
        : items.map((item) => ({
            collectionId: manualCollectionId,
            // The episode is a fact about the video, kept wherever it is filed.
            mixEpisode: item.mix?.episode ?? null,
            collectionManual: true,
          })),
  };
}

export interface IntakeInput {
  secUserId: string;
  manualCollectionId: string | null;
  rows: readonly (VideoUpsert & CollectionHints)[];
}

/**
 * The one way videos enter the table, for enumerate and for a single video:
 * resolve collections, insert the new rows into them, then move rows already
 * stored in the channel collection into the 合集 their item names. Returns how
 * many rows were new.
 *
 * A hand-picked collection applies to the NEW rows. Rows already stored still
 * move into their 合集 like on any run, unless a person placed them
 * (`moveIntoMixes`); a caller that means the pick for an existing video (the
 * single-video form) applies it itself.
 */
export async function intakeVideos(
  exec: Executor,
  input: IntakeInput,
  opts: UpsertOptions,
): Promise<number> {
  const { rows } = input;
  if (rows.length === 0) return 0;

  const resolved = await resolveCollections(exec, {
    secUserId: input.secUserId,
    // The mapper falls back to the sec_user_id for a name that cleans to
    // nothing; that is no name at all for this purpose.
    channelName:
      rows.find((row) => row.authorNickname && row.authorNickname !== input.secUserId)
        ?.authorNickname ?? null,
    manualCollectionId: input.manualCollectionId,
    items: rows,
  });

  const placed = rows.map((row, i) => ({ ...row, ...resolved.assignments[i]! }));
  const inserted = await videosRepository.upsertMany(exec, placed, opts);

  await videosRepository.moveIntoMixes(
    exec,
    resolved.channelCollectionId,
    rows.flatMap((row, i) => {
      const home = resolved.automatic[i]!;
      return row.mix === null
        ? []
        : [{ awemeId: row.awemeId, collectionId: home.collectionId, episode: home.mixEpisode }];
    }),
  );
  return inserted;
}

/**
 * The channel's default collection, created on first use. Also fills an empty
 * `profiles.nickname` and renames a collection still named after its id.
 * Throws when the channel has no profile: a collection's owner must exist.
 */
async function ensureChannelCollection(
  exec: Executor,
  secUserId: string,
  channelName: string | null,
): Promise<string> {
  if (channelName !== null) {
    await exec.execute(sql`
      UPDATE ${profiles} SET nickname = ${channelName}
       WHERE ${profiles.secUserId} = ${secUserId} AND ${profiles.nickname} IS NULL`);
  }

  // Named like the migration's backfill: the stored nickname, else the id.
  await exec.execute(sql`
    INSERT INTO ${collections} (kind, external_id, sec_user_id, name)
    SELECT 'channel', ${profiles.secUserId}, ${profiles.secUserId},
           coalesce(nullif(btrim(left(${profiles.nickname}, 200)), ''), left(${profiles.secUserId}, 200))
      FROM ${profiles}
     WHERE ${profiles.secUserId} = ${secUserId}
    ON CONFLICT (kind, external_id) DO NOTHING`);

  if (channelName !== null) {
    await exec.execute(sql`
      UPDATE ${collections} SET name = ${channelName}, updated_at = now()
       WHERE kind = 'channel' AND external_id = ${secUserId}
         AND name = left(external_id, 200) AND name <> ${channelName}`);
  }

  const [row] = await exec
    .select({ id: collections.id })
    .from(collections)
    .where(and(eq(collections.kind, 'channel'), eq(collections.externalId, secUserId)));
  if (!row) {
    throw new Error(`channel ${secUserId} has no profile, so it cannot own a collection`);
  }
  return row.id;
}

/**
 * The collection of every 合集 named by `items`, created on first use and
 * owned by `secUserId`. Returns mix id -> collection id.
 *
 * Inserted in mix-id order so that two writers creating overlapping sets wait
 * on each other in the same order instead of deadlocking.
 */
async function ensureMixCollections(
  exec: Executor,
  secUserId: string,
  items: readonly CollectionHints[],
): Promise<Map<string, string>> {
  // First usable name per 合集: one that is not the id placeholder.
  const names = new Map<string, string>();
  for (const { mix } of items) {
    if (mix === null) continue;
    const known = names.get(mix.mixId);
    if (known === undefined || (known === mix.mixId && mix.mixName !== mix.mixId)) {
      names.set(mix.mixId, mix.mixName);
    }
  }
  if (names.size === 0) return new Map();

  const ordered = [...names.entries()].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  const values = sql.join(
    ordered.map(([mixId, name]) => sql`('mix', ${mixId}, ${secUserId}, ${name})`),
    sql`, `,
  );
  await exec.execute(sql`
    INSERT INTO ${collections} (kind, external_id, sec_user_id, name)
    VALUES ${values}
    ON CONFLICT (kind, external_id) DO NOTHING`);

  const renames = ordered.filter(([mixId, name]) => name !== mixId);
  if (renames.length > 0) {
    const renameValues = sql.join(
      renames.map(([mixId, name]) => sql`(${mixId}, ${name})`),
      sql`, `,
    );
    await exec.execute(sql`
      UPDATE ${collections} AS c SET name = r.name, updated_at = now()
        FROM (VALUES ${renameValues}) AS r (external_id, name)
       WHERE c.kind = 'mix' AND c.external_id = r.external_id
         AND c.name = left(c.external_id, 200)`);
  }

  const rows = await exec
    .select({ id: collections.id, mixId: collections.externalId })
    .from(collections)
    .where(and(eq(collections.kind, 'mix'), inArray(collections.externalId, [...names.keys()])));
  const ids = new Map(rows.map((row) => [row.mixId!, row.id]));
  for (const mixId of names.keys()) {
    if (!ids.has(mixId)) throw new Error(`合集 ${mixId} has no collection after it was created`);
  }
  return ids;
}
