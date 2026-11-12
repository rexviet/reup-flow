import { GetObjectCommand, S3Client } from '@aws-sdk/client-s3';
import {
  BadGatewayException,
  HttpException,
  HttpStatus,
  Inject,
  Injectable,
  Logger,
  NotFoundException,
  ServiceUnavailableException,
} from '@nestjs/common';
import type { Pool } from 'pg';
import type { Readable } from 'node:stream';
import { ENV } from '../config/config.module';
import type { Env } from '../config/env';
import { PG_POOL } from '../db/db.module';
import { MEDIA_KINDS, MEDIA_TOKEN_TTL_MS, signMediaToken, verifyMediaToken, type MediaKind } from './media-token';

const log = new Logger('DashboardMedia');

export const MEDIA_READER = 'DASHBOARD_MEDIA_READER';

export interface MediaObject {
  status: 200 | 206;
  body: Readable;
  contentLength?: number;
  contentRange?: string;
  contentType?: string;
}

/** Thrown when the requested byte range lies outside the object. */
export class RangeNotSatisfiableError extends Error {}

/** Read only. Returns `null` when the object does not exist (or the credential cannot see it). */
export interface MediaReader {
  open(key: string, range?: string): Promise<MediaObject | null>;
}

/**
 * Its own read-only S3 client rather than `Storage`: that class has no read method and
 * always writes under `S3_KEY_PREFIX` (`source/`). It uses the downloader's credential and
 * only the configured bucket; the bucket is never taken from the database or the request.
 */
export class S3MediaReader implements MediaReader {
  constructor(
    private readonly client: S3Client,
    private readonly bucket: string,
  ) {}

  async open(key: string, range?: string): Promise<MediaObject | null> {
    try {
      const out = await this.client.send(new GetObjectCommand({ Bucket: this.bucket, Key: key, Range: range }));
      if (!out.Body) return null;
      return {
        status: range !== undefined && out.ContentRange !== undefined ? 206 : 200,
        body: out.Body as Readable,
        contentLength: out.ContentLength,
        contentRange: out.ContentRange,
        contentType: out.ContentType,
      };
    } catch (err) {
      const name = (err as { name?: string }).name;
      const http = (err as { $metadata?: { httpStatusCode?: number } }).$metadata?.httpStatusCode;
      // MinIO answers 403 for a missing object when the credential has no ListBucket there.
      if (name === 'NoSuchKey' || name === 'NotFound' || http === 404) return null;
      if (name === 'AccessDenied' || http === 403) {
        // Also what a missing policy looks like, so say so instead of a silent 404.
        log.warn(`media read denied for key ${key}: the downloader policy may lack s3:GetObject there`);
        return null;
      }
      if (name === 'InvalidRange' || http === 416) throw new RangeNotSatisfiableError();
      throw err;
    }
  }
}

export function createS3MediaReader(env: Env): MediaReader | null {
  if (env.STORAGE_BACKEND !== 's3') return null;
  const client = new S3Client({
    endpoint: env.S3_ENDPOINT,
    region: env.S3_REGION,
    forcePathStyle: env.S3_FORCE_PATH_STYLE,
    credentials: {
      accessKeyId: env.S3_ACCESS_KEY_ID as string,
      secretAccessKey: env.S3_SECRET_ACCESS_KEY as string,
    },
    requestHandler: { connectionTimeout: 10_000, requestTimeout: 120_000 },
  });
  return new S3MediaReader(client, env.S3_BUCKET);
}

const SOURCE_KEY = /^source\/[A-Za-z0-9._-]+(?:\/[A-Za-z0-9._-]+)*$/;
const noDotSegments = (key: string): boolean => key.split('/').every((s) => s !== '.' && s !== '..');

export interface ResolvedKey {
  key: string;
  ext: 'mp4' | 'srt';
  /** Shown to the admin next to the link. */
  note?: string;
}

/**
 * Rebuilds the object key from the database and accepts only the three known shapes:
 * `source/...` of this bucket, `output/review/<aweme>/g<gen>/<file>`, and the legacy
 * `output/<aweme>.mp4`. A key anywhere else in the bucket is never served, whatever a
 * row says.
 */
export async function resolveMediaKey(
  pool: Pool,
  bucket: string,
  awemeId: string,
  generation: number,
  kind: MediaKind,
): Promise<ResolvedKey | null> {
  if (kind === 'source') {
    const { rows } = await pool.query(`SELECT storage_path FROM videos WHERE aweme_id = $1 AND status = 'done'`, [
      awemeId,
    ]);
    const path: unknown = rows[0]?.storage_path;
    if (typeof path !== 'string') return null;
    const match = /^s3:\/\/([^/]+)\/(.+)$/.exec(path);
    if (!match || match[1] !== bucket) return null;
    const key = match[2];
    return SOURCE_KEY.test(key) && noDotSegments(key) ? { key, ext: 'mp4' } : null;
  }

  const reviewDir = `output/review/${awemeId}/g${generation}/`;
  const { rows } = await pool.query(
    `SELECT final_key, srt_key FROM localization.final_renders
      WHERE aweme_id = $1 AND generation = $2 AND state = 'rendered'`,
    [awemeId, generation],
  );
  const stored: unknown = rows[0]?.[kind === 'srt' ? 'srt_key' : 'final_key'];
  if (typeof stored === 'string') {
    const ok = stored.startsWith(reviewDir) && /^[A-Za-z0-9._-]+$/.test(stored.slice(reviewDir.length));
    if (ok && noDotSegments(stored)) return { key: stored, ext: kind === 'srt' ? 'srt' : 'mp4' };
  }
  if (kind === 'final') {
    const legacy = await pool.query(
      `SELECT 1 FROM localization.runs WHERE aweme_id = $1 AND generation = $2 AND output_key = $3`,
      [awemeId, generation, `output/${awemeId}.mp4`],
    );
    if (legacy.rows.length > 0) {
      return { key: `output/${awemeId}.mp4`, ext: 'mp4', note: 'Bản cũ, không gắn generation.' };
    }
  }
  return null;
}

export interface MediaLink {
  kind: MediaKind;
  available: boolean;
  url?: string;
  expires_at?: string;
  note?: string;
}

export interface MediaStream {
  object: MediaObject;
  filename: string;
  contentType: string;
}

/** Only a single `bytes=a-b` range is forwarded; anything else is a plain full read. */
export function sanitizeRange(header: unknown): string | undefined {
  return typeof header === 'string' && /^bytes=\d*-\d*$/.test(header) && header !== 'bytes=-' ? header : undefined;
}

@Injectable()
export class DashboardMediaService {
  constructor(
    @Inject(PG_POOL) private readonly pool: Pool,
    @Inject(ENV) private readonly env: Env,
    @Inject(MEDIA_READER) private readonly reader: MediaReader | null,
    private readonly clock: () => number,
  ) {}

  private secret(): string {
    if (this.reader === null) {
      throw new ServiceUnavailableException('Media cần STORAGE_BACKEND=s3.');
    }
    if (this.env.DASHBOARD_MEDIA_SECRET === undefined) {
      throw new ServiceUnavailableException('Chưa đặt DASHBOARD_MEDIA_SECRET.');
    }
    return this.env.DASHBOARD_MEDIA_SECRET;
  }

  /** One signed URL per requested kind that exists; the rest come back `available: false`. */
  async links(awemeId: string, generation: number, kinds: readonly MediaKind[]): Promise<{ links: MediaLink[] }> {
    const secret = this.secret();
    const expiresAtMs = this.clock() + MEDIA_TOKEN_TTL_MS;
    const links: MediaLink[] = [];
    for (const kind of [...new Set(kinds)]) {
      const resolved = await resolveMediaKey(this.pool, this.env.S3_BUCKET, awemeId, generation, kind);
      if (!resolved) {
        links.push({ kind, available: false });
        continue;
      }
      const token = signMediaToken(secret, { awemeId, generation, kind, expiresAtMs });
      links.push({
        kind,
        available: true,
        url: `/api/dashboard/media/${token}`,
        expires_at: new Date(expiresAtMs).toISOString(),
        ...(resolved.note ? { note: resolved.note } : {}),
      });
    }
    return { links };
  }

  /** Opens the object a valid token names. The guard has already checked the signature; this re-checks it. */
  async open(token: string, rangeHeader: unknown): Promise<MediaStream> {
    const secret = this.secret();
    const claims = verifyMediaToken(secret, token, this.clock());
    if (!claims) throw new HttpException('Unauthorized', HttpStatus.UNAUTHORIZED);
    const resolved = await resolveMediaKey(
      this.pool,
      this.env.S3_BUCKET,
      claims.awemeId,
      claims.generation,
      claims.kind,
    );
    if (!resolved) throw new NotFoundException('Không có tệp này.');
    let object: MediaObject | null;
    try {
      object = await (this.reader as MediaReader).open(resolved.key, sanitizeRange(rangeHeader));
    } catch (err) {
      if (err instanceof RangeNotSatisfiableError) {
        throw new HttpException('Range Not Satisfiable', HttpStatus.REQUESTED_RANGE_NOT_SATISFIABLE);
      }
      throw new BadGatewayException('Không đọc được kho media.');
    }
    if (!object) throw new NotFoundException('Không có tệp này.');
    return {
      object,
      filename: `${claims.awemeId}-g${claims.generation}.${resolved.ext}`,
      // From the file kind, never from the object's metadata: uploads set none, and a stored
      // type must not decide what the dashboard's origin serves.
      contentType: resolved.ext === 'srt' ? 'application/x-subrip; charset=utf-8' : 'video/mp4',
    };
  }
}

export { MEDIA_KINDS };
