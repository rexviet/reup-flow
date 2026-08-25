/**
 * Phase 6 -- the `beforeCommit` hook on `Storage.put`.
 *
 * The risk register's "Reaper nhả video, 2 worker commit 1 file" is closed by
 * re-checking ownership immediately before the rename, and `put` owns the
 * rename. So the check has to arrive here, as a hook, and phase 2 already
 * wrote down the two traps it would fall into (see the long note in
 * `storage.interface.ts`):
 *
 *   1. A declined commit has no `PutResult` to return.
 *   2. A declined commit must still unlink the temp file -- otherwise every
 *      reaped download leaks a FULL-SIZE video that `exists()` cannot see and
 *      that eats exactly the space `freeBytes()` promises the phase-10
 *      preflight.
 *
 * Both are asserted here by looking at the DISK, never at a spy. A spy that
 * proves cleanup RAN says nothing about whether the file is gone.
 */
import { mkdtemp, readFile, readdir, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Readable } from 'node:stream';
import { LocalDiskStorage } from '../../src/storage/local-disk.storage';
import { videoKey } from '../../src/storage/storage.interface';

const SEC_USER_ID = 'MS4wLjABAAAA_before_commit';
const AWEME_ID = '7372484719365098888';
const KEY = videoKey(SEC_USER_ID, AWEME_ID);

describe('Storage.put(beforeCommit)', () => {
  let root: string;
  let storage: LocalDiskStorage;

  beforeEach(async () => {
    root = await mkdtemp(join(tmpdir(), 'reup-before-commit-'));
    storage = new LocalDiskStorage(root);
  });

  afterEach(async () => {
    await rm(root, { recursive: true, force: true });
  });

  /** Every entry under the key's directory, committed or not. */
  async function filesInKeyDir(): Promise<string[]> {
    try {
      return (await readdir(join(root, 'douyin', SEC_USER_ID))).sort();
    } catch {
      return [];
    }
  }

  it('commits and returns the result when the hook approves', async () => {
    const payload = Buffer.alloc(3072, 0x42);

    const result = await storage.put(KEY, Readable.from([payload]), payload.length, async () => true);

    expect(result).not.toBeNull();
    expect(result!.bytes).toBe(payload.length);
    expect(await readFile(result!.path)).toEqual(payload);
    expect(await filesInKeyDir()).toEqual([`${AWEME_ID}.mp4`]);
  });

  it('returns null and commits nothing when the hook declines', async () => {
    const payload = Buffer.alloc(3072, 0x43);

    const result = await storage.put(KEY, Readable.from([payload]), payload.length, async () => false);

    expect(result).toBeNull();
    expect(await storage.exists(KEY)).toBe(false);
  });

  it('deletes the temp file when the hook declines', async () => {
    // Trap 2 from storage.interface.ts. Measured on disk: an assertion that
    // the cleanup was CALLED would pass with `unlink` deleted.
    const payload = Buffer.alloc(64 * 1024, 0x44);

    await storage.put(KEY, Readable.from([payload]), payload.length, async () => false);

    expect(await filesInKeyDir()).toEqual([]);
  });

  it('runs the hook only after the whole body is on disk and verified', async () => {
    // The order is the point. Asking "do we still own this row?" before the
    // bytes exist would answer a question about a download that has not
    // happened yet, and the answer would be stale by the time it mattered --
    // the window this hook exists to shrink is exactly the one between the
    // last byte and the rename.
    const payload = Buffer.alloc(8192, 0x45);
    let tempAtHookTime: string[] = [];
    let tempBytesAtHookTime = 0;
    let committedAtHookTime = true;

    await storage.put(KEY, Readable.from([payload]), payload.length, async () => {
      tempAtHookTime = await filesInKeyDir();
      tempBytesAtHookTime = (
        await stat(join(root, 'douyin', SEC_USER_ID, tempAtHookTime[0]!))
      ).size;
      committedAtHookTime = await storage.exists(KEY);
      return true;
    });

    expect(tempAtHookTime).toHaveLength(1);
    expect(tempAtHookTime[0]).toMatch(/\.tmp$/);
    // The FULL body, not a prefix of it.
    expect(tempBytesAtHookTime).toBe(payload.length);
    // And nothing under the final name yet.
    expect(committedAtHookTime).toBe(false);
  });

  it('never asks the hook about a body that failed verification', async () => {
    // A short body is not going to be committed whatever the hook says, and
    // the hook is a database round trip. Asking anyway would also produce a
    // log line claiming an ownership loss for a download that failed for a
    // completely different reason.
    let asked = false;

    await expect(
      storage.put(KEY, Readable.from([Buffer.alloc(100, 1)]), 999, async () => {
        asked = true;
        return true;
      }),
    ).rejects.toThrow(/received 100 bytes but 999 were advertised/);

    expect(asked).toBe(false);
    expect(await filesInKeyDir()).toEqual([]);
  });

  it('commits nothing and leaves no temp file when the hook itself throws', async () => {
    // The hook is a database call, so "Postgres just went away" is its most
    // likely failure. That must not commit, and must not leak.
    const payload = Buffer.alloc(4096, 0x46);

    await expect(
      storage.put(KEY, Readable.from([payload]), payload.length, async () => {
        throw new Error('connection terminated unexpectedly');
      }),
    ).rejects.toThrow('connection terminated unexpectedly');

    expect(await storage.exists(KEY)).toBe(false);
    expect(await filesInKeyDir()).toEqual([]);
  });

  it('lets a LATER put on the same key commit normally', async () => {
    // Replaces an assertion that could not fail. The deleted one claimed to pin
    // the in-flight bookkeeping by calling `removeStaleTemp(0)` afterwards --
    // but the declined put unlinks its temp file, and the sweep only inspects
    // paths it finds on DISK, so it answered `[]` whether the registry had been
    // cleaned or not. A test that stays green with the guard deleted is not
    // evidence (plan §5).
    //
    // What IS observable is the thing the registry exists for: the replacement
    // worker's put must not be obstructed by anything the declined one left
    // behind -- neither a file (the `wx` flag would refuse it) nor a claim.
    const payload = Buffer.alloc(2048, 0x47);
    await storage.put(KEY, Readable.from([Buffer.alloc(512, 9)]), 512, async () => false);

    const result = await storage.put(KEY, Readable.from([payload]), payload.length, async () => true);

    expect(result).not.toBeNull();
    expect(await readFile(result!.path)).toEqual(payload);
    expect(await filesInKeyDir()).toEqual([`${AWEME_ID}.mp4`]);
  });
});
