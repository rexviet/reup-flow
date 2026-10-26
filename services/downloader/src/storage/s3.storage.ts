import {
  AbortMultipartUploadCommand,
  CompleteMultipartUploadCommand,
  CreateMultipartUploadCommand,
  DeleteObjectCommand,
  HeadObjectCommand,
  ListMultipartUploadsCommand,
  S3Client,
  UploadPartCommand,
  type CompletedPart,
} from '@aws-sdk/client-s3';
import type { Readable } from 'node:stream';
import type { BeforeCommit, PutResult, Storage } from './storage.interface';

export interface S3StorageOptions {
  bucket: string;
  /** Prepended to every key. Non-empty, so a sweep can never see another tenant's uploads. */
  keyPrefix: string;
  /** `S3_STALE_UPLOAD_MS`: the floor under any `removeStaleTemp` cutoff. */
  staleUploadMs: number;
  /** Multipart chunk size. S3 rejects non-final parts under 5 MiB. */
  partSizeBytes?: number;
  /** Injected so a test can age an upload without waiting a day. */
  now?: () => number;
  /** `MaxUploads` per listing request; injectable so a test can force pagination. */
  listPageSize?: number;
}

export const DEFAULT_PART_SIZE_BYTES = 8 * 1024 * 1024;
const MIN_PART_SIZE_BYTES = 5 * 1024 * 1024;

/**
 * `Storage` over the S3 API (MinIO locally; S3 or R2 in production).
 *
 * The commit point is `CompleteMultipartUpload`: until it succeeds nothing is
 * readable under the final key, which is the same "absent or complete" property
 * `LocalDiskStorage` gets from `rename`. Every path out of `put` that does not
 * complete the upload aborts it, because an unaborted multipart upload is
 * invisible to `exists()` yet is billed storage nobody will ever read.
 */
export class S3Storage implements Storage {
  /**
   * Upload ids this instance is writing RIGHT NOW. `removeStaleTemp` never
   * aborts one of them, whatever its age says.
   *
   * This only protects uploads of THIS process. A migration script or the old
   * pod of a rolling deploy is invisible here, which is why the age threshold
   * in `removeStaleTemp` is a real guard and not a formality.
   */
  private readonly inFlight = new Set<string>();
  private readonly partSize: number;
  private readonly now: () => number;

  constructor(
    private readonly client: S3Client,
    private readonly opts: S3StorageOptions,
  ) {
    // The trailing slash makes the prefix a directory boundary: without it
    // `source/a` would also claim `source/ab/...` in the sweep.
    if (opts.keyPrefix.length === 0 || !opts.keyPrefix.endsWith('/')) {
      throw new Error('storage: S3 key prefix must be non-empty and end with "/"');
    }
    // NaN or a non-positive floor would make every upload look stale.
    if (!Number.isFinite(opts.staleUploadMs) || opts.staleUploadMs <= 0) {
      throw new Error('storage: staleUploadMs must be a positive finite number');
    }
    this.partSize = opts.partSizeBytes ?? DEFAULT_PART_SIZE_BYTES;
    if (this.partSize < MIN_PART_SIZE_BYTES) {
      throw new Error(`storage: S3 part size must be at least ${MIN_PART_SIZE_BYTES} bytes`);
    }
    this.now = opts.now ?? Date.now;
  }

  async put(key: string, stream: Readable, expectedBytes?: number): Promise<PutResult>;
  async put(
    key: string,
    stream: Readable,
    expectedBytes: number | undefined,
    beforeCommit: BeforeCommit,
  ): Promise<PutResult | null>;
  async put(
    key: string,
    stream: Readable,
    expectedBytes?: number,
    beforeCommit?: BeforeCommit,
  ): Promise<PutResult | null> {
    const { bucket } = this.opts;

    let objectKey: string;
    let uploadId: string;
    try {
      objectKey = this.objectKey(key);
      const created = await this.client.send(
        new CreateMultipartUploadCommand({ Bucket: bucket, Key: objectKey }),
      );
      if (!created.UploadId) throw new Error(`storage: S3 returned no upload id for ${objectKey}`);
      uploadId = created.UploadId;
    } catch (err) {
      // Nothing will ever read this stream now. A caller that opened a file
      // for it (the migration script opens one per video) would otherwise
      // keep the descriptor until garbage collection, and a bulk run against
      // a store that refuses every put runs out of descriptors first.
      stream.destroy();
      throw err;
    }

    // Registered before the first byte is sent, so a sweep cannot list the
    // upload without also seeing the claim on it.
    this.inFlight.add(uploadId);

    try {
      const parts: CompletedPart[] = [];
      let bytes = 0;
      let pending: Buffer[] = [];
      let pendingBytes = 0;

      const flush = async (): Promise<void> => {
        const body = Buffer.concat(pending, pendingBytes);
        pending = [];
        pendingBytes = 0;
        const partNumber = parts.length + 1;
        const res = await this.client.send(
          new UploadPartCommand({
            Bucket: bucket,
            Key: objectKey,
            UploadId: uploadId,
            PartNumber: partNumber,
            Body: body,
          }),
        );
        parts.push({ ETag: res.ETag, PartNumber: partNumber });
      };

      // Counted as it is sent, not read back from the store afterwards: a
      // dropped connection yields a short body that is otherwise
      // indistinguishable from a complete one.
      for await (const chunk of stream) {
        const buf = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk as Uint8Array);
        bytes += buf.length;
        pending.push(buf);
        pendingBytes += buf.length;
        if (pendingBytes >= this.partSize) await flush();
      }
      // The final part may be short; an empty body still needs one part, since
      // a multipart upload cannot be completed with none.
      if (pendingBytes > 0 || parts.length === 0) await flush();

      if (expectedBytes !== undefined && bytes !== expectedBytes) {
        throw new Error(
          `storage: ${key} received ${bytes} bytes but ${expectedBytes} were advertised`,
        );
      }

      // The ownership re-check sits immediately before the commit, after the
      // slow part: an answer given earlier would be stale by the time it mattered.
      if (beforeCommit && !(await beforeCommit())) {
        await this.abort(objectKey, uploadId);
        return null;
      }

      // If the response to this call is lost after the server committed, the
      // object exists while this put reports failure (and then aborts an
      // upload that no longer exists, which is harmless). The outcome is an
      // orphan object at the final key, never a corrupt one: the caller does
      // not mark the video `done`, so the retry overwrites it with the same
      // bytes. The client's request timeout is what keeps a hung socket from
      // pinning this upload's in-flight entry forever.
      await this.client.send(
        new CompleteMultipartUploadCommand({
          Bucket: bucket,
          Key: objectKey,
          UploadId: uploadId,
          MultipartUpload: { Parts: parts },
        }),
      );
      return { path: `s3://${bucket}/${objectKey}`, bytes };
    } catch (err) {
      await this.abort(objectKey, uploadId);
      throw err;
    } finally {
      this.inFlight.delete(uploadId);
    }
  }

  async exists(key: string): Promise<boolean> {
    try {
      await this.client.send(
        new HeadObjectCommand({ Bucket: this.opts.bucket, Key: this.objectKey(key) }),
      );
      return true;
    } catch (err) {
      if (isNotFound(err)) return false;
      throw err;
    }
  }

  /** An object store has no disk to fill. */
  async freeBytes(): Promise<number> {
    return Infinity;
  }

  async remove(key: string): Promise<void> {
    try {
      await this.client.send(
        new DeleteObjectCommand({ Bucket: this.opts.bucket, Key: this.objectKey(key) }),
      );
    } catch (err) {
      if (!isNotFound(err)) throw err;
    }
  }

  /**
   * Aborts every unfinished multipart upload under this instance's prefix that
   * is older than `max(olderThanMs, S3_STALE_UPLOAD_MS)` and that this process
   * is not itself writing. Returns `s3://bucket/key?uploadId=...` per abort.
   *
   * The floor matters: `Initiated` never changes while an upload is running, so
   * a lease-sized cutoff (minutes) would abort a slow but healthy upload --
   * and the registry cannot veto an upload owned by another process.
   *
   * An upload that vanished between listing and abort (`NoSuchUpload`) counts
   * as handled. Any other abort error is collected and thrown once the sweep
   * has finished, and listing errors propagate at once: a sweep that cannot see
   * or cannot reclaim must not report success.
   */
  async removeStaleTemp(olderThanMs: number): Promise<string[]> {
    const threshold = Math.max(olderThanMs, this.opts.staleUploadMs);
    const cutoff = this.now() - threshold;
    const removed: string[] = [];
    const failures: string[] = [];

    let keyMarker: string | undefined;
    let uploadIdMarker: string | undefined;
    for (;;) {
      // No `Prefix` on the request, deliberately. MinIO answers a prefix that
      // is not a complete object key with an empty list (verified: a prefix of
      // `source/x/` finds nothing, the exact key finds the upload), so a
      // server-side prefix would make every sweep a silent no-op on MinIO. The
      // bucket's unfinished uploads are few, so the prefix is applied here,
      // which behaves the same on S3, R2 and MinIO.
      const page = await this.client.send(
        new ListMultipartUploadsCommand({
          Bucket: this.opts.bucket,
          MaxUploads: this.opts.listPageSize,
          KeyMarker: keyMarker,
          UploadIdMarker: uploadIdMarker,
        }),
      );
      for (const upload of page.Uploads ?? []) {
        const { Key: key, UploadId: uploadId, Initiated: initiated } = upload;
        if (!key || !uploadId || !initiated) continue;
        if (!key.startsWith(this.opts.keyPrefix)) continue;
        if (this.inFlight.has(uploadId)) continue;
        // Written so that an unparseable cutoff (NaN) skips the upload instead
        // of treating everything as stale.
        if (!(initiated.getTime() <= cutoff)) continue;
        try {
          await this.client.send(
            new AbortMultipartUploadCommand({
              Bucket: this.opts.bucket,
              Key: key,
              UploadId: uploadId,
            }),
          );
          removed.push(`s3://${this.opts.bucket}/${key}?uploadId=${uploadId}`);
        } catch (err) {
          // Finished or aborted by its owner since the listing: that is the
          // outcome the sweep wanted. Anything else (AccessDenied, a dead
          // network) means the garbage is still there, so it must not read as
          // a successful sweep.
          if (!isNoSuchUpload(err)) failures.push(`${key}: ${describeError(err)}`);
        }
      }
      if (!page.IsTruncated) break;
      keyMarker = page.NextKeyMarker;
      uploadIdMarker = page.NextUploadIdMarker;
    }
    // After the loop, so one refused abort does not stop the rest being reclaimed.
    if (failures.length > 0) {
      throw new Error(
        `storage: ${failures.length} stale upload(s) could not be aborted (${removed.length} were): ` +
          failures.slice(0, 3).join('; '),
      );
    }
    return removed;
  }

  /** Best effort: the original failure is what the caller needs to see. */
  private async abort(objectKey: string, uploadId: string): Promise<void> {
    await this.client
      .send(
        new AbortMultipartUploadCommand({
          Bucket: this.opts.bucket,
          Key: objectKey,
          UploadId: uploadId,
        }),
      )
      .catch(() => undefined);
  }

  /** Same containment rule as the local backend: a key cannot leave the prefix. */
  private objectKey(key: string): string {
    if (key.length === 0) throw new Error('storage: key must not be empty');
    if (key.startsWith('/')) throw new Error(`storage: key must be relative, got ${key}`);
    if (key.split('/').some((segment) => segment === '..' || segment === '.')) {
      throw new Error(`storage: key escapes the prefix, refusing: ${key}`);
    }
    return `${this.opts.keyPrefix}${key}`;
  }
}

/** The connection settings of the `S3_*` env group, so a script can build the same storage the service does. */
export interface S3Settings {
  endpoint: string;
  region: string;
  forcePathStyle: boolean;
  accessKeyId: string;
  secretAccessKey: string;
  bucket: string;
  keyPrefix: string;
  staleUploadMs: number;
}

export function createS3Storage(settings: S3Settings): S3Storage {
  const client = new S3Client({
    endpoint: settings.endpoint,
    region: settings.region,
    forcePathStyle: settings.forcePathStyle,
    credentials: {
      accessKeyId: settings.accessKeyId,
      secretAccessKey: settings.secretAccessKey,
    },
    // Without these a hung socket never errors, and `put` would sit on it
    // forever with its upload id in the in-flight registry, which the sweep
    // honours. `requestTimeout` is an idle-socket timeout, so a slow but
    // progressing 8 MiB part is not cut.
    requestHandler: { connectionTimeout: 10_000, requestTimeout: 120_000 },
  });
  return new S3Storage(client, {
    bucket: settings.bucket,
    keyPrefix: settings.keyPrefix,
    staleUploadMs: settings.staleUploadMs,
  });
}

function isNoSuchUpload(err: unknown): boolean {
  return typeof err === 'object' && err !== null && (err as { name?: string }).name === 'NoSuchUpload';
}

function describeError(err: unknown): string {
  if (err instanceof Error) return `${err.name}: ${err.message}`;
  return String(err);
}

function isNotFound(err: unknown): boolean {
  if (typeof err !== 'object' || err === null) return false;
  const e = err as { name?: string; $metadata?: { httpStatusCode?: number } };
  return e.name === 'NotFound' || e.name === 'NoSuchKey' || e.$metadata?.httpStatusCode === 404;
}
