import { SetMetadata } from '@nestjs/common';

/**
 * Metadata key `ApiKeyGuard` (api-key.guard.ts) reads via
 * `Reflector.getAllAndOverride` to decide whether a route is exempt from the
 * `X-Api-Key` check. `ApiKeyGuard` is registered globally as `APP_GUARD`, so
 * this decorator is the ONLY opt-out mechanism -- phase-3 DoD requires every
 * route, `/api` and `/internal` alike, to 401 without a valid key, and
 * nothing in this codebase currently applies `@Public()` to anything.
 */
export const IS_PUBLIC_KEY = 'isPublic';

export const Public = (): ReturnType<typeof SetMetadata> => SetMetadata(IS_PUBLIC_KEY, true);
