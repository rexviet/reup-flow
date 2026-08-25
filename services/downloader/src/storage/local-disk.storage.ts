import { randomUUID } from 'node:crypto';
import { createWriteStream } from 'node:fs';
import { access, mkdir, open, realpath, rename, statfs, unlink } from 'node:fs/promises';
import { dirname, isAbsolute, relative, resolve, sep } from 'node:path';
import { Transform, type Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import type { PutResult, Storage } from './storage.interface';

/**
 * `Storage` over a plain directory (`DATA_DIR`).
 *
 * Two invariants carry the whole class, and both have mutation tests
 * (`test/mutations/phase-02.sh`) rather than only comments:
 *
 *   1. Bytes are written to a temp file and `rename`d into place. The rename
 *      is the ONLY commit point, so at every instant the destination name
 *      either does not exist or holds a complete file.
 *   2. The temp name is unique PER PUT -- never derived from the key. See
 *      `put` for why a per-key temp name is a corruption bug rather than a
 *      style choice.
 */
/**
 * The one syscall this class cannot be honest about without injection.
 *
 * `freeBytes()` has to choose between `bavail` and `bfree`, and on many
 * filesystems (APFS among them) the two are identical, so no assertion
 * against the real filesystem can tell a correct implementation from a wrong
 * one. Injecting the call lets the choice be pinned deterministically on any
 * machine; production always uses the real `statfs`.
 */
export interface StorageSyscalls {
  statfs: typeof statfs;
}

export class LocalDiskStorage implements Storage {
  constructor(
    private readonly root: string,
    private readonly syscalls: StorageSyscalls = { statfs },
  ) {}

  /**
   * Streams to a unique temp file, then commits with a single `rename`.
   *
   * Why the temp name must not be `{aweme_id}.tmp`:
   *
   * The reaper (F3) hands a video whose lease expired to a new worker. An
   * expired lease does not mean the old worker is dead -- a slow download or
   * a stalled socket gets reaped while its stream is still live. Both
   * workers then hold the same key. With one temp name per key they share a
   * file descriptor's worth of destination and interleave their bytes, and
   * the rename commits an mp4 that is neither download while the row is
   * marked `done`. Nothing downstream ever notices, because the file exists
   * and has a plausible size.
   *
   * With a unique name per put, the two writes cannot touch each other; the
   * last rename wins and commits one COMPLETE file. Phase 6 narrows that
   * further by re-checking the lease immediately before the rename so a
   * reaped worker declines to commit at all -- but the file it wrote was
   * always whole either way.
   */
  async put(key: string, stream: Readable, expectedBytes?: number): Promise<PutResult> {
    const path = this.resolveKey(key);
    await mkdir(dirname(path), { recursive: true });
    await this.assertParentInsideRoot(path, key);

    // randomUUID, not the key, not a pid, not a timestamp: two workers on the
    // same key inside the same millisecond is the exact case this exists for.
    const tmpPath = `${path}.${randomUUID()}.tmp`;

    let bytes = 0;
    const count = new Transform({
      transform(chunk: Buffer, _encoding, callback) {
        bytes += chunk.length;
        callback(null, chunk);
      },
    });

    try {
      // 'wx' -- refuse to reuse an existing temp file. With a unique name this
      // can never fire; if it ever does, the naming guarantee has been broken
      // and failing loudly beats appending into someone else's download.
      await pipeline(stream, count, createWriteStream(tmpPath, { flags: 'wx' }));

      if (expectedBytes !== undefined && bytes !== expectedBytes) {
        // A dropped connection produces a short body that is otherwise
        // indistinguishable from a complete one. Without this check the video
        // is committed and marked `done`, and the truncation is discovered
        // only when somebody tries to play it.
        throw new Error(
          `storage: ${key} received ${bytes} bytes but ${expectedBytes} were advertised`,
        );
      }

      // Flush the bytes before the rename, not after.
      //
      // A rename is ordered against METADATA; the file's contents may still
      // be sitting in the page cache. A host that loses power just after the
      // rename can come back with the file present under its final name and
      // empty or torn inside -- committed, marked `done`, and unplayable,
      // with nothing to re-trigger a download. The temp-file dance exists so
      // that "committed" means "complete"; without this it would mean
      // "complete unless the machine died", which is the case it is for.
      //
      // Re-opened read-only rather than kept open across the pipeline:
      // `FileHandle.createWriteStream({ autoClose: false })` never emits
      // `close`, so `pipeline` waits on it forever. fsync does not require
      // write access.
      //
      // The directory itself is deliberately NOT fsync'd. Losing the rename
      // costs one re-download and the lease reaper (F3) already handles it;
      // losing the CONTENTS is the failure that has no detector.
      const flush = await open(tmpPath, 'r');
      try {
        await flush.sync();
      } finally {
        await flush.close();
      }

      // THE COMMIT POINT. Everything above this line is invisible under the
      // final key; everything after it is durable. `rename` within one
      // filesystem is atomic, which is why the temp file must live in the
      // destination directory rather than in /tmp.
      await rename(tmpPath, path);
    } catch (err) {
      // Best effort: a crash gets no chance to run this at all, which is why
      // correctness rests on the rename rather than on this cleanup.
      await unlink(tmpPath).catch(() => undefined);
      throw err;
    }

    return { path, bytes };
  }

  /**
   * Read-only, so it deliberately does NOT pay for the symlink check that
   * `put` and `remove` do: the worst a symlinked directory can do here is
   * answer a question about a file outside the root, which writes nothing and
   * deletes nothing.
   */
  async exists(key: string): Promise<boolean> {
    try {
      await access(this.resolveKey(key));
      return true;
    } catch (err) {
      if (isNotFound(err)) return false;
      throw err;
    }
  }

  /**
   * Bytes an unprivileged writer can still use, for the phase-10 preflight.
   *
   * `bavail`, not `bfree`: `bfree` counts the reserved blocks only root may
   * touch, so a preflight built on it would cheerfully accept a job that
   * fills the disk for everyone else.
   */
  async freeBytes(): Promise<number> {
    // DATA_DIR may not exist yet on a fresh deployment. Free space is a
    // property of the FILESYSTEM, so the nearest existing ancestor answers
    // the same question -- and answering it beats making the caller create
    // directories as a side effect of asking.
    let target = resolve(this.root);
    for (;;) {
      try {
        const stats = await this.syscalls.statfs(target);
        return Number(stats.bavail) * Number(stats.bsize);
      } catch (err) {
        const parent = dirname(target);
        if (!isNotFound(err) || parent === target) throw err;
        target = parent;
      }
    }
  }

  async remove(key: string): Promise<void> {
    const path = this.resolveKey(key);
    await this.assertParentInsideRoot(path, key);
    try {
      await unlink(path);
    } catch (err) {
      if (!isNotFound(err)) throw err;
    }
  }

  /**
   * Turns a key into an absolute path, refusing anything that leaves the root.
   *
   * Keys are built from `sec_user_id` and `aweme_id`, both of which are
   * remote data straight off Douyin. Without this, a key containing `..`
   * turns a download into an arbitrary file write anywhere the process can
   * reach.
   */
  /**
   * The half of containment that `resolveKey` cannot do.
   *
   * `resolve()` and `relative()` are purely lexical -- they never touch the
   * filesystem -- so a key containing no `..` at all still escapes if an
   * INTERMEDIATE directory is a symlink out of DATA_DIR. Remote data cannot
   * create that symlink, so this is not the Douyin-controlled attack the
   * lexical check answers; it is the shared-or-mounted-DATA_DIR case. Cheap
   * enough (one syscall per put) that the guard may as well mean what its
   * comment says.
   *
   * Both sides are resolved, so a symlinked DATA_DIR itself (`/tmp` ->
   * `/private/tmp` on macOS) is not mistaken for an escape.
   */
  private async assertParentInsideRoot(path: string, key: string): Promise<void> {
    let realParent: string;
    let realRoot: string;
    try {
      realParent = await realpath(dirname(path));
      realRoot = await realpath(this.root);
    } catch (err) {
      // Nothing there to escape through. `remove` of a missing file is a
      // no-op and `put` has just created the directory, so this only fires on
      // a genuinely absent tree.
      if (isNotFound(err)) return;
      throw err;
    }
    const rel = relative(realRoot, realParent);
    if (rel === '..' || rel.startsWith(`..${sep}`) || isAbsolute(rel)) {
      throw new Error(
        `storage: key resolves outside DATA_DIR through a symlinked directory, refusing: ${key}`,
      );
    }
  }

  private resolveKey(key: string): string {
    if (key.length === 0) {
      throw new Error('storage: key must not be empty');
    }
    if (isAbsolute(key)) {
      throw new Error(`storage: key must be relative to DATA_DIR, got ${key}`);
    }
    const path = resolve(this.root, key);
    const rel = relative(resolve(this.root), path);
    if (rel.length === 0 || rel === '..' || rel.startsWith(`..${sep}`) || isAbsolute(rel)) {
      throw new Error(`storage: key escapes DATA_DIR, refusing: ${key}`);
    }
    return path;
  }
}

function isNotFound(err: unknown): boolean {
  return typeof err === 'object' && err !== null && (err as { code?: string }).code === 'ENOENT';
}
