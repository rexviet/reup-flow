import { createHmac, timingSafeEqual } from 'node:crypto';

/** The files of one generation of a video. */
export const GENERATION_MEDIA_KINDS = ['source', 'final', 'srt', 'poster'] as const;
/**
 * `thumb` is the still of the source video, the same for every generation, so its tokens carry
 * generation 0 and the server rebuilds its key from the video id alone.
 */
export const MEDIA_KINDS = [...GENERATION_MEDIA_KINDS, 'thumb'] as const;
export type MediaKind = (typeof MEDIA_KINDS)[number];
export type GenerationMediaKind = (typeof GENERATION_MEDIA_KINDS)[number];

/** A link lives long enough to watch a video, and no longer. */
export const MEDIA_TOKEN_TTL_MS = 10 * 60 * 1000;
/** Thumbnail links expire on the next boundary of this window after the TTL. */
export const MEDIA_TOKEN_WINDOW_MS = 10 * 60 * 1000;

/**
 * An expiry at least the TTL away, rounded up to the window, so every link signed for the same
 * file within one window is the same URL and the browser can serve it from its cache.
 */
export function windowedExpiry(nowMs: number): number {
  return Math.ceil((nowMs + MEDIA_TOKEN_TTL_MS) / MEDIA_TOKEN_WINDOW_MS) * MEDIA_TOKEN_WINDOW_MS;
}

export interface MediaClaims {
  awemeId: string;
  generation: number;
  kind: MediaKind;
  expiresAtMs: number;
}

// Built from the kinds, so a new kind cannot be signed yet refused when it comes back.
const TOKEN_PAYLOAD = new RegExp(`^(\\d{1,32})\\.(\\d{1,9})\\.(${MEDIA_KINDS.join('|')})\\.(\\d{1,12})$`);

const b64 = (buf: Buffer): string => buf.toString('base64url');

function mac(secret: string, payload: string): Buffer {
  return createHmac('sha256', secret).update(payload).digest();
}

/**
 * `<payload>.<hmac>`: the payload is the four signed facts, readable by anyone, and the
 * HMAC is what makes them unforgeable. Nothing here names an object key; the server
 * rebuilds the key from the database when the link is used.
 */
export function signMediaToken(secret: string, claims: MediaClaims): string {
  const payload = b64(
    Buffer.from(`${claims.awemeId}.${claims.generation}.${claims.kind}.${Math.floor(claims.expiresAtMs / 1000)}`),
  );
  return `${payload}.${b64(mac(secret, payload))}`;
}

/** Returns the claims of a genuine, unexpired token; `null` for anything else. */
export function verifyMediaToken(secret: string, token: unknown, nowMs: number): MediaClaims | null {
  if (typeof token !== 'string' || token.length > 512) return null;
  const parts = token.split('.');
  if (parts.length !== 2) return null;
  const [payload, signature] = parts;

  const expected = mac(secret, payload);
  const given = Buffer.from(signature, 'base64url');
  if (given.length !== expected.length || !timingSafeEqual(given, expected)) return null;

  const match = TOKEN_PAYLOAD.exec(
    Buffer.from(payload, 'base64url').toString('utf8'),
  );
  if (!match) return null;
  const expiresAtMs = Number(match[4]) * 1000;
  if (expiresAtMs <= nowMs) return null;
  return { awemeId: match[1], generation: Number(match[2]), kind: match[3] as MediaKind, expiresAtMs };
}
