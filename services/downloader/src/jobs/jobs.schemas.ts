import { z } from 'zod';

/**
 * FIX 10 (review) -- SSRF guard.
 *
 * `jobs.service.ts`'s `createFromUrl` hands this value VERBATIM to
 * `ParserClient.resolveSecUserId`, which makes the parser fork itself issue
 * the outbound HTTP request (it follows `v.douyin.com` short-link
 * redirects). `docker-compose.yml` deliberately gives the parser container
 * no `ports:` -- it is an unauthenticated scraper by design, reachable only
 * from inside the compose network. A `url` field validated only as
 * "non-empty" hands an authenticated caller of THIS API a way to make the
 * parser fetch an arbitrary address one hop removed from this service's own
 * `X-Api-Key` check -- e.g. `http://169.254.169.254/latest/meta-data/`, a
 * cloud metadata endpoint, or any other host on the compose network.
 *
 * Kept as one named, exported constant, deliberately short, so a future
 * maintainer who hits a rejected regional Douyin domain can see at a glance
 * that the rejection is a deliberate SSRF guard -- extend this list, do not
 * relax the check that uses it.
 */
export const ALLOWED_JOB_URL_HOSTS = ['douyin.com', 'iesdouyin.com'] as const;

/**
 * True for an exact allowed host or any subdomain of one (`www.douyin.com`,
 * `v.douyin.com`), false for a lookalike that merely CONTAINS the domain as
 * a substring (`notdouyin.com`, `douyin.com.evil.com`) -- the dot-boundary
 * check (`host === allowed` or `host.endsWith('.' + allowed)`) is what rules
 * those out; a bare `.includes()` would not.
 */
function isAllowedJobUrlHost(host: string): boolean {
  return ALLOWED_JOB_URL_HOSTS.some((allowed) => host === allowed || host.endsWith(`.${allowed}`));
}

/**
 * http(s) only, host on the {@link ALLOWED_JOB_URL_HOSTS} allowlist. Both
 * checks matter: a scheme check alone still lets `http://169.254.169.254/`
 * through, and a host check alone would not stop `file:///etc/passwd` or
 * similar non-network schemes the underlying HTTP client might still act on
 * in some form.
 */
function isAllowedJobUrl(value: string): boolean {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return false;
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return false;
  return isAllowedJobUrlHost(url.hostname);
}

export const createJobSchema = z.object({
  url: z
    .string()
    .min(1, 'url is required')
    .refine(isAllowedJobUrl, {
      message: `url must be an http(s) URL on one of: ${ALLOWED_JOB_URL_HOSTS.join(', ')} (or a subdomain)`,
    }),
});
export type CreateJobDto = z.infer<typeof createJobSchema>;

/**
 * `limit`/`offset` bounds from phase-03's DoD exactly: default 20, max 100
 * for `limit`; default 0, min 0 for `offset`. `z.coerce.number()` is
 * required, not cosmetic -- query-string values arrive as strings, and a
 * bare `z.number()` would reject every request that did not omit the
 * parameter entirely.
 */
export const paginationSchema = z.object({
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
});
export type PaginationDto = z.infer<typeof paginationSchema>;

/**
 * Validates a `:id` route param as a UUID BEFORE it reaches a repository
 * call. Without this, a non-UUID `:id` (a typo, a short-link slug pasted
 * into the wrong field) reaches Postgres and raises `22P02 invalid input
 * syntax for type uuid`, which Nest has no special handling for and which
 * therefore surfaces as an opaque 500 instead of the 400 the caller's own
 * bad input deserves.
 */
export const uuidParamSchema = z.string().uuid('must be a valid UUID');
