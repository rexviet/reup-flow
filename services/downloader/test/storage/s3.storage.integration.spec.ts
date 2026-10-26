/**
 * S3Storage against a real S3 API (MinIO). Every assertion reads the STORE --
 * the object, the list of unfinished uploads -- never a spy: a spy proving
 * `AbortMultipartUpload` was sent says nothing about whether the upload is gone.
 *
 * Needs S3_ENDPOINT / S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY (the downloader's
 * own MinIO user). Each run writes under a prefix of its own and wipes it.
 */
import {
  CreateMultipartUploadCommand,
  type S3Client,
} from '@aws-sdk/client-s3';
import { Readable } from 'node:stream';
import { S3Storage } from '../../src/storage/s3.storage';
import { videoKey } from '../../src/storage/storage.interface';
import { deferred, failingStream, gatedStream } from '../support/streams';
import {
  getObjectBytes,
  listObjectKeys,
  listUploads,
  makeS3Client,
  readS3TestEnv,
  uniqueTestPrefix,
  wipePrefix,
} from '../support/s3-test-env';

const MIB = 1024 * 1024;
const PART = 5 * MIB;
const DAY_MS = 24 * 60 * 60 * 1000;
const LEASE_MS = 5 * 60 * 1000;

// Multi-part bodies cross a network hop; the 5s default is for in-memory tests.
jest.setTimeout(60_000);

const env = readS3TestEnv();
const KEY = videoKey('MS4wLjABAAAA_s3_it', '7372484719365098001');

describe('S3Storage (MinIO)', () => {
  let client: S3Client;
  let prefix: string;

  beforeAll(() => {
    client = makeS3Client(env);
  });

  afterAll(() => {
    // Open keep-alive sockets would stop jest from exiting.
    client.destroy();
  });

  beforeEach(() => {
    // A prefix per test, so one test's orphan can never be another's evidence.
    prefix = uniqueTestPrefix('storage');
  });

  afterEach(async () => {
    await wipePrefix(client, env.bucket, prefix);
  });

  function storageWith(opts: { now?: () => number; staleUploadMs?: number } = {}): S3Storage {
    return new S3Storage(client, {
      bucket: env.bucket,
      keyPrefix: prefix,
      staleUploadMs: opts.staleUploadMs ?? DAY_MS,
      partSizeBytes: PART,
      now: opts.now,
    });
  }

  /** The store's whole state under this test's prefix. */
  async function snapshot(): Promise<{ objects: string[]; uploads: string[] }> {
    return {
      objects: await listObjectKeys(client, env.bucket, prefix),
      uploads: (await listUploads(client, env.bucket, prefix)).map((u) => u.uploadId),
    };
  }

  /**
   * The stored object is exactly `expected`. `Buffer.equals`, not `toEqual`:
   * jest's deep equality walks a 10 MB buffer element by element and takes
   * minutes.
   */
  async function expectStored(expected: Buffer): Promise<void> {
    const stored = await getObjectBytes(client, env.bucket, `${prefix}${KEY}`);
    expect({ length: stored.length, same: stored.equals(expected) }).toEqual({
      length: expected.length,
      same: true,
    });
  }

  function payload(bytes: number): Buffer {
    const buf = Buffer.alloc(bytes);
    for (let i = 0; i < bytes; i++) buf[i] = (i * 31 + 7) & 0xff;
    return buf;
  }

  describe('put', () => {
    it('commits a multi-part body byte for byte and leaves no upload behind', async () => {
      const body = payload(PART * 2 + 12345);
      const storage = storageWith();

      const result = await storage.put(KEY, Readable.from([body]), body.length);

      expect(result).toEqual({ path: `s3://${env.bucket}/${prefix}${KEY}`, bytes: body.length });
      await expectStored(body);
      expect(await storage.exists(KEY)).toBe(true);
      expect((await snapshot()).uploads).toEqual([]);
    });

    it('commits a body smaller than one part', async () => {
      const body = payload(4096);
      const storage = storageWith();

      const result = await storage.put(KEY, Readable.from([body]), body.length);

      expect(result.bytes).toBe(4096);
      await expectStored(body);
    });

    it('commits an empty body as an empty object', async () => {
      const storage = storageWith();

      const result = await storage.put(KEY, Readable.from([]), 0);

      expect(result.bytes).toBe(0);
      expect((await getObjectBytes(client, env.bucket, `${prefix}${KEY}`)).length).toBe(0);
      expect((await snapshot()).uploads).toEqual([]);
    });

    it('commits a body delivered in many small chunks', async () => {
      const body = payload(PART + 777);
      const chunks: Buffer[] = [];
      for (let off = 0; off < body.length; off += 64 * 1024) chunks.push(body.subarray(off, off + 64 * 1024));

      await storageWith().put(KEY, Readable.from(chunks), body.length);

      await expectStored(body);
    });

    it('leaves no object and no unfinished upload when the stream is cut after a part was sent', async () => {
      const head = payload(PART + 100);

      await expect(
        storageWith().put(KEY, failingStream(head, new Error('connection reset')), head.length * 2),
      ).rejects.toThrow('connection reset');

      expect(await snapshot()).toEqual({ objects: [], uploads: [] });
    });

    it('leaves nothing behind when the stream is cut before any part was sent', async () => {
      await expect(
        storageWith().put(KEY, failingStream(payload(100), new Error('reset early'))),
      ).rejects.toThrow('reset early');

      expect(await snapshot()).toEqual({ objects: [], uploads: [] });
    });

    it('rejects a body shorter than advertised and commits nothing', async () => {
      const body = payload(PART + 10);

      await expect(storageWith().put(KEY, Readable.from([body]), body.length + 1)).rejects.toThrow(
        /advertised/,
      );

      expect(await snapshot()).toEqual({ objects: [], uploads: [] });
    });

    it('rejects a body longer than advertised and commits nothing', async () => {
      const body = payload(1000);

      await expect(storageWith().put(KEY, Readable.from([body]), 999)).rejects.toThrow(/advertised/);

      expect(await snapshot()).toEqual({ objects: [], uploads: [] });
    });

    it.each(['', '/abs.mp4', '../escape.mp4', 'a/../../escape.mp4', 'a/./b.mp4'])(
      'refuses the key %j before touching the store',
      async (key) => {
        await expect(storageWith().put(key, Readable.from([payload(10)]))).rejects.toThrow(/storage:/);

        expect(await snapshot()).toEqual({ objects: [], uploads: [] });
      },
    );
  });

  describe('put with beforeCommit', () => {
    it('commits when the hook approves', async () => {
      const body = payload(PART + 1);
      const storage = storageWith();

      const result = await storage.put(KEY, Readable.from([body]), body.length, async () => true);

      expect(result?.bytes).toBe(body.length);
      await expectStored(body);
    });

    it('answers null on decline and leaves no object and no unfinished upload', async () => {
      const body = payload(PART * 2);
      const storage = storageWith();

      const result = await storage.put(KEY, Readable.from([body]), body.length, async () => false);

      expect(result).toBeNull();
      expect(await storage.exists(KEY)).toBe(false);
      expect(await snapshot()).toEqual({ objects: [], uploads: [] });
    });

    it('does not overwrite an existing committed object when it declines', async () => {
      const storage = storageWith();
      const original = payload(2048);
      await storage.put(KEY, Readable.from([original]), original.length);

      const declined = await storage.put(KEY, Readable.from([payload(PART + 5)]), undefined, async () => false);

      expect(declined).toBeNull();
      await expectStored(original);
    });

    it('rethrows when the hook throws and leaves nothing behind', async () => {
      const storage = storageWith();

      await expect(
        storage.put(KEY, Readable.from([payload(PART + 5)]), undefined, async () => {
          throw new Error('db went away');
        }),
      ).rejects.toThrow('db went away');

      expect(await storage.exists(KEY)).toBe(false);
      expect(await snapshot()).toEqual({ objects: [], uploads: [] });
    });

    it('asks the hook only after the whole body has been received', async () => {
      const body = payload(PART + 321);
      let uploadsWhenAsked = -1;
      let objectsWhenAsked = -1;

      await storageWith().put(KEY, Readable.from([body]), body.length, async () => {
        const state = await snapshot();
        uploadsWhenAsked = state.uploads.length;
        objectsWhenAsked = state.objects.length;
        return true;
      });

      // Mid-commit the upload is still open and the final key does not exist yet.
      expect(uploadsWhenAsked).toBe(1);
      expect(objectsWhenAsked).toBe(0);
    });
  });

  describe('exists, remove, freeBytes', () => {
    it('reports a missing key as absent rather than failing', async () => {
      expect(await storageWith().exists('douyin/nobody/none.mp4')).toBe(false);
    });

    it('removes a committed object, and removing a missing one is not an error', async () => {
      const storage = storageWith();
      await storage.put(KEY, Readable.from([payload(100)]), 100);

      await storage.remove(KEY);
      await expect(storage.remove(KEY)).resolves.toBeUndefined();

      expect(await storage.exists(KEY)).toBe(false);
      expect((await snapshot()).objects).toEqual([]);
    });

    it('has no disk to fill', async () => {
      expect(await storageWith().freeBytes()).toBe(Infinity);
    });
  });

  describe('removeStaleTemp', () => {
    /** An orphaned upload, as a killed worker leaves behind. */
    async function orphan(under: string, key = 'douyin/x/orphan.mp4'): Promise<string> {
      const res = await client.send(
        new CreateMultipartUploadCommand({ Bucket: env.bucket, Key: `${under}${key}` }),
      );
      return res.UploadId as string;
    }

    it('does not touch an upload this process is still writing, however old the clock says it is', async () => {
      // Clock two days ahead: by age alone, an upload started a moment ago is stale.
      const storage = storageWith({ now: () => Date.now() + 2 * DAY_MS });
      const head = payload(PART + 1);
      const tail = payload(2000);
      const gate = deferred();

      const putting = storage.put(KEY, gatedStream([head, tail], gate.promise), head.length + tail.length);
      await waitFor(async () => (await listUploads(client, env.bucket, prefix)).length === 1);

      const removed = await storage.removeStaleTemp(0);
      expect(removed).toEqual([]);
      expect((await listUploads(client, env.bucket, prefix)).length).toBe(1);

      gate.resolve();
      await putting;
      await expectStored(Buffer.concat([head, tail]));
    });

    it('aborts an upload older than the stale threshold that nothing here is writing', async () => {
      const uploadId = await orphan(prefix);
      const storage = storageWith({ now: () => Date.now() + DAY_MS + 60_000 });

      const removed = await storage.removeStaleTemp(LEASE_MS);

      expect(removed).toEqual([`s3://${env.bucket}/${prefix}douyin/x/orphan.mp4?uploadId=${uploadId}`]);
      expect((await snapshot()).uploads).toEqual([]);
    });

    it('keeps an upload that is older than the lease but younger than the stale threshold', async () => {
      const uploadId = await orphan(prefix);
      // Ten minutes on: past a 5 minute lease, nowhere near a day.
      const storage = storageWith({ now: () => Date.now() + 10 * 60_000 });

      const removed = await storage.removeStaleTemp(LEASE_MS);

      expect(removed).toEqual([]);
      expect((await snapshot()).uploads).toEqual([uploadId]);
    });

    it('lets a caller ask for a longer cutoff than the stale threshold', async () => {
      const uploadId = await orphan(prefix);
      const storage = storageWith({ now: () => Date.now() + 2 * DAY_MS, staleUploadMs: DAY_MS });

      const removed = await storage.removeStaleTemp(3 * DAY_MS);

      expect(removed).toEqual([]);
      expect((await snapshot()).uploads).toEqual([uploadId]);
    });

    it('never touches uploads outside its own prefix', async () => {
      const otherPrefix = uniqueTestPrefix('neighbour');
      const neighbour = await orphan(otherPrefix);
      try {
        await orphan(prefix);
        const storage = storageWith({ now: () => Date.now() + 2 * DAY_MS });

        const removed = await storage.removeStaleTemp(0);

        expect(removed).toHaveLength(1);
        expect((await listUploads(client, env.bucket, otherPrefix)).map((u) => u.uploadId)).toEqual([neighbour]);
      } finally {
        await wipePrefix(client, env.bucket, otherPrefix);
      }
    });

    it('sweeps a mix: aborts every stale orphan and leaves committed objects alone', async () => {
      const live = storageWith();
      await live.put(KEY, Readable.from([payload(500)]), 500);
      await orphan(prefix, 'douyin/x/a.mp4');
      await orphan(prefix, 'douyin/x/b.mp4');

      const removed = await storageWith({ now: () => Date.now() + 2 * DAY_MS }).removeStaleTemp(0);

      expect(removed).toHaveLength(2);
      const state = await snapshot();
      expect(state.uploads).toEqual([]);
      expect(state.objects).toEqual([`${prefix}${KEY}`]);
    });
  });

  it('refuses an empty key prefix', () => {
    expect(
      () => new S3Storage(client, { bucket: env.bucket, keyPrefix: '', staleUploadMs: DAY_MS }),
    ).toThrow(/prefix/);
  });
});

async function waitFor(condition: () => Promise<boolean>, timeoutMs = 10_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await condition()) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error('waitFor: condition not met in time');
}
