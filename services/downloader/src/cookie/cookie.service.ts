import { Inject, Injectable } from '@nestjs/common';
import type Redis from 'ioredis';
import { REDIS } from '../redis/redis.module';
import { redactor } from '../redaction/redactor';
import { UnsupportedPlatformError } from './cookie.errors';

/**
 * Every platform this endpoint is allowed to write a cookie for.
 *
 * The parser fork reads exactly the literal key `douyin:cookie:current`
 * (crawlers/douyin/web/cookie_provider.py) -- there is no platform-discovery
 * mechanism on that side. Keeping this an exact allowlist (not "anything
 * non-empty") is what turns a typo'd platform into a loud 400 at push time
 * instead of a key nothing ever reads.
 */
export const SUPPORTED_PLATFORMS = ['douyin'] as const;
export type Platform = (typeof SUPPORTED_PLATFORMS)[number];

/** 24h, per phase-3 §3.2. Applied to BOTH keys `push` writes -- see its comment. */
export const COOKIE_TTL_SECONDS = 24 * 60 * 60;

export interface CookieStatus {
  present: boolean;
  /** Null when `pushed_at` is missing or unparseable. NEVER derived from TTL. */
  age_seconds: number | null;
  /** ISO-8601, or null. Nothing writes this in phase 3; see `status()`. */
  last_ok_at: string | null;
}

/** `${platform}:cookie:current` -- the literal key the parser fork reads. */
export function cookieCurrentKey(platform: Platform): string {
  return `${platform}:cookie:current`;
}

/**
 * `${platform}:cookie:pushed_at` -- epoch milliseconds, as a string. The
 * ONLY honest source of a cookie's age; see `push`'s comment.
 */
export function cookiePushedAtKey(platform: Platform): string {
  return `${platform}:cookie:pushed_at`;
}

/**
 * `${platform}:cookie:last_ok_at` -- ISO-8601, written by phases 5/7 when a
 * lane confirms the cookie actually worked. `status()` only ever reads it.
 */
export function cookieLastOkAtKey(platform: Platform): string {
  return `${platform}:cookie:last_ok_at`;
}

/**
 * Runtime guard behind the compile-time `Platform` union: `push`/`status`
 * both take `platform` as a bare `string` because it originates outside this
 * process (an HTTP body), so the type system cannot actually promise it is
 * one of `SUPPORTED_PLATFORMS` -- only this check can.
 */
function assertSupportedPlatform(platform: string): asserts platform is Platform {
  if (!(SUPPORTED_PLATFORMS as readonly string[]).includes(platform)) {
    throw new UnsupportedPlatformError(platform);
  }
}

@Injectable()
export class CookieService {
  constructor(@Inject(REDIS) private readonly redis: Redis) {}

  /**
   * Writes `current` and `pushed_at` together, both with a
   * {@link COOKIE_TTL_SECONDS} TTL.
   *
   * `pushed_at` is not a convenience field. Every push refreshes `current`'s
   * TTL too, so TTL alone can never say how old a cookie is -- only how much
   * longer until Redis evicts it. A `status` endpoint that derived age from
   * TTL would report a cookie pushed a week ago as brand new, and an
   * operator debugging a `blocked` job would conclude the cookie is fine
   * when it is the actual cause. `pushed_at` is the only honest source of
   * age (see `status()`).
   *
   * Both keys are written in one MULTI so there is no window where
   * `current` exists without `pushed_at`: a crash or dropped connection
   * between two separate SETs would leave a cookie of unknowable age on the
   * shelf -- exactly the state `pushed_at` exists to make impossible.
   * `.exec()`'s result is checked command-by-command, not just for a thrown
   * exception: a runtime error on one queued command does not abort the
   * others in the same MULTI (only a queueing-time error does that), so
   * ignoring per-command errors would silently reintroduce the exact
   * partial-write window this method exists to prevent.
   */
  async push(platform: string, cookie: string): Promise<void> {
    assertSupportedPlatform(platform);

    // Registered FIRST -- before this value is passed to anything, before a
    // single Redis command is issued, and before any code below has a chance
    // to throw. Ordering is the whole point and it is not stylistic:
    //
    // This is the exact-value registry (redaction/redactor.ts), independent
    // of the pattern mechanism, and it is the ONLY mechanism that can scrub
    // this particular string -- a cookie need not look like a known
    // `field=value` shape at all (Douyin renames fields; a caller can hand
    // this endpoint any opaque blob), so the pattern cannot be relied on to
    // recognise it. Until this line runs, the value is completely
    // unprotected.
    //
    // Registering at the END of this method -- the obvious placement, as the
    // last step of a successful push -- leaves the entire body as a window
    // in which the cookie is live in memory and NOT yet redactable. Anything
    // that writes in that window leaks it verbatim: a debug line somebody
    // adds later, or, more likely, an ioredis error that embeds the argument
    // it failed on and is then written to stderr by the caller. That window
    // was real and was caught by the must-stay-green control in
    // test/mutations/phase-03.sh, not by any test that existed at the time.
    // Do not move this call down.
    redactor.registerSecret(cookie);

    const results = await this.redis
      .multi()
      .set(cookieCurrentKey(platform), cookie, 'EX', COOKIE_TTL_SECONDS)
      .set(cookiePushedAtKey(platform), String(Date.now()), 'EX', COOKIE_TTL_SECONDS)
      .exec();

    if (results === null) {
      // ioredis returns null when the transaction was aborted before EXEC
      // (e.g. a WATCH failure). This service never WATCHes anything, so
      // seeing null here means something is badly wrong with the
      // connection, not a benign race -- surfacing it beats silently
      // returning as if the write succeeded.
      throw new Error(
        `cookie push for platform "${platform}" aborted: MULTI/EXEC returned null`,
      );
    }
    for (const [err] of results) {
      if (err) {
        throw new Error(
          `cookie push for platform "${platform}" partially failed inside MULTI/EXEC: ${err.message}`,
        );
      }
    }

  }

  /**
   * Never returns the cookie value, in any field, under any name. There is
   * no code path in this method that even reads `current`'s VALUE -- only
   * whether the key exists.
   */
  async status(platform: string): Promise<CookieStatus> {
    assertSupportedPlatform(platform);

    const [presentCount, pushedAtRaw, lastOkAt] = await Promise.all([
      this.redis.exists(cookieCurrentKey(platform)),
      this.redis.get(cookiePushedAtKey(platform)),
      // Read-only here, deliberately: nothing in phase 3 writes this key.
      // Phases 5/7 will set it the first time a lane confirms the pushed
      // cookie actually worked against Douyin.
      this.redis.get(cookieLastOkAtKey(platform)),
    ]);

    return {
      present: presentCount > 0,
      age_seconds: ageSecondsFrom(pushedAtRaw),
      last_ok_at: lastOkAt,
    };
  }
}

/**
 * `pushed_at` is read as epoch milliseconds, never as a TTL: TTL is
 * refreshed by every push (see `push`'s comment) and so can only answer
 * "how much longer until Redis evicts this", never "how old is it".
 */
function ageSecondsFrom(pushedAtRaw: string | null): number | null {
  if (pushedAtRaw === null) return null;
  const pushedAtMs = Number(pushedAtRaw);
  if (!Number.isFinite(pushedAtMs)) return null;
  return Math.floor((Date.now() - pushedAtMs) / 1000);
}
