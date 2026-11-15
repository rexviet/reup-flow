import { createHash, randomUUID } from 'node:crypto';
import { Readable } from 'node:stream';
import {
  BadGatewayException,
  BadRequestException,
  ConflictException,
  Inject,
  Injectable,
  Logger,
  NotFoundException,
  ServiceUnavailableException,
} from '@nestjs/common';
import type { Pool } from 'pg';
import { ENV } from '../config/config.module';
import type { Env } from '../config/env';
import { MEDIA_READER, type MediaObject, type MediaReader } from '../dashboard/dashboard.media';
import { PG_POOL } from '../db/db.module';
import { STORAGE } from '../storage/storage.module';
import type { Storage } from '../storage/storage.interface';
import type { WatermarkMeta } from './collections.schemas';

const log = new Logger('CollectionWatermarks');

/** More images make every run of the collection slower to match; a style needs one or two. */
export const MAX_WATERMARKS_PER_COLLECTION = 10;
/** Below nginx's default 1 MiB request limit; a cut watermark is a few kilobytes. */
export const MAX_WATERMARK_PNG_BYTES = 1_000_000;

const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

/** What the dashboard sees of one watermark image. */
export interface WatermarkView {
  id: string;
  /** The PNG, behind the API key like every route (fetch it, then show it as a blob). */
  image_url: string;
  image_sha256: string;
  frame_width: number;
  frame_height: number;
  aweme_id: string;
  time_ms: number;
  x: number;
  y: number;
  w: number;
  h: number;
  created_at: string;
}

/** Width and height from the IHDR chunk, or null when `data` is not a PNG. */
export function pngSize(data: Buffer): { width: number; height: number } | null {
  if (data.length < 33 || !data.subarray(0, 8).equals(PNG_SIGNATURE)) return null;
  if (data.readUInt32BE(8) !== 13 || data.toString('latin1', 12, 16) !== 'IHDR') return null;
  return { width: data.readUInt32BE(16), height: data.readUInt32BE(20) };
}

/**
 * The image's key under the storage prefix. Under the downloader's own prefix
 * (`S3_KEY_PREFIX`, `source/`) because the downloader already writes there and the
 * localization user already reads there, so no storage policy widens.
 */
function relativeKey(collectionId: string, watermarkId: string): string {
  return `collections/${collectionId}/watermarks/${watermarkId}.png`;
}

// A pg row of collection_watermarks.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function toView(r: Record<string, any>): WatermarkView {
  return {
    id: r.id,
    image_url: `/api/collections/${r.collection_id}/watermarks/${r.id}/image`,
    image_sha256: r.image_sha256,
    frame_width: r.frame_width,
    frame_height: r.frame_height,
    aweme_id: r.aweme_id,
    time_ms: r.time_ms,
    x: r.x,
    y: r.y,
    w: r.w,
    h: r.h,
    created_at: r.created_at.toISOString(),
  };
}

const COLUMNS = `id, collection_id, image_sha256, frame_width, frame_height, aweme_id, time_ms, x, y, w, h, created_at`;

/**
 * The watermark images of a collection: a PNG in object storage and a row naming it.
 * The localization worker reads both (it needs S3, so this does too). A row is written
 * only after its object, and an object whose row never committed is removed again, so
 * a row never names a missing image.
 */
@Injectable()
export class CollectionWatermarksService {
  constructor(
    @Inject(PG_POOL) private readonly pool: Pool,
    @Inject(ENV) private readonly env: Env,
    @Inject(STORAGE) private readonly storage: Storage,
    @Inject(MEDIA_READER) private readonly reader: MediaReader | null,
  ) {}

  async list(collectionId: string): Promise<{ items: WatermarkView[] }> {
    const [exists, rows] = await Promise.all([
      this.pool.query(`SELECT 1 FROM collections WHERE id = $1`, [collectionId]),
      this.pool.query(
        `SELECT ${COLUMNS} FROM collection_watermarks WHERE collection_id = $1 ORDER BY created_at, id`,
        [collectionId],
      ),
    ]);
    if (exists.rows.length === 0) throw new NotFoundException(`collection ${collectionId} not found`);
    return { items: rows.rows.map(toView) };
  }

  async add(collectionId: string, meta: WatermarkMeta, png: Buffer): Promise<WatermarkView> {
    if (this.env.STORAGE_BACKEND !== 's3') {
      throw new ServiceUnavailableException('Ảnh watermark cần STORAGE_BACKEND=s3.');
    }
    const size = pngSize(png);
    if (size === null) throw new BadRequestException(['body: must be a PNG image']);
    if (size.width !== meta.w || size.height !== meta.h) {
      throw new BadRequestException([`body: the PNG is ${size.width}×${size.height}, the region ${meta.w}×${meta.h}`]);
    }

    const id = randomUUID();
    const key = relativeKey(collectionId, id);
    const sha256 = createHash('sha256').update(png).digest('hex');
    const client = await this.pool.connect();
    let stored = false;
    let broken: Error | undefined;
    try {
      await client.query('BEGIN');
      try {
        // Locked so two uploads at once cannot both pass the count.
        const locked = await client.query(`SELECT 1 FROM collections WHERE id = $1 FOR UPDATE`, [collectionId]);
        if (locked.rows.length === 0) throw new NotFoundException(`collection ${collectionId} not found`);
        const { rows } = await client.query<{ count: number; video: boolean }>(
          `SELECT (SELECT count(*)::int FROM collection_watermarks WHERE collection_id = $1) AS count,
                  EXISTS (SELECT 1 FROM videos WHERE aweme_id = $2 AND collection_id = $1) AS video`,
          [collectionId, meta.aweme_id],
        );
        if (!rows[0]!.video) {
          throw new BadRequestException([`aweme_id: video ${meta.aweme_id} is not in this collection`]);
        }
        if (rows[0]!.count >= MAX_WATERMARKS_PER_COLLECTION) {
          throw new ConflictException(`Collection đã có ${MAX_WATERMARKS_PER_COLLECTION} ảnh watermark, xoá bớt rồi thêm.`);
        }
        const put = await this.storage.put(key, Readable.from([png]), png.length);
        stored = true;
        const objectKey = this.objectKey(put.path);
        const inserted = await client.query(
          `INSERT INTO collection_watermarks
             (id, collection_id, image_key, image_sha256, frame_width, frame_height, aweme_id, time_ms, x, y, w, h)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
           RETURNING ${COLUMNS}`,
          [
            id,
            collectionId,
            objectKey,
            sha256,
            meta.frame_width,
            meta.frame_height,
            meta.aweme_id,
            meta.time_ms,
            meta.x,
            meta.y,
            meta.w,
            meta.h,
          ],
        );
        await client.query('COMMIT');
        return toView(inserted.rows[0]);
      } catch (err) {
        await client.query('ROLLBACK');
        throw err;
      }
    } catch (err) {
      if (stored) await this.discard([key]);
      if (!(err instanceof NotFoundException || err instanceof BadRequestException || err instanceof ConflictException)) {
        broken = err instanceof Error ? err : new Error('transaction failed');
      }
      throw err;
    } finally {
      client.release(broken);
    }
  }

  /** The PNG of one image of the collection. */
  async image(collectionId: string, watermarkId: string): Promise<MediaObject> {
    const { rows } = await this.pool.query<{ image_key: string }>(
      `SELECT image_key FROM collection_watermarks WHERE id = $1 AND collection_id = $2`,
      [watermarkId, collectionId],
    );
    const key = rows[0]?.image_key;
    if (key === undefined) throw new NotFoundException('Không có ảnh watermark này.');
    if (this.reader === null) throw new ServiceUnavailableException('Ảnh watermark cần STORAGE_BACKEND=s3.');
    let object: MediaObject | null;
    try {
      object = await this.reader.open(key);
    } catch {
      throw new BadGatewayException('Không đọc được kho media.');
    }
    if (!object) throw new NotFoundException('Không tìm thấy tệp ảnh watermark.');
    return object;
  }

  async remove(collectionId: string, watermarkId: string): Promise<void> {
    const removed = await this.pool.query(
      `DELETE FROM collection_watermarks WHERE id = $1 AND collection_id = $2`,
      [watermarkId, collectionId],
    );
    if (removed.rowCount === 0) throw new NotFoundException('Không có ảnh watermark này.');
    await this.discard([relativeKey(collectionId, watermarkId)]);
  }

  /** Keys of a collection's images, for removing them once the collection itself is gone. */
  static keysOf(collectionId: string, watermarkIds: readonly string[]): string[] {
    return watermarkIds.map((id) => relativeKey(collectionId, id));
  }

  /**
   * Removes objects whose rows are gone or never committed. A failure only leaves a
   * small orphan behind, so it is logged, not raised.
   */
  async discard(keys: readonly string[]): Promise<void> {
    for (const key of keys) {
      try {
        await this.storage.remove(key);
      } catch (err) {
        log.warn(`could not remove watermark image ${key}: ${(err as Error).message}`);
      }
    }
  }

  /** `s3://<bucket>/<key>` of a put in the configured bucket → `<key>`. */
  private objectKey(path: string): string {
    const match = /^s3:\/\/([^/]+)\/(.+)$/.exec(path);
    if (!match || match[1] !== this.env.S3_BUCKET) throw new Error(`unexpected storage path for a watermark image`);
    return match[2]!;
  }
}
