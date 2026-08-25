import { Readable } from 'node:stream';

export interface Deferred<T = void> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (err: unknown) => void;
}

export function deferred<T = void>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (err: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/**
 * A stream that stops after `chunks[0]` until `gate` settles.
 *
 * Storage tests need to observe the filesystem WHILE a write is in flight --
 * that is the only moment at which "is a half-written file visible under its
 * final name?" has an answer. A pre-built buffer would be written and
 * committed before any assertion could run.
 */
export function gatedStream(chunks: Buffer[], gate: Promise<unknown>): Readable {
  return Readable.from(
    (async function* () {
      const [first, ...rest] = chunks;
      if (first) yield first;
      await gate;
      for (const chunk of rest) yield chunk;
    })(),
  );
}

/** A stream that delivers `head` and then fails, standing in for a dropped connection. */
export function failingStream(head: Buffer, error: Error): Readable {
  return Readable.from(
    (async function* () {
      yield head;
      throw error;
    })(),
  );
}

/**
 * Two streams that are provably writing at the same time.
 *
 * `Promise.all` over two puts proves only that both were DISPATCHED
 * concurrently; the first can finish -- rename included -- before the second
 * opens its file, which is exactly the shape that lets a shared temp-file
 * name pass unnoticed. Each stream here yields one chunk, reports arrival,
 * and refuses to continue until every other stream has arrived too, so both
 * writers are guaranteed to hold an open file handle simultaneously.
 */
export interface WriterSpec {
  /** Byte value this writer fills its payload with. */
  fill: number;
  /** How many chunks it writes. Give writers DIFFERENT counts -- see below. */
  chunks: number;
}

export function overlappingStreams(
  specs: WriterSpec[],
  chunkSize: number,
): { streams: Readable[]; payloads: Buffer[] } {
  const arrivals = specs.map(() => deferred());
  const allArrived = Promise.all(arrivals.map((a) => a.promise)).then(() => undefined);

  const streams = specs.map((spec, i) =>
    Readable.from(
      (async function* () {
        yield Buffer.alloc(chunkSize, spec.fill);
        arrivals[i].resolve();
        await allArrived;
        for (let n = 1; n < spec.chunks; n += 1) {
          yield Buffer.alloc(chunkSize, spec.fill);
          // Yield to the event loop between chunks so the writers really do
          // interleave rather than each running to completion in one tick.
          await new Promise((resolve) => setImmediate(resolve));
        }
      })(),
    ),
  );

  return {
    streams,
    payloads: specs.map((spec) => Buffer.alloc(chunkSize * spec.chunks, spec.fill)),
  };
}

/** Polls `predicate` until true, or throws. Used to catch a write mid-flight. */
export async function waitUntil(
  predicate: () => Promise<boolean> | boolean,
  label: string,
  timeoutMs = 5000,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    if (await predicate()) return;
    if (Date.now() > deadline) throw new Error(`timed out waiting for: ${label}`);
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}
