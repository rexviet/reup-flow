import { Global, Module } from '@nestjs/common';
import { ENV } from '../config/config.module';
import type { Env } from '../config/env';
import { LocalDiskStorage } from './local-disk.storage';
import { createS3Storage } from './s3.storage';
import type { Storage } from './storage.interface';

/** Injection token for the shared `Storage`. */
export const STORAGE = 'STORAGE';

/**
 * Both credentials and the endpoint are guaranteed present once
 * `STORAGE_BACKEND=s3`: `env.ts` refuses to boot otherwise.
 */
function s3FromEnv(env: Env): Storage {
  return createS3Storage({
    endpoint: env.S3_ENDPOINT as string,
    region: env.S3_REGION,
    forcePathStyle: env.S3_FORCE_PATH_STYLE,
    accessKeyId: env.S3_ACCESS_KEY_ID as string,
    secretAccessKey: env.S3_SECRET_ACCESS_KEY as string,
    bucket: env.S3_BUCKET,
    keyPrefix: env.S3_KEY_PREFIX,
    staleUploadMs: env.S3_STALE_UPLOAD_MS,
  });
}

/**
 * @Global() for the same reason as `DbModule`/`ParserModule`: the download
 * lane (phase 6) and the reaper both need it, and phase 10's disk preflight
 * will too.
 *
 * ONE instance, deliberately. Both backends keep an in-memory set of the
 * uploads they are currently writing, which is what makes `removeStaleTemp`
 * safe to run alongside live downloads (see their comments). Two instances
 * would each know only their own writes, and the reaper's would know none of
 * them.
 */
@Global()
@Module({
  providers: [
    {
      provide: STORAGE,
      inject: [ENV],
      useFactory: (env: Env): Storage =>
        env.STORAGE_BACKEND === 's3' ? s3FromEnv(env) : new LocalDiskStorage(env.DATA_DIR),
    },
  ],
  exports: [STORAGE],
})
export class StorageModule {}
