/**
 * Phase 4 -- `removeStaleTemp`, the missing half of the risk register's
 * "`.tmp` mồ côi không ai dọn" row (plan §6, assigned to THIS phase).
 *
 * `local-disk.storage.spec.ts` already asserts that a `SIGKILL`ed writer
 * leaves its temp file behind and commits nothing -- correct behaviour, and
 * the reason the temp name is unique per put. What nothing did was delete it
 * afterwards. The reaper (F3) recovers the DATABASE ROW; the bytes stay. Each
 * one is a full-size video, `exists()` cannot see it because it is not under
 * the final key, and it eats exactly the space `freeBytes()` promises the
 * phase-10 preflight. A crash-loop accumulates them at ~30MB a turn until the
 * disk fills, at which point the preflight is measuring a lie.
 *
 * The rule is MTIME, not creation time, and that is the whole design. A temp
 * file whose worker is alive is being written to, so its mtime keeps moving. A
 * cutoff of one lease means "nothing has written to this for longer than the
 * lease its writer would have had to renew" -- which is the same evidence the
 * row-level reaper acts on, read off the filesystem instead of off Postgres.
 */
import { mkdir, mkdtemp, readdir, rm, utimes, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Readable } from 'node:stream';
import { LocalDiskStorage } from '../../src/storage/local-disk.storage';
import { videoKey } from '../../src/storage/storage.interface';

const SEC_USER = 'MS4wLjABAAAA_abc';

describe('LocalDiskStorage.removeStaleTemp', () => {
  let root: string;
  let storage: LocalDiskStorage;

  beforeEach(async () => {
    root = await mkdtemp(join(tmpdir(), 'reup-tmpsweep-'));
    storage = new LocalDiskStorage(root);
  });

  afterEach(async () => {
    await rm(root, { recursive: true, force: true });
  });

  /** Writes a temp file shaped exactly like `put`'s, aged `ageMs` by mtime. */
  async function seedTemp(name: string, ageMs: number, dir = join(root, 'douyin', SEC_USER)) {
    await mkdir(dir, { recursive: true });
    const path = join(dir, name);
    await writeFile(path, Buffer.alloc(64, 7));
    const when = new Date(Date.now() - ageMs);
    await utimes(path, when, when);
    return path;
  }

  async function listing(dir = join(root, 'douyin', SEC_USER)): Promise<string[]> {
    try {
      return (await readdir(dir)).sort();
    } catch {
      return [];
    }
  }

  it('removes a temp file whose mtime is older than the cutoff', async () => {
    await seedTemp('7372484719365098803.mp4.abc-uuid.tmp', 10 * 60_000);

    const removed = await storage.removeStaleTemp(5 * 60_000);

    expect(removed).toHaveLength(1);
    expect(await listing()).toEqual([]);
  });

  it('KEEPS a temp file that was written to recently -- its worker is still alive', async () => {
    // The case that makes mtime the right clock. An expired lease does not
    // mean the worker is dead (local-disk.storage.ts's `put` comment), and
    // deleting the temp file out from under a live download makes its `rename`
    // fail with ENOENT -- the exact failure phase 2's journal spent a day on.
    await seedTemp('7372484719365098803.mp4.abc-uuid.tmp', 1_000);

    expect(await storage.removeStaleTemp(5 * 60_000)).toEqual([]);
    expect(await listing()).toHaveLength(1);
  });

  it('never touches a COMMITTED file, however old', async () => {
    // A downloaded video from last month is not garbage. Only the `.tmp`
    // suffix marks a file as uncommitted, which is why the sweep keys on it
    // and not on age alone.
    await seedTemp('7372484719365098803.mp4', 90 * 24 * 3600_000);

    expect(await storage.removeStaleTemp(5 * 60_000)).toEqual([]);
    expect(await listing()).toEqual(['7372484719365098803.mp4']);
  });

  it('never touches an old file that merely CONTAINS .tmp in its name', async () => {
    await seedTemp('a.tmp.mp4', 90 * 24 * 3600_000);

    expect(await storage.removeStaleTemp(5 * 60_000)).toEqual([]);
    expect(await listing()).toEqual(['a.tmp.mp4']);
  });

  it('sweeps every profile directory, not just one', async () => {
    // The reaper has no list of keys to sweep -- a crashed worker's key is not
    // recorded anywhere. So the sweep walks DATA_DIR.
    await seedTemp('1.mp4.u1.tmp', 10 * 60_000, join(root, 'douyin', 'profile-a'));
    await seedTemp('2.mp4.u2.tmp', 10 * 60_000, join(root, 'douyin', 'profile-b'));

    expect(await storage.removeStaleTemp(5 * 60_000)).toHaveLength(2);
    expect(await listing(join(root, 'douyin', 'profile-a'))).toEqual([]);
    expect(await listing(join(root, 'douyin', 'profile-b'))).toEqual([]);
  });

  it('returns the paths it removed, so the reaper can log what it reclaimed', async () => {
    const path = await seedTemp('1.mp4.u1.tmp', 10 * 60_000);

    expect(await storage.removeStaleTemp(5 * 60_000)).toEqual([path]);
  });

  it('is a no-op when DATA_DIR does not exist yet', async () => {
    // Same reasoning as `freeBytes`: a fresh deployment has no DATA_DIR until
    // the first download, and the 60s reaper starts long before that. Asking
    // must not be an error, and must not create directories as a side effect.
    const fresh = new LocalDiskStorage(join(root, 'not-created-yet'));

    await expect(fresh.removeStaleTemp(5 * 60_000)).resolves.toEqual([]);
  });

  it('keeps going when one file cannot be removed', async () => {
    // One unreadable entry must not abandon the rest of the sweep: the whole
    // point is reclaiming space, and stopping at the first problem is how a
    // full disk stays full.
    const good = await seedTemp('good.mp4.u1.tmp', 10 * 60_000);
    // A DIRECTORY named like a stale temp file: `unlink` on it fails with
    // EISDIR (or EPERM), which is a realistic stand-in for any per-entry error.
    await seedTemp('placeholder', 10 * 60_000, join(root, 'douyin', SEC_USER, 'weird.mp4.u2.tmp'));

    const removed = await storage.removeStaleTemp(5 * 60_000);

    expect(removed).toContain(good);
    expect(await listing()).toEqual(['weird.mp4.u2.tmp']);
  });

  it('does not delete the temp file of a put that is still in flight', async () => {
    // End to end against the real `put`, not a hand-seeded file: this is the
    // regression that matters, and it is the one a fixture cannot prove.
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const slow = new Readable({
      read() {
        void gate.then(() => {
          this.push(Buffer.alloc(32, 3));
          this.push(null);
        });
      },
    });
    const writing = storage.put(videoKey(SEC_USER, '777'), slow);
    // Give the pipeline time to create the temp file.
    await new Promise((r) => setTimeout(r, 30));

    // Cutoff of 0ms: EVERY temp file looks stale by age. The in-flight one
    // must still survive, because a sweep this aggressive is what an operator
    // reaching for a manual cleanup would run.
    const removed = await storage.removeStaleTemp(0);
    release();
    await writing;

    expect(removed).toEqual([]);
    expect(await listing()).toEqual(['777.mp4']);
  });
});
