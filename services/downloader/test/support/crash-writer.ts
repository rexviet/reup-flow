import { Readable } from 'node:stream';
import { LocalDiskStorage } from '../../src/storage/local-disk.storage';

/**
 * Writes to storage forever, so the parent can SIGKILL it mid-write.
 *
 * Deliberately a separate PROCESS. An in-process "simulated crash" always
 * runs some cleanup path of ours; a real kill runs none, which is the only
 * honest way to ask whether the on-disk state is safe at an arbitrary
 * instant.
 */
const [root, key] = process.argv.slice(2);

const forever = Readable.from(
  (async function* () {
    for (;;) {
      yield Buffer.alloc(64 * 1024, 0x41);
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
  })(),
);

new LocalDiskStorage(root).put(key, forever).catch(() => process.exit(1));
