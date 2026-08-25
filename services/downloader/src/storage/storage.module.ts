import { Global, Module } from '@nestjs/common';
import { ENV } from '../config/config.module';
import type { Env } from '../config/env';
import { LocalDiskStorage } from './local-disk.storage';
import type { Storage } from './storage.interface';

/** Injection token for the shared `Storage`. */
export const STORAGE = 'STORAGE';

/**
 * @Global() for the same reason as `DbModule`/`ParserModule`: the download
 * lane (phase 6) and the reaper both need it, and phase 10's disk preflight
 * will too.
 *
 * ONE instance, deliberately. `LocalDiskStorage` keeps an in-memory set of the
 * temp files it is currently writing, which is what makes `removeStaleTemp`
 * safe to run alongside live downloads (see its comment). Two instances would
 * each know only their own writes, and the reaper's would know none of them.
 */
@Global()
@Module({
  providers: [
    {
      provide: STORAGE,
      inject: [ENV],
      useFactory: (env: Env): Storage => new LocalDiskStorage(env.DATA_DIR),
    },
  ],
  exports: [STORAGE],
})
export class StorageModule {}
