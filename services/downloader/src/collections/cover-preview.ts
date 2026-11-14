import { createHash } from 'node:crypto';
import type { Readable } from 'node:stream';
import {
  BadGatewayException,
  Inject,
  Injectable,
  NotFoundException,
  ServiceUnavailableException,
} from '@nestjs/common';
import type { Pool } from 'pg';
import { z } from 'zod';
import { DashboardMediaService, MEDIA_READER, type MediaReader } from '../dashboard/dashboard.media';
import { PG_POOL } from '../db/db.module';

/** What the collection dialog needs to draw the cover box over a real frame. */
export interface CoverPreview {
  aweme_id: string;
  generation: number;
  width: number;
  height: number;
  /** The source subtitle band the review project was drawn from, in pixels; null when none was found. */
  source_region: { x: number; y: number; w: number; h: number } | null;
  /** A moment that shows source subtitles: the middle of the first source cue, else a third of the way in. */
  sample_ms: number;
  /** Short-lived signed link to the source video; null when the source cannot be served. */
  source_url: string | null;
}

/** HEAD and manifest are small JSON documents; anything larger is not one of ours. */
const MAX_JSON_BYTES = 16 * 1024 * 1024;
const AWAITING = ['ready_for_review', 'needs_human'];
const REVISION_ID = /^[0-9a-f]{32}$/;

const headSchema = z.object({
  aweme_id: z.string(),
  generation: z.number().int().nonnegative(),
  revision_id: z.string().regex(REVISION_ID),
  manifest_key: z.string(),
  manifest_sha256: z.string().regex(/^[0-9a-f]{64}$/),
});

const nonNegInt = z.number().int().nonnegative();
const manifestSchema = z.object({
  aweme_id: z.string(),
  generation: nonNegInt,
  revision_id: z.string(),
  duration_ms: z.number().int().positive(),
  width: z.number().int().positive(),
  height: z.number().int().positive(),
  source_region: z
    .object({ x: nonNegInt, y: nonNegInt, w: z.number().int().positive(), h: z.number().int().positive() })
    .nullable(),
  source_cues: z.array(z.tuple([nonNegInt, nonNegInt])),
});

async function readCapped(body: Readable, limit: number): Promise<Buffer> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of body) {
    const buf = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk as string);
    size += buf.length;
    if (size > limit) {
      body.destroy();
      throw new BadGatewayException('Project review quá lớn.');
    }
    chunks.push(buf);
  }
  return Buffer.concat(chunks);
}

/**
 * The cover-box preview of a collection: the most recent of its videos that has a
 * review project (one still awaiting review first), read from that project's HEAD
 * manifest the way the localization `Repository.current()` resolves it, plus a
 * signed link to its source video.
 */
@Injectable()
export class CoverPreviewService {
  constructor(
    @Inject(PG_POOL) private readonly pool: Pool,
    @Inject(MEDIA_READER) private readonly reader: MediaReader | null,
    private readonly media: DashboardMediaService,
  ) {}

  async preview(collectionId: string): Promise<CoverPreview> {
    const [exists, picked] = await Promise.all([
      this.pool.query(`SELECT 1 FROM collections WHERE id = $1`, [collectionId]),
      this.pool.query<{ aweme_id: string; generation: number }>(
        `SELECT r.aweme_id, r.generation
           FROM videos v
           JOIN LATERAL (
             SELECT r.aweme_id, r.generation, r.status
               FROM localization.runs r
              WHERE r.aweme_id = v.aweme_id
                AND r.project_key = 'review/' || r.aweme_id || '/g' || r.generation || '/head.json'
              ORDER BY r.generation DESC
              LIMIT 1
           ) r ON true
          WHERE v.collection_id = $1
          ORDER BY (r.status = ANY($2::text[])) DESC, v.create_time DESC, v.aweme_id DESC
          LIMIT 1`,
        [collectionId, AWAITING],
      ),
    ]);
    if (exists.rows.length === 0) throw new NotFoundException(`collection ${collectionId} not found`);
    const row = picked.rows[0];
    if (!row) throw new NotFoundException('Bộ sưu tập chưa có video nào có project review để xem trước.');
    if (this.reader === null) throw new ServiceUnavailableException('Xem trước cần STORAGE_BACKEND=s3.');

    const { aweme_id: awemeId, generation } = row;
    const dir = `review/${awemeId}/g${generation}/`;
    const headRaw = await this.read(`${dir}head.json`);
    const head = this.parse(headSchema, headRaw);
    if (
      head.aweme_id !== awemeId ||
      head.generation !== generation ||
      head.manifest_key !== `${dir}r${head.revision_id}/manifest.json`
    ) {
      throw new BadGatewayException('HEAD của project review không khớp video.');
    }
    const manifestRaw = await this.read(head.manifest_key);
    if (createHash('sha256').update(manifestRaw).digest('hex') !== head.manifest_sha256) {
      throw new BadGatewayException('Manifest của project review sai checksum.');
    }
    const manifest = this.parse(manifestSchema, manifestRaw);
    if (manifest.aweme_id !== awemeId || manifest.generation !== generation || manifest.revision_id !== head.revision_id) {
      throw new BadGatewayException('HEAD và manifest của project review không khớp.');
    }

    const first = manifest.source_cues[0];
    const sampleMs = first ? Math.floor((first[0] + first[1]) / 2) : Math.floor(manifest.duration_ms / 3);
    const { links } = await this.media.links(awemeId, generation, ['source']);
    const source = links[0];

    return {
      aweme_id: awemeId,
      generation,
      width: manifest.width,
      height: manifest.height,
      source_region: manifest.source_region,
      sample_ms: sampleMs,
      source_url: source?.available && source.url ? source.url : null,
    };
  }

  private async read(key: string): Promise<Buffer> {
    let object;
    try {
      object = await (this.reader as MediaReader).open(key);
    } catch {
      throw new BadGatewayException('Không đọc được kho media.');
    }
    if (!object) throw new NotFoundException(`Không tìm thấy project review (${key}).`);
    return readCapped(object.body, MAX_JSON_BYTES);
  }

  private parse<T>(schema: z.ZodType<T>, raw: Buffer): T {
    let json: unknown;
    try {
      json = JSON.parse(raw.toString('utf8'));
    } catch {
      throw new BadGatewayException('Project review không phải JSON.');
    }
    const parsed = schema.safeParse(json);
    if (!parsed.success) throw new BadGatewayException('Project review sai định dạng.');
    return parsed.data;
  }
}
