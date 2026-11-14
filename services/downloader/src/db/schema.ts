import { sql } from 'drizzle-orm';
import {
  bigint,
  bigserial,
  boolean,
  index,
  integer,
  jsonb,
  pgTable,
  primaryKey,
  real,
  smallint,
  text,
  timestamp,
  unique,
  uniqueIndex,
  uuid,
} from 'drizzle-orm/pg-core';

/**
 * Phase 1 schema (solution design §6, plan phase-01 §1.1).
 *
 * This file and migrations/260824-init-schema.sql describe the same tables
 * and must stay in lockstep; test/db/schema-migration.spec.ts compares the
 * two in both directions on every run, so a column added here without a
 * matching migration line (or vice versa) fails the build rather than a
 * lane at runtime.
 *
 * Status values are plain `text`, not Postgres enums, on purpose: adding a
 * value to an enum is a migration, and the lane statuses here are still
 * being discovered through phases 4-7.
 */

/** Timestamps are timestamptz everywhere. Lease arithmetic is done against
 * the DATABASE clock (`now()`), never the app's -- two lane processes on
 * hosts with a few seconds of skew would otherwise disagree about whether a
 * lease has expired, and the reaper would either steal live work or leave
 * orphans behind. */
const tstz = (name: string) => timestamp(name, { withTimezone: true, mode: 'date' });

export const profiles = pgTable('profiles', {
  secUserId: text('sec_user_id').primaryKey(),
  nickname: text('nickname'),
  lastSyncedAt: tstz('last_synced_at'),
  /** Per-channel switch: localize finished downloads without waiting for an admin. */
  autoLocalize: boolean('auto_localize').notNull().default(false),
});

/**
 * A Douyin 合集 (`mix`), a channel (`channel`) or one made by hand (`manual`).
 * Every video belongs to exactly one, and the collection carries the settings a
 * run of its videos is rendered with and the folder they are saved to on the Mac.
 * Value rules (hex colours, 0.5 dB steps, a relative `save_subdir`) are CHECK
 * constraints in migrations/261114-collections.sql.
 */
export const collections = pgTable(
  'collections',
  {
    id: uuid('id').primaryKey().defaultRandom(),
    /** channel·mix·manual */
    kind: text('kind').notNull(),
    /** `sec_user_id` for a channel, `mix_id` for a 合集, NULL for a manual one. */
    externalId: text('external_id'),
    secUserId: text('sec_user_id').references(() => profiles.secUserId),
    name: text('name').notNull(),
    bgmOffsetDb: real('bgm_offset_db'),
    captionTextColor: text('caption_text_color'),
    coverColor: text('cover_color'),
    coverOpacityPct: smallint('cover_opacity_pct'),
    /** Relative to REVIEW_WORKER_EXPORT_ROOT on the Mac. */
    saveSubdir: text('save_subdir'),
    createdAt: tstz('created_at').notNull().defaultNow(),
    updatedAt: tstz('updated_at').notNull().defaultNow(),
  },
  (table) => [
    unique('collections_kind_external_id_key').on(table.kind, table.externalId),
  ],
);

export const jobs = pgTable(
  'jobs',
  {
    id: uuid('id').primaryKey().defaultRandom(),
    secUserId: text('sec_user_id')
      .notNull()
      .references(() => profiles.secUserId),
    /** pending·enumerating·downloading·completed·failed·blocked·cancelled */
    status: text('status').notNull().default('pending'),
    /** NULL until enumerate finishes and knows the real total. */
    totalVideos: integer('total_videos'),
    /** Job-local: how many items THIS run enumerated. Contrast the two
     * counters below, which are derived from profile state (F7). */
    enumeratedCount: integer('enumerated_count').notNull().default(0),
    downloadedCount: integer('downloaded_count').notNull().default(0),
    failedCount: integer('failed_count').notNull().default(0),
    /** Enumerate checkpoint, written once per page (phase 5). */
    maxCursor: bigint('max_cursor', { mode: 'number' }).notNull().default(0),
    /** F5. Without this, a crash mid-enumerate strands the job in
     * `enumerating` forever: the lane only ever picks up `pending`, so the
     * job is dead with no error and no way back. The column costs nothing
     * now and is a migration later, which is why it lands in phase 1
     * alongside the reaper index below. */
    leaseExpiresAt: tstz('lease_expires_at'),
    /** Operator-facing failure reason. Never contains a cookie (plan §0.3). */
    error: text('error'),
    /** Collection picked by hand for every video this job brings in; NULL = automatic. */
    collectionId: uuid('collection_id').references(() => collections.id),
    createdAt: tstz('created_at').notNull().defaultNow(),
    updatedAt: tstz('updated_at').notNull().defaultNow(),
  },
  (table) => [
    index('jobs_status_created_idx').on(table.status, table.createdAt),
    index('jobs_enumerating_lease_idx')
      .on(table.status, table.leaseExpiresAt)
      .where(sql`${table.status} = 'enumerating'`),
    index('jobs_sec_user_id_idx').on(table.secUserId),
  ],
);

/**
 * F7 -- `videos` deliberately has NO `job_id`, and must never gain one.
 *
 * `aweme_id` is the global primary key across every job and every run. That
 * single choice is what makes incremental re-runs free: a second job over
 * the same profile re-inserts nothing it already has, so only genuinely new
 * videos are `pending`. There is no separate "have I seen this before?"
 * bookkeeping because the primary key already answers it.
 *
 * The consequence -- and the reason this comment exists -- is that job
 * progress is measured against PROFILE state, not against a per-job delta.
 * A second job on a profile with no new videos shows `340/340` the instant
 * it starts. That is correct information ("everything is already
 * downloaded"), not a bug, and the obvious "fix" for it is to add `job_id`
 * here so each job can count its own rows. Doing that re-scopes the primary
 * key per job, re-downloads everything on every run, and destroys the
 * dedupe. If per-job attribution is genuinely needed later, add a separate
 * join table; do not add a column here.
 *
 * schema-migration.spec.ts asserts both halves of this: that no `job_id`
 * column exists, and that this explanation is still attached to the table.
 *
 * `collection_id` does not contradict F7: it is a property of the VIDEO (which
 * 合集 or channel it belongs to, and so how it is rendered and where it is saved),
 * the same whichever job enumerated it, not a record of the job that did.
 */
export const videos = pgTable(
  'videos',
  {
    awemeId: text('aweme_id').primaryKey(),
    secUserId: text('sec_user_id').notNull(),
    /** `desc` is a reserved word in Postgres; it only works quoted. Drizzle
     * quotes every identifier, and the migration file quotes it by hand. */
    desc: text('desc'),
    createTime: tstz('create_time').notNull(),
    durationMs: integer('duration_ms'),
    /** F1. Cached from the enumerate response instead of re-resolved before
     * every download: doing it per-video costs ~525 requests for a
     * 500-video profile against ~25, which is the difference between
     * working and being rate-limited into a ban. Paired with
     * `url_expires_at` and a REACTIVE re-resolve on expiry/403/410
     * (phase 6), never a pre-emptive one. */
    playAddr: text('play_addr'),
    urlExpiresAt: tstz('url_expires_at'),
    storagePath: text('storage_path'),
    /** pending·downloading·done·failed·skipped */
    status: text('status').notNull().default('pending'),
    /** F6. Incremented at CLAIM time, not at failure time -- see
     * videos.repository.ts claimOne() for why, and for the tradeoff that
     * choice makes. */
    attempts: integer('attempts').notNull().default(0),
    lastError: text('last_error'),
    /** F3. Same role as jobs.lease_expires_at: without it a worker that dies
     * mid-download leaves the row in `downloading` with nobody working on
     * it, forever. */
    leaseExpiresAt: tstz('lease_expires_at'),
    /** Per-video override of the channel switch; NULL follows the channel. */
    localizeOnDone: boolean('localize_on_done'),
    collectionId: uuid('collection_id')
      .notNull()
      .references(() => collections.id),
    /** Episode number inside its 合集; NULL outside one. */
    mixEpisode: integer('mix_episode'),
    /** A person chose the collection; enumerate never moves this video. */
    collectionManual: boolean('collection_manual').notNull().default(false),
  },
  (table) => [
    /** Claim path. Partial so the index holds only outstanding work: at
     * 100k videos with 200 left to do, this is a 200-entry index, and it
     * shrinks as the job progresses rather than growing. */
    index('videos_pending_claim_idx')
      .on(table.status, table.createTime)
      .where(sql`${table.status} = 'pending'`),
    /** Reaper path (F3). Partial for the same reason. */
    index('videos_downloading_lease_idx')
      .on(table.status, table.leaseExpiresAt)
      .where(sql`${table.status} = 'downloading'`),
    /** Browse + progress counting. Not partial: it must cover every status. */
    index('videos_profile_recent_idx').on(table.secUserId, table.createTime.desc()),
    index('videos_collection_recent_idx').on(table.collectionId, table.createTime.desc()),
  ],
);

/**
 * One row per finished download awaiting pickup by the localization side.
 * Written by `markDone` in the same transaction that flips the video to
 * `done`. Deliberately a plain `pgTable`, not `pgSchema(...)`: queries must
 * follow `search_path` so each test run gets its own copy.
 *
 * `generation` is bumped to re-run a video; `state` is
 * pending | held | dispatched | rejected | dead. Only `pending` is dispatched.
 */
export const localizationOutbox = pgTable(
  'localization_outbox',
  {
    awemeId: text('aweme_id')
      .primaryKey()
      .references(() => videos.awemeId),
    generation: integer('generation').notNull().default(1),
    state: text('state').notNull().default('pending'),
    enqueuedAt: tstz('enqueued_at').notNull().defaultNow(),
    nextAttemptAt: tstz('next_attempt_at').notNull().defaultNow(),
    dispatchedAt: tstz('dispatched_at'),
    workflowId: text('workflow_id'),
    dispatchAttempts: integer('dispatch_attempts').notNull().default(0),
    lastError: text('last_error'),
  },
  (table) => [
    index('localization_outbox_due_idx')
      .on(table.nextAttemptAt)
      .where(sql`${table.state} = 'pending'`),
  ],
);

/** Admin decision on a review revision. Written only by the downloader. */
export const reviewDecisions = pgTable(
  'review_decisions',
  {
    awemeId: text('aweme_id').notNull(),
    generation: integer('generation').notNull(),
    /** approved | returned */
    decision: text('decision').notNull(),
    approvedRevision: text('approved_revision'),
    confirmMismatch: boolean('confirm_mismatch').notNull().default(false),
    note: text('note'),
    decidedAt: tstz('decided_at').notNull().defaultNow(),
  },
  (table) => [primaryKey({ columns: [table.awemeId, table.generation] })],
);

export const publications = pgTable(
  'publications',
  {
    awemeId: text('aweme_id').notNull(),
    generation: integer('generation').notNull(),
    /** unpublished | scheduled | publishing | published | failed */
    state: text('state').notNull().default('unpublished'),
    platform: text('platform'),
    postUrl: text('post_url'),
    note: text('note'),
    updatedAt: tstz('updated_at').notNull().defaultNow(),
  },
  (table) => [primaryKey({ columns: [table.awemeId, table.generation] })],
);

/** Dashboard asks the host worker to open Studio for a review project. */
export const studioOpenRequests = pgTable('studio_open_requests', {
  id: bigserial('id', { mode: 'number' }).primaryKey(),
  awemeId: text('aweme_id').notNull(),
  generation: integer('generation').notNull(),
  /** pending | done | failed */
  state: text('state').notNull().default('pending'),
  error: text('error'),
  requestedAt: tstz('requested_at').notNull().defaultNow(),
  handledAt: tstz('handled_at'),
});

export const dashboardActions = pgTable(
  'dashboard_actions',
  {
    id: bigserial('id', { mode: 'number' }).primaryKey(),
    awemeId: text('aweme_id').notNull(),
    action: text('action').notNull(),
    detail: jsonb('detail').notNull().default({}),
    createdAt: tstz('created_at').notNull().defaultNow(),
  },
  (table) => [index('dashboard_actions_aweme_idx').on(table.awemeId, table.createdAt.desc())],
);

/**
 * A request to save a finished video into its collection's folder on the Mac.
 * Written by the downloader; the Mac worker (role `localization`) claims it with
 * `state`/`claim`/`lease_expires_at` and settles it. Target folder and file name
 * are captured when the request is made.
 */
export const folderExports = pgTable(
  'folder_exports',
  {
    id: bigserial('id', { mode: 'number' }).primaryKey(),
    awemeId: text('aweme_id')
      .notNull()
      .references(() => videos.awemeId),
    generation: integer('generation').notNull(),
    collectionId: uuid('collection_id')
      .notNull()
      .references(() => collections.id),
    targetSubdir: text('target_subdir').notNull(),
    /** `E{episode:02d}-{aweme_id}` or `{aweme_id}`. */
    fileStem: text('file_stem').notNull(),
    finalKey: text('final_key').notNull(),
    srtKey: text('srt_key'),
    /** pending | exporting | done | failed */
    state: text('state').notNull().default('pending'),
    claim: uuid('claim'),
    leaseExpiresAt: tstz('lease_expires_at'),
    error: text('error'),
    files: jsonb('files'),
    requestedAt: tstz('requested_at').notNull().defaultNow(),
    handledAt: tstz('handled_at'),
  },
  (table) => [
    /** The worker's claim, in id order, over open requests only. */
    index('folder_exports_open_idx')
      .on(table.id)
      .where(sql`${table.state} IN ('pending','exporting')`),
    /** At most one open request per video generation. */
    uniqueIndex('folder_exports_one_open_idx')
      .on(table.awemeId, table.generation)
      .where(sql`${table.state} IN ('pending','exporting')`),
  ],
);

export type Profile = typeof profiles.$inferSelect;
export type Job = typeof jobs.$inferSelect;
export type Video = typeof videos.$inferSelect;
