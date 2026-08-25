import { execFileSync, spawn, type ChildProcess } from 'node:child_process';
import { mkdtemp, mkdir, readFile, readdir, rm, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Readable } from 'node:stream';
import { LocalDiskStorage } from '../../src/storage/local-disk.storage';
import { videoKey } from '../../src/storage/storage.interface';
import { deferred, failingStream, gatedStream, overlappingStreams, waitUntil } from '../support/streams';

const KEY = videoKey('MS4wLjABAAAA_abc', '7372484719365098803');

describe('LocalDiskStorage', () => {
  let root: string;
  let storage: LocalDiskStorage;

  beforeEach(async () => {
    root = await mkdtemp(join(tmpdir(), 'reup-storage-'));
    storage = new LocalDiskStorage(root);
  });

  afterEach(async () => {
    await rm(root, { recursive: true, force: true });
  });

  /** Every file under the key's directory, committed or not. */
  async function filesInKeyDir(): Promise<string[]> {
    try {
      return await readdir(join(root, 'douyin', 'MS4wLjABAAAA_abc'));
    } catch {
      return [];
    }
  }

  describe('put', () => {
    it('writes the stream under DATA_DIR/douyin/{sec_user_id}/{aweme_id}.mp4', async () => {
      const payload = Buffer.alloc(4096, 0x41);

      const result = await storage.put(KEY, Readable.from([payload]));

      expect(result.path).toBe(join(root, KEY));
      expect(result.bytes).toBe(payload.length);
      expect(await readFile(result.path)).toEqual(payload);
    });

    it('creates the profile directory when it does not exist yet', async () => {
      await expect(
        storage.put(videoKey('MS4w_brand_new', '999'), Readable.from([Buffer.from('x')])),
      ).resolves.toBeDefined();
    });
  });

  describe('atomic commit', () => {
    it('does not expose the destination file while the write is still running', async () => {
      // A crash at this instant must leave nothing behind at the final name.
      // If a half-written file were visible here, `exists()` would report a
      // truncated download as present and phase 6 would skip re-fetching it.
      const gate = deferred();
      const writing = storage.put(KEY, gatedStream([Buffer.alloc(1024, 1), Buffer.alloc(1024, 2)], gate.promise));

      await waitUntil(async () => (await filesInKeyDir()).length > 0, 'the write to start');

      expect(await storage.exists(KEY)).toBe(false);
      const inFlight = await filesInKeyDir();
      expect(inFlight).toHaveLength(1);
      expect(inFlight[0]).toMatch(/\.tmp$/);

      gate.resolve();
      await writing;

      expect(await storage.exists(KEY)).toBe(true);
      expect(await filesInKeyDir()).toEqual(['7372484719365098803.mp4']);
    });

    it('leaves no committed file when the source stream dies mid-body', async () => {
      const dropped = failingStream(Buffer.alloc(2048, 7), new Error('socket hang up'));

      await expect(storage.put(KEY, dropped)).rejects.toThrow('socket hang up');

      expect(await storage.exists(KEY)).toBe(false);
      expect(await filesInKeyDir()).toEqual([]);
    });

    it('survives a real SIGKILL mid-write without committing anything', async () => {
      // The mid-flight test above proves the invariant at one instant; this
      // proves it against an actual process death, where no cleanup handler
      // of ours gets to run at all.
      const child = spawnCrashWriter(root, KEY);
      try {
        await waitUntil(async () => (await filesInKeyDir()).length > 0, 'the child to start writing');
        child.kill('SIGKILL');
        await waitUntil(() => child.exitCode !== null || child.signalCode !== null, 'the child to die');

        expect(await storage.exists(KEY)).toBe(false);
        // `.every` on an empty array is true, so the length assertion is what
        // makes this line mean anything: the killed writer really did leave a
        // temp file, and it is a temp file rather than the committed name.
        const leftovers = await filesInKeyDir();
        expect(leftovers).not.toHaveLength(0);
        expect(leftovers.every((name) => name.endsWith('.tmp'))).toBe(true);
      } finally {
        child.kill('SIGKILL');
      }
    }, 30_000);
  });

  describe('temp file naming', () => {
    it('gives every put its own temp file, so two writers cannot corrupt one key', async () => {
      // The reaper (F3) can hand a video to a new worker while the old one is
      // still streaming. Both then write the SAME key. With one shared temp
      // name they interleave into a single file and the committed result is
      // neither download -- a corrupt mp4 recorded as `done`.
      //
      // The two payloads deliberately differ in LENGTH as well as content.
      // An earlier version of this test gave both writers the same size and
      // only asked whether the committed file was homogeneous; two writers
      // sharing a file write the same bytes at the same offsets, so whichever
      // lands last can leave the file looking perfectly uniform. The test
      // passed for an incidental reason (the loser's rename hit ENOENT), and
      // went flaky-green the moment that rename was made forgiving -- which
      // is exactly the shape phase 6's lease check will introduce. Different
      // lengths mean a shared file can equal neither payload.
      const chunk = 8192;
      const { streams, payloads } = overlappingStreams(
        [
          { fill: 0x41, chunks: 8 },
          { fill: 0x42, chunks: 20 },
        ],
        chunk,
      );

      const results = await Promise.all([
        storage.put(KEY, streams[0]),
        storage.put(KEY, streams[1]),
      ]);

      expect(results[0].bytes).toBe(payloads[0].length);
      expect(results[1].bytes).toBe(payloads[1].length);

      // Exactly one COMPLETE download must be committed -- not a blend, and
      // not a truncation of the longer one.
      const committed = await readFile(join(root, KEY));
      const matches = payloads.some((payload) => committed.equals(payload));
      expect({
        matchesOnePayloadExactly: matches,
        committedBytes: committed.length,
        expectedOneOf: payloads.map((p) => p.length),
      }).toEqual({
        matchesOnePayloadExactly: true,
        committedBytes: committed.length,
        expectedOneOf: payloads.map((p) => p.length),
      });
    });

    it('never derives the temp name from the key alone', async () => {
      const gate = deferred();
      const first = storage.put(KEY, gatedStream([Buffer.alloc(64, 1), Buffer.alloc(64, 1)], gate.promise));
      await waitUntil(async () => (await filesInKeyDir()).length > 0, 'the first write to start');
      const second = storage.put(KEY, gatedStream([Buffer.alloc(64, 2), Buffer.alloc(64, 2)], gate.promise));
      await waitUntil(async () => (await filesInKeyDir()).length > 1, 'the second write to start');

      expect(new Set(await filesInKeyDir()).size).toBe(2);

      gate.resolve();
      await Promise.all([first, second]);
    });
  });

  describe('expectedBytes', () => {
    it('rejects a short body and commits nothing', async () => {
      const truncated = Buffer.alloc(1000, 9);

      await expect(storage.put(KEY, Readable.from([truncated]), 4096)).rejects.toThrow(/1000.*4096|4096.*1000/);

      expect(await storage.exists(KEY)).toBe(false);
      expect(await filesInKeyDir()).toEqual([]);
    });

    it('rejects a body that is longer than advertised too', async () => {
      await expect(storage.put(KEY, Readable.from([Buffer.alloc(5000, 9)]), 4096)).rejects.toThrow();
      expect(await storage.exists(KEY)).toBe(false);
    });

    it('accepts a body of exactly the advertised size', async () => {
      const body = Buffer.alloc(4096, 9);

      await expect(storage.put(KEY, Readable.from([body]), 4096)).resolves.toMatchObject({ bytes: 4096 });
    });
  });

  describe('exists / remove', () => {
    it('reports a committed file and forgets it after remove', async () => {
      await storage.put(KEY, Readable.from([Buffer.from('x')]));
      expect(await storage.exists(KEY)).toBe(true);

      await storage.remove(KEY);

      expect(await storage.exists(KEY)).toBe(false);
    });

    it('treats removing a file that is not there as a no-op', async () => {
      await expect(storage.remove(videoKey('MS4w_nobody', '1'))).resolves.toBeUndefined();
    });
  });

  describe('key safety', () => {
    // Keys are built from `sec_user_id` and `aweme_id`, both of which arrive
    // from Douyin. A key that climbs out of DATA_DIR turns a download into an
    // arbitrary file write.
    it('refuses a key whose directory is a symlink pointing out of DATA_DIR', async () => {
      // `resolve()` is purely lexical -- it never touches the filesystem. A
      // key with no `..` in it at all still escapes if an INTERMEDIATE
      // directory is a symlink, which is reachable on a shared or mounted
      // DATA_DIR. The comment on the guard claims it prevents "an arbitrary
      // file write anywhere the process can reach"; without this, that claim
      // is bigger than the guard.
      const outside = await mkdtemp(join(tmpdir(), 'reup-outside-'));
      await mkdir(join(root, 'douyin'), { recursive: true });
      await symlink(outside, join(root, 'douyin', 'MS4w_evil'));
      const escaping = videoKey('MS4w_evil', '1');

      await expect(storage.put(escaping, Readable.from([Buffer.from('PWNED')]))).rejects.toThrow(
        /key/i,
      );
      await expect(readdir(outside)).resolves.toEqual([]);

      await rm(outside, { recursive: true, force: true });
    });

    it('refuses to remove through a symlinked directory too', async () => {
      const outside = await mkdtemp(join(tmpdir(), 'reup-outside-'));
      await mkdir(join(root, 'douyin'), { recursive: true });
      await symlink(outside, join(root, 'douyin', 'MS4w_evil'));

      await expect(storage.remove(videoKey('MS4w_evil', '1'))).rejects.toThrow(/key/i);

      await rm(outside, { recursive: true, force: true });
    });

    it.each([
      ['a parent traversal', '../escape.mp4'],
      ['a traversal in the middle', 'douyin/MS4w/../../../escape.mp4'],
      ['an absolute path', '/etc/cron.d/escape'],
      ['an empty key', ''],
    ])('refuses %s', async (_label, key) => {
      await expect(storage.put(key, Readable.from([Buffer.from('x')]))).rejects.toThrow(/key/i);
      await expect(storage.exists(key)).rejects.toThrow(/key/i);
      await expect(storage.remove(key)).rejects.toThrow(/key/i);
    });
  });

  describe('freeBytes', () => {
    it('counts blocks available to an unprivileged writer, not the root reserve', async () => {
      // This is the claim `df` cannot check. Most filesystems reserve a slice
      // of space only root may use; `bfree` counts it, `bavail` does not. A
      // preflight built on `bfree` accepts a job that fills the disk for
      // everyone else -- and the difference is 5% on ext4, which is exactly
      // the tolerance the `df` test below has to allow. On APFS the two are
      // equal, so on a developer Mac the distinction is not observable at all.
      //
      // Injecting statfs is what makes the choice checkable on ANY machine
      // rather than only on one with a reserved filesystem.
      const reserved = new LocalDiskStorage(root, {
        statfs: async () => ({ bavail: 100n, bfree: 500n, bsize: 4096 }) as never,
      });

      await expect(reserved.freeBytes()).resolves.toBe(100 * 4096);
    });

    it('agrees with df about the filesystem holding DATA_DIR', async () => {
      const reported = await storage.freeBytes();

      const df = execFileSync('df', ['-k', root], { encoding: 'utf8' }).trim().split('\n');
      const available = Number(df[df.length - 1].split(/\s+/)[3]) * 1024;

      expect(reported).toBeGreaterThan(0);
      // Tolerance, not equality: `df` and statfs are two samples of a live
      // filesystem taken milliseconds apart on a machine that is also doing
      // other things.
      expect(Math.abs(reported - available)).toBeLessThan(Math.max(available * 0.05, 512 * 1024 * 1024));
    });

    it('reports free space even before DATA_DIR has anything in it', async () => {
      await expect(new LocalDiskStorage(root).freeBytes()).resolves.toBeGreaterThan(0);
    });
  });
});

/** Spawns the out-of-process writer used by the SIGKILL test. */
function spawnCrashWriter(root: string, key: string): ChildProcess {
  return spawn(
    process.execPath,
    ['-r', 'ts-node/register', join(__dirname, '..', 'support', 'crash-writer.ts'), root, key],
    { env: { ...process.env, TS_NODE_TRANSPILE_ONLY: 'true' }, stdio: 'ignore' },
  );
}
