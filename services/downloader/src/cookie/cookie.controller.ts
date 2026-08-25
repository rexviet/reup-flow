import { BadRequestException, Body, Controller, Get, HttpCode, HttpStatus, Post } from '@nestjs/common';
import { z, type ZodIssue } from 'zod';
import { MIN_REGISTERED_SECRET_LENGTH } from '../redaction/redactor';
import { UnsupportedPlatformError } from './cookie.errors';
import { CookieService, type CookieStatus, SUPPORTED_PLATFORMS } from './cookie.service';

/**
 * Shape-only validation -- both fields are required, non-empty strings.
 * Deliberately NOT the platform allowlist: that decision lives exactly once,
 * in `CookieService` (`assertSupportedPlatform` / `UnsupportedPlatformError`),
 * so duplicating `SUPPORTED_PLATFORMS` into this schema would give "which
 * platforms are supported" two owners that could silently drift.
 *
 * Every message below is a fixed string, never built from the parsed value.
 * `z.string()`'s own default messages ("Required", "String must contain at
 * least 1 character(s)") never interpolate the received value either, but
 * these are pinned explicitly so nobody "improves" this schema later by
 * adding a message that echoes `received` -- see `formatValidationError`,
 * and `src/config/env.ts`'s `parseEnv` for the same trap on `API_KEY`.
 */
const pushCookieBodySchema = z.object({
  platform: z
    .string({ required_error: 'platform is required', invalid_type_error: 'platform must be a string' })
    .min(1, 'platform must not be empty'),
  // FIX 8 (review): floored at MIN_REGISTERED_SECRET_LENGTH, not a bare
  // `.min(1)` -- `Redactor.registerSecret` (redaction/redactor.ts) silently
  // refuses to register anything shorter than that constant, so a cookie
  // this schema accepted below the floor would push successfully and then
  // sit completely unprotected by the exact-value registry the moment it
  // appeared in a log line. Importing the constant directly means the two
  // limits cannot drift apart into two different numbers.
  cookie: z
    .string({ required_error: 'cookie is required', invalid_type_error: 'cookie must be a string' })
    .min(
      MIN_REGISTERED_SECRET_LENGTH,
      `cookie must be at least ${MIN_REGISTERED_SECRET_LENGTH} characters`,
    ),
});

/**
 * Builds the 400 body from zod issue `path` + `message` ONLY -- never from
 * `issue.received` or the raw body. A message that interpolated the
 * received `cookie` value would put the cookie itself into this
 * controller's 400 response, which is exactly the leak phase-3 §3.2 exists
 * to prevent; see `src/config/env.ts`'s `parseEnv` for the identical trap
 * on `API_KEY`.
 */
function formatValidationError(issues: ZodIssue[]): string {
  return issues.map((issue) => `${issue.path.join('.') || '(root)'}: ${issue.message}`).join('; ');
}

@Controller('internal/cookie')
export class CookieController {
  constructor(private readonly cookieService: CookieService) {}

  /**
   * Protected by the global `ApiKeyGuard` (registered as `APP_GUARD` in
   * `AppModule`) like every other route -- deliberately NOT `@Public()`.
   * Phase-3 DoD: missing/wrong `X-Api-Key` must 401 on every route,
   * `/internal` included.
   *
   * `@HttpCode(200)`: this is a control-plane write triggered by an
   * operator running curl (plan.md §7 -- no UI for this in v1), not a
   * REST "resource creation"; 200 with `{ ok: true }` matches the shape of
   * `/internal/cookie/status` below rather than inventing a 201 for a
   * "resource" nothing else in this API ever addresses by id.
   */
  @Post()
  @HttpCode(HttpStatus.OK)
  async push(@Body() body: unknown): Promise<{ ok: true }> {
    const parsed = pushCookieBodySchema.safeParse(body);
    if (!parsed.success) {
      throw new BadRequestException(formatValidationError(parsed.error.issues));
    }

    try {
      await this.cookieService.push(parsed.data.platform, parsed.data.cookie);
    } catch (err) {
      // The allowlist check lives in CookieService, not here (see the
      // schema comment above) -- this is the one place that translates
      // "platform rejected" into the HTTP 400 the phase-3 spec requires.
      if (err instanceof UnsupportedPlatformError) {
        throw new BadRequestException(err.message);
      }
      throw err;
    }

    return { ok: true };
  }

  /**
   * Never returns the cookie value -- see `CookieService.status`'s comment.
   * Protected by the global `ApiKeyGuard` like every other route.
   */
  @Get('status')
  async status(): Promise<CookieStatus> {
    // Hardcoded, not a request parameter: phase-3 §3.1's endpoint list has
    // no `{platform}` segment on this route, and `SUPPORTED_PLATFORMS` has
    // exactly one member today. `CookieService.status` still validates the
    // platform independently, so this stays correct the day a second
    // platform earns its own route instead of a silent behaviour change
    // here.
    return this.cookieService.status(SUPPORTED_PLATFORMS[0]);
  }
}
