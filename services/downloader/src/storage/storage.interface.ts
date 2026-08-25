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
   * NOTE FOR PHASE 6 -- the shape of the lease check, decided here while it
   * is still free.
   *
   * The risk register requires the download lane to re-check its lease
   * immediately before the rename, so a worker the reaper already replaced
   * (F3) declines to commit. `put()` owns the rename, so the check has to
   * arrive as a `beforeCommit?: () => Promise<boolean>` argument.
   *
   * That addition is NOT purely additive, and the two traps are worth naming
   * now rather than discovering under a deadline:
   *
   *   1. On `false` there is no `PutResult` to return -- the file was never
   *      committed. The return type has to widen to `PutResult | null`, or
   *      the method has to throw a distinguishable error. Either way every
   *      existing caller is affected, so decide it before there are many.
   *   2. A declined commit must still unlink the temp file. Today's cleanup
   *      runs only on the throw path (`local-disk.storage.ts`), so a plain
   *      `if (!ok) return null` would leak a full-size file per reaped
   *      download -- invisible to `exists()` and silently eating the space
   *      `freeBytes()` promises the phase-10 preflight.
   *
   * Deliberately not implemented yet: nothing calls it, and a hook with no
   * caller is a hook with no test.
   */
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
