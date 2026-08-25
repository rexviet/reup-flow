import type { Readable } from 'node:stream';

export interface PutResult {
  /** Absolute filesystem path of the committed file. */
  path: string;
  /** Bytes actually written. */
  bytes: number;
}

/**
 * The only way anything in this service touches durable bytes.
 *
 * The point of the interface is that the download lane (phase 6) never
 * learns whether it is writing to a local disk, an NFS mount, or an object
 * store. The lane's job is "get these bytes committed, atomically, or fail";
 * where they land is a deployment question that plan §8 deliberately leaves
 * open.
 *
 * Note what is NOT here: no `open`, no `write`, no `append`. A partially
 * written file must never be observable under its final key, so the only
 * offered shape is "hand me the whole stream and I will commit it or leave
 * nothing behind".
 */
/**
 * Asked once, immediately before the rename, with the whole body already on
 * disk and verified: may this still be committed?
 *
 * `false` means "no" -- the temp file is deleted and `put` answers `null`.
 * This is the download lane's ownership re-check (phase 6): a worker the
 * reaper already replaced must decline rather than commit its bytes over the
 * replacement's. Throwing (a database that just went away) is treated as a
 * failed put, not as a decline.
 */
export type BeforeCommit = () => Promise<boolean>;

export interface Storage {
  /**
   * Streams `stream` to `key`, atomically.
   *
   * `expectedBytes` -- when the source advertises a Content-Length, pass it.
   * A truncated response (connection dropped mid-body) is otherwise
   * indistinguishable from a complete one, and the video would be marked
   * `done` with a short file that nobody discovers until playback.
   *
   * Throws, leaving no file at `key`, if the stream errors or the byte count
   * disagrees.
   */
  put(key: string, stream: Readable, expectedBytes?: number): Promise<PutResult>;
  /**
   * PHASE 6 -- the same put, with an ownership re-check wired into the commit.
   *
   * The risk register's "Reaper nhả video, 2 worker commit 1 file" is closed
   * here, because `put` owns the rename and the rename is the only commit
   * point. Phase 2 wrote down the two traps this addition falls into, and both
   * are answered by the signature:
   *
   *   1. A declined commit has no `PutResult`, so THIS overload -- and only
   *      this one -- answers `PutResult | null`. Callers that pass no hook keep
   *      the narrow return type, which is why this is an overload rather than a
   *      widened single signature.
   *   2. A declined commit still unlinks the temp file. Phase 2's cleanup ran
   *      only on the throw path, so a plain `if (!ok) return null` would leak a
   *      FULL-SIZE video per reaped download -- invisible to `exists()`, and
   *      eating exactly the space `freeBytes()` promises the phase-10
   *      preflight. `test/storage/before-commit.spec.ts` asserts the file's
   *      ABSENCE ON DISK, not that a cleanup function was called.
   */
  put(
    key: string,
    stream: Readable,
    expectedBytes: number | undefined,
    beforeCommit: BeforeCommit,
  ): Promise<PutResult | null>;
  /** True only for a COMMITTED file -- never for one still being written. */
  exists(key: string): Promise<boolean>;
  /** Bytes available to an unprivileged writer. Feeds the phase-10 preflight. */
  freeBytes(): Promise<number>;
  /** Removes a committed file. A missing file is not an error. */
  remove(key: string): Promise<void>;
  /**
   * Deletes every uncommitted temp file nothing has written to for
   * `olderThanMs`, returning the paths removed.
   *
   * On the interface rather than only on the local-disk class because the
   * problem is not local-disk-specific: any backend that commits by moving a
   * staged object leaves the staged object behind when a worker is killed, and
   * the reaper (phase 4 §4.3) needs one call it can make without knowing which
   * backend it is talking to. See the implementation for why the cutoff is read
   * off MTIME.
   */
  removeStaleTemp(olderThanMs: number): Promise<string[]>;
}

/**
 * The one place the storage layout is decided.
 *
 * `douyin/{sec_user_id}/{aweme_id}.mp4` -- profile-scoped so an operator can
 * find, tar, or delete one profile's downloads without a database, and so a
 * single directory never accumulates every video ever fetched.
 */
export function videoKey(secUserId: string, awemeId: string): string {
  return `douyin/${secUserId}/${awemeId}.mp4`;
}
