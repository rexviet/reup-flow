import { Readable } from 'node:stream';
import { S3Storage } from '../../src/storage/s3.storage';
import { makeS3Client, readS3TestEnv } from './s3-test-env';

/**
 * A second PROCESS doing an upload that outlives the parent's sweep.
 *
 * Separate process because the in-flight registry is in-memory: a sweep in the
 * parent cannot see this upload's id, so only the age threshold can protect
 * it -- which is exactly what the cross-process test asks.
 *
 * Protocol on stdout: `READY` once the first part is in the store, then it
 * waits for a line on stdin before sending the rest, then prints `DONE <bytes>`.
 */
const [keyPrefix, key, headBytes, tailBytes] = process.argv.slice(2);

const env = readS3TestEnv();
const storage = new S3Storage(makeS3Client(env), {
  bucket: env.bucket,
  keyPrefix,
  staleUploadMs: 86_400_000,
});

const waitForGo = new Promise<void>((resolve) => {
  process.stdin.once('data', () => resolve());
});

const body = Readable.from(
  (async function* () {
    yield Buffer.alloc(Number(headBytes), 0x41);
    console.log('READY');
    await waitForGo;
    yield Buffer.alloc(Number(tailBytes), 0x42);
  })(),
);

storage
  .put(key, body, Number(headBytes) + Number(tailBytes))
  .then((res) => {
    console.log(`DONE ${res.bytes}`);
    process.exit(0);
  })
  .catch((err) => {
    console.error(`FAILED ${err instanceof Error ? err.message : String(err)}`);
    process.exit(1);
  });
