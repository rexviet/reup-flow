import {
  type CanActivate,
  type ExecutionContext,
  Inject,
  Injectable,
  UnauthorizedException,
} from '@nestjs/common';
import { Reflector } from '@nestjs/core';
import { createHash, timingSafeEqual } from 'node:crypto';
import { ENV } from '../config/config.module';
import type { Env } from '../config/env';
import { IS_PUBLIC_KEY } from './public.decorator';

/** Header carrying the API key. Express lowercases every incoming header name. */
const API_KEY_HEADER = 'x-api-key';

/**
 * Registered in `AppModule` as `APP_GUARD`, which means it runs in front of
 * EVERY route -- `/api` and `/internal` alike (phase-3 DoD). There is
 * deliberately no per-controller or per-module opt-out other than
 * `@Public()`: the moment an "internal" route is assumed safe because it is
 * "only called by the extension", it is one refactor away from being
 * reachable the same way everything else is.
 */
@Injectable()
export class ApiKeyGuard implements CanActivate {
  constructor(
    private readonly reflector: Reflector,
    @Inject(ENV) private readonly env: Env,
  ) {}

  canActivate(context: ExecutionContext): boolean {
    const isPublic = this.reflector.getAllAndOverride<boolean>(IS_PUBLIC_KEY, [
      context.getHandler(),
      context.getClass(),
    ]);
    if (isPublic) return true;

    const request = context.switchToHttp().getRequest<{ headers: Record<string, unknown> }>();
    const provided = request.headers[API_KEY_HEADER];

    if (typeof provided !== 'string' || !this.matchesConfiguredKey(provided)) {
      // Explicitly 401, not merely `return false`: a guard returning
      // `false` produces Nest's default 403 Forbidden, and the phase-3 DoD
      // requires 401 specifically. The thrown exception's default body
      // ("Unauthorized") never includes `provided`, so a wrong key can
      // never round-trip back to whoever sent it.
      throw new UnauthorizedException();
    }
    return true;
  }

  /**
   * Constant-time comparison against the configured API_KEY.
   *
   * Hashes BOTH sides to a fixed 32-byte digest before comparing, rather
   * than calling `timingSafeEqual` on the raw values directly.
   * `timingSafeEqual` throws `RangeError` when its two buffers differ in
   * length, and a caller who special-cased that (`if (a.length !==
   * b.length) return false`) would leak the real key's length through an
   * early, cheap return versus the slower path taken for a same-length
   * guess. Hashing first removes the length signal entirely: every input,
   * of any length, produces a fixed-size digest before the constant-time
   * comparison ever runs.
   */
  private matchesConfiguredKey(provided: string): boolean {
    const expectedDigest = createHash('sha256').update(this.env.API_KEY).digest();
    const providedDigest = createHash('sha256').update(provided).digest();
    return timingSafeEqual(expectedDigest, providedDigest);
  }
}
