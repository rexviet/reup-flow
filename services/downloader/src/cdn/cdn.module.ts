import { Global, Inject, Module, type OnApplicationShutdown } from '@nestjs/common';
import { UndiciCdnClient, type CdnClient } from './cdn.client';

/** Injection token for the shared `CdnClient`. */
export const CDN_CLIENT = 'CDN_CLIENT';

/**
 * The CDN port, wired the same way `ParserModule` wires the parser.
 *
 * ONE instance, deliberately, and for a stronger reason than tidiness: the
 * client owns a private undici `Agent` (see cdn.client.ts on why it must not
 * use the process-wide dispatcher), and an `Agent` is a connection pool.
 * `DOWNLOAD_CONCURRENCY` clients would be six pools to the same host, six sets
 * of keep-alive sockets, and six things to remember to close.
 *
 * It takes nothing from `ENV`: there is no base URL to configure -- every
 * address comes off a `play_addr` -- and the timeout is a property of what a
 * CDN is, not of a deployment. A knob nobody would turn is a knob that goes
 * stale.
 */
@Global()
@Module({
  providers: [
    {
      provide: CDN_CLIENT,
      useFactory: (): CdnClient => new UndiciCdnClient(),
    },
  ],
  exports: [CDN_CLIENT],
})
export class CdnModule implements OnApplicationShutdown {
  constructor(@Inject(CDN_CLIENT) private readonly client: CdnClient) {}

  /**
   * `onApplicationShutdown`, NOT `onModuleDestroy` -- the last teardown phase.
   *
   * `ParserModule` records the full story and the five-minute blast radius it
   * had: Nest runs every `onModuleDestroy`, then every
   * `beforeApplicationShutdown` (where `LaneSupervisor` drains the lanes and
   * releases their leases), then every `onApplicationShutdown`. A download lane
   * draining in the middle phase may still be reading from this client, so
   * closing it any earlier would cut a transfer off in a way that reads as a
   * download failure rather than as a shutdown.
   */
  async onApplicationShutdown(): Promise<void> {
    await this.client.close();
  }
}
