import { z } from 'zod';
import { isAllowedJobUrl } from '../jobs/jobs.schemas';
import { extractAwemeId } from './video-url';

/**
 * Same `limit`/`offset` bounds as jobs.schemas.ts's `paginationSchema`
 * (default 20 / max 100, default 0 / min 0) plus an optional `status`
 * filter. `status` is intentionally a bare non-empty string, not a
 * `z.enum` of known values -- `videos.status` is plain `text` in the schema
 * (see the comment in schema.ts on why: new statuses are still being
 * discovered through phases 4-7), so pinning this to a closed enum would
 * make the query route reject a legitimate status the day one is added
 * elsewhere and this file is not updated in lockstep.
 */
export const videoListQuerySchema = z.object({
  limit: z.coerce
    .number({ invalid_type_error: 'limit must be a number' })
    .int('limit must be an integer')
    .min(1, 'limit must be at least 1')
    .max(100, 'limit must be at most 100')
    .default(20),
  offset: z.coerce
    .number({ invalid_type_error: 'offset must be a number' })
    .int('offset must be an integer')
    .min(0, 'offset must be at least 0')
    .default(0),
  status: z.string().min(1, 'status must not be empty').optional(),
});
export type VideoListQueryDto = z.infer<typeof videoListQuerySchema>;

/**
 * `POST /api/videos`. Same host allowlist as `POST /api/jobs` (it is the same
 * SSRF concern: the URL's id ends up in a request the parser makes), plus the
 * requirement that the URL actually name one video.
 */
export const createVideoSchema = z.object({
  url: z
    .string()
    .min(1, 'url is required')
    .refine(isAllowedJobUrl, { message: 'url must be an http(s) douyin.com URL' })
    .refine((value) => extractAwemeId(value) !== null, {
      message: 'url must point at one video (/video/<id> or ?modal_id=<id>)',
    }),
  /** true: localize as soon as it is downloaded; false: hold it once it finishes (a row already
   * pending is not taken back). Omitted follows the channel. */
  localize: z.boolean().optional(),
  /** A collection picked by hand; it must exist. Omitted: the video's 合集, else its channel. */
  collection_id: z.string().uuid('collection_id must be a valid UUID').optional(),
});
export type CreateVideoDto = z.infer<typeof createVideoSchema>;
