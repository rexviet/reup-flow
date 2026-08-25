/**
 * Phase 6 -- `CdnModule` wires `CDN_CLIENT` the same way `ParserModule` wires
 * `PARSER_CLIENT`: `@Global()`, one instance, one lifecycle hook releasing what
 * it built.
 *
 * ONE instance matters for the same reason it does there: the client owns a
 * private undici `Agent`, so a per-lane client would mean six connection pools
 * to the same CDN and six sets of keep-alive sockets nobody closes.
 */
import { Test } from '@nestjs/testing';
import { CDN_CLIENT, CdnModule } from '../../src/cdn/cdn.module';
import { UndiciCdnClient } from '../../src/cdn/cdn.client';
import { ConfigModule } from '../../src/config/config.module';
import { fixedEnv } from '../support/fixed-env';

describe('CdnModule', () => {
  it('provides an UndiciCdnClient under the CDN_CLIENT token', async () => {
    const moduleRef = await Test.createTestingModule({
      imports: [ConfigModule.withEnv(fixedEnv()), CdnModule],
    }).compile();

    expect(moduleRef.get(CDN_CLIENT)).toBeInstanceOf(UndiciCdnClient);

    await moduleRef.close();
  });

  it('closes it in the LAST teardown phase, not the first', () => {
    // `onApplicationShutdown`, NOT `onModuleDestroy`, and the invocation test
    // below cannot tell them apart: `moduleRef.close()` runs every phase, so
    // moving the hook keeps that spy at 1. Phase 4 records this exact ordering
    // mistake as its worst find -- `pool.end()` in the first phase, the lane's
    // lease release in the middle one, five minutes of a restarted process
    // idling beside work it could not claim -- and every in-process shutdown
    // test stayed green through it.
    //
    // So this asserts the SHAPE: which hook the class implements. Nest runs
    // every `onModuleDestroy`, then every `beforeApplicationShutdown` (where
    // `LaneSupervisor` drains the lanes), then every `onApplicationShutdown`. A
    // download lane still draining may be reading from this client, so it may
    // only be closed in the last phase.
    const proto = CdnModule.prototype as unknown as Record<string, unknown>;
    expect(typeof proto.onApplicationShutdown).toBe('function');
    expect(proto.onModuleDestroy).toBeUndefined();
    expect(proto.beforeApplicationShutdown).toBeUndefined();
  });

  it('closes the client on shutdown, so a keep-alive socket cannot hold the process open', async () => {
    const moduleRef = await Test.createTestingModule({
      imports: [ConfigModule.withEnv(fixedEnv()), CdnModule],
    }).compile();
    const client = moduleRef.get<UndiciCdnClient>(CDN_CLIENT);
    const closeSpy = jest.spyOn(client, 'close');

    await moduleRef.close();

    expect(closeSpy).toHaveBeenCalledTimes(1);
  });
});
