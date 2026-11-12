import 'reflect-metadata';
import { UnauthorizedException, type ExecutionContext } from '@nestjs/common';
import { Reflector } from '@nestjs/core';
import { ApiKeyGuard } from '../../src/auth/api-key.guard';
import { Public } from '../../src/auth/public.decorator';
import { SignedMedia } from '../../src/auth/signed-media.decorator';
import { signMediaToken } from '../../src/dashboard/media-token';
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

class SignedController {
  @SignedMedia()
  route(): void {
    /* no-op */
  }
}

function contextFor(
  controller: { prototype: { route: () => void } },
  headers: Record<string, string> = {},
  params: Record<string, string> = {},
): ExecutionContext {
  const request = { headers, params };
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

  describe('signed media route', () => {
    const SIGNING_KEY = 'g'.repeat(40);
    const signedGuard = new ApiKeyGuard(new Reflector(), fixedEnv({ API_KEY, DASHBOARD_MEDIA_SECRET: SIGNING_KEY }));
    const claims = { awemeId: '1', generation: 1, kind: 'final' as const };
    const token = (expiresAtMs: number, key = SIGNING_KEY) => signMediaToken(key, { ...claims, expiresAtMs });

    it('accepts a valid signature in place of the API key', () => {
      const ctx = contextFor(SignedController, {}, { token: token(Date.now() + 60_000) });
      expect(signedGuard.canActivate(ctx)).toBe(true);
    });

    it('rejects an expired, forged or missing signature, and the API key does not stand in for it', () => {
      expect(() => signedGuard.canActivate(contextFor(SignedController, {}, { token: token(Date.now() - 1) }))).toThrow(
        UnauthorizedException,
      );
      expect(() =>
        signedGuard.canActivate(contextFor(SignedController, {}, { token: token(Date.now() + 60_000, 'z'.repeat(40)) })),
      ).toThrow(UnauthorizedException);
      expect(() => signedGuard.canActivate(contextFor(SignedController, {}, {}))).toThrow(UnauthorizedException);
      expect(() => signedGuard.canActivate(contextFor(SignedController, { 'x-api-key': API_KEY }, { token: 'junk' }))).toThrow(
        UnauthorizedException,
      );
    });

    it('rejects every signature when no signing key is configured', () => {
      const ctx = contextFor(SignedController, {}, { token: token(Date.now() + 60_000) });
      expect(() => guard.canActivate(ctx)).toThrow(UnauthorizedException);
    });

    it('does not let a signature open a route that is not marked', () => {
      const ctx = contextFor(ProtectedController, {}, { token: token(Date.now() + 60_000) });
      expect(() => signedGuard.canActivate(ctx)).toThrow(UnauthorizedException);
    });
  });
});
