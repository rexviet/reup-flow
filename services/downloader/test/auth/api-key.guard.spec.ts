import 'reflect-metadata';
import { UnauthorizedException, type ExecutionContext } from '@nestjs/common';
import { Reflector } from '@nestjs/core';
import { ApiKeyGuard } from '../../src/auth/api-key.guard';
import { Public } from '../../src/auth/public.decorator';
import { fixedEnv } from '../support/fixed-env';

class ProtectedController {
  route(): void {
    /* no-op */
  }
}

class PublicController {
  @Public()
  route(): void {
    /* no-op */
  }
}

function contextFor(
  controller: { prototype: { route: () => void } },
  headers: Record<string, string> = {},
): ExecutionContext {
  const request = { headers };
  return {
    switchToHttp: () => ({ getRequest: () => request, getResponse: () => ({}) }),
    getHandler: () => controller.prototype.route,
    getClass: () => controller,
  } as unknown as ExecutionContext;
}

describe('ApiKeyGuard', () => {
  const API_KEY = 'the-real-api-key-abc123';
  const guard = new ApiKeyGuard(new Reflector(), fixedEnv({ API_KEY }));

  it('rejects a request with no x-api-key header with 401 Unauthorized', () => {
    const ctx = contextFor(ProtectedController, {});
    expect(() => guard.canActivate(ctx)).toThrow(UnauthorizedException);
  });

  it('rejects a request with the wrong x-api-key with 401 Unauthorized', () => {
    const ctx = contextFor(ProtectedController, { 'x-api-key': 'totally-wrong-key' });
    expect(() => guard.canActivate(ctx)).toThrow(UnauthorizedException);
  });

  it('rejects a wrong key of a different length than the real key without throwing a raw crypto error', () => {
    // Guards against a naive `timingSafeEqual(a, b)` on the raw values,
    // which throws RangeError on a length mismatch instead of producing a
    // clean 401 -- and would leak the real key's length via which inputs
    // throw vs which return false.
    const ctx = contextFor(ProtectedController, { 'x-api-key': 'x' });
    expect(() => guard.canActivate(ctx)).toThrow(UnauthorizedException);
  });

  it('allows a request with the correct x-api-key', () => {
    const ctx = contextFor(ProtectedController, { 'x-api-key': API_KEY });
    expect(guard.canActivate(ctx)).toBe(true);
  });

  it('allows a request on a route marked @Public() with no header at all', () => {
    const ctx = contextFor(PublicController, {});
    expect(guard.canActivate(ctx)).toBe(true);
  });

  it('the 401 error never echoes the supplied key anywhere in its response', () => {
    const suppliedKey = 'sneaky-supplied-key-should-not-leak';
    const ctx = contextFor(ProtectedController, { 'x-api-key': suppliedKey });

    let caught: unknown;
    try {
      guard.canActivate(ctx);
    } catch (err) {
      caught = err;
    }

    expect(caught).toBeInstanceOf(UnauthorizedException);
    const response = (caught as UnauthorizedException).getResponse();
    expect(JSON.stringify(response)).not.toContain(suppliedKey);
  });
});
