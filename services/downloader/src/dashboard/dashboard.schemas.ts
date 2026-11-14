import { z } from 'zod';
import { STAGES } from './dashboard.stage';
import { GENERATION_MEDIA_KINDS } from './media-token';

/** A TikTok/Douyin video id: digits only, as the workspace validates it. */
export const AWEME_ID = /^\d{1,32}$/;

export const awemeParamsSchema = z.object({ awemeId: z.string().regex(AWEME_ID) }).strict();
export const awemeIdsSchema = z.array(z.string().regex(AWEME_ID)).min(1).max(100);

const isoInstant = z
  .string()
  .max(40)
  .refine((v) => !Number.isNaN(Date.parse(v)), { message: 'must be an ISO date or datetime' })
  .transform((v) => new Date(v).toISOString());

/** A date-only `to` means that whole day, so it becomes the last millisecond of it. */
const isoUpperBound = z
  .string()
  .max(40)
  .refine((v) => !Number.isNaN(Date.parse(v)), { message: 'must be an ISO date or datetime' })
  .transform((v) =>
    /^\d{4}-\d{2}-\d{2}$/.test(v) ? new Date(Date.parse(v) + 86_400_000 - 1).toISOString() : new Date(v).toISOString(),
  );

const booleanFlag = z.enum(['true', 'false', '1', '0']).transform((v) => v === 'true' || v === '1');

/** Query of `GET /api/dashboard/videos`. Every value reaches SQL as a bound parameter. */
export const videosQuerySchema = z
  .object({
    stage: z.enum(STAGES).optional(),
    channel: z.string().min(1).max(200).optional(),
    q: z.string().min(1).max(100).optional(),
    from: isoInstant.optional(),
    to: isoUpperBound.optional(),
    attention: booleanFlag.optional(),
    collection: z
      .string()
      .uuid()
      .transform((v) => v.toLowerCase())
      .optional(),
    limit: z.coerce.number().int().min(1).max(200).default(50),
    offset: z.coerce.number().int().min(0).max(1_000_000).default(0),
  })
  .strict();
export type VideosQuery = z.infer<typeof videosQuerySchema>;

/** The SQL-side part of the list filter; `stage` and `attention` need Temporal and run afterwards. */
export interface VideoFilter {
  channel?: string;
  q?: string;
  from?: string;
  to?: string;
  awemeId?: string;
  collection?: string;
}

/** `%` and `_` are LIKE wildcards; a search for them must match them literally. */
export function escapeLike(value: string): string {
  return value.replace(/[\\%_]/g, (c) => `\\${c}`);
}

export const mediaLinksSchema = z
  .object({
    aweme_id: z.string().regex(AWEME_ID),
    generation: z.number().int().min(0).max(999_999_999),
    kinds: z.array(z.enum(GENERATION_MEDIA_KINDS)).min(1).max(GENERATION_MEDIA_KINDS.length),
  })
  .strict();
export type MediaLinksBody = z.infer<typeof mediaLinksSchema>;

/** The kinds a page of videos asks for at once; each is the same for every generation. */
export const BATCH_MEDIA_KINDS = ['thumb'] as const;

export const mediaLinksBatchSchema = z
  .object({
    aweme_ids: awemeIdsSchema,
    kinds: z.array(z.enum(BATCH_MEDIA_KINDS)).min(1).max(BATCH_MEDIA_KINDS.length),
  })
  .strict();
export type MediaLinksBatchBody = z.infer<typeof mediaLinksBatchSchema>;

export const mediaQuerySchema = z.object({ download: booleanFlag.optional() }).strict();

/** Every action the dashboard can run on a video. */
export const ACTIONS = [
  'localize',
  'retry_download',
  'resume',
  'rerun',
  'approve',
  'return',
  'rerender',
  'mark_published',
  'mark_unpublished',
  'open_studio',
  'save_to_folder',
] as const;
export type DashboardAction = (typeof ACTIONS)[number];

export const actionBodySchema = z
  .object({
    action: z.enum(ACTIONS),
    aweme_ids: awemeIdsSchema,
    note: z.string().max(2000).optional(),
    post_url: z.string().url().max(2048).regex(/^https?:\/\//).optional(),
    platform: z.string().min(1).max(50).optional(),
    revision: z.string().min(1).max(200).optional(),
    /**
     * The generation the admin was looking at; the action is skipped when it is no longer current.
     * `save_to_folder` instead saves exactly this generation, current or not, when it was rendered;
     * without it, the current one.
     */
    generation: z.number().int().min(0).max(999_999_999).optional(),
    confirm_mismatch: z.boolean().optional(),
  })
  .strict()
  .superRefine((v, ctx) => {
    if (v.action === 'approve' && v.revision === undefined) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, path: ['revision'], message: 'approve needs the revision being viewed' });
    }
    // One revision string cannot be right for several videos, and approving a revision the
    // admin has not looked at is not a bulk operation.
    if ((v.action === 'open_studio' || v.action === 'approve') && new Set(v.aweme_ids).size !== 1) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, path: ['aweme_ids'], message: `${v.action} takes exactly one video` });
    }
    // `save_to_folder` saves the generation it names, and one number cannot be right for several
    // videos; a bulk save leaves it out and each video saves its current generation.
    if (v.action === 'save_to_folder' && v.generation !== undefined && new Set(v.aweme_ids).size !== 1) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, path: ['generation'], message: 'a generation names exactly one video' });
    }
  });
export type ActionBody = z.infer<typeof actionBodySchema>;

/** `PATCH /api/dashboard/videos/collection`: put videos in a collection by hand. */
export const moveCollectionBodySchema = z
  .object({ aweme_ids: awemeIdsSchema, collection_id: z.string().uuid() })
  .strict();
export type MoveCollectionBody = z.infer<typeof moveCollectionBodySchema>;
