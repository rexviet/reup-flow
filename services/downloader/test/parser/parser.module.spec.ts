/**
 * Phase 3 (group B) -- `ParserModule` wires `PARSER_CLIENT` for every
 * feature module (jobs, videos) exactly the way `DbModule`/`RedisModule`
 * wire `DB`/`REDIS`: `@Global()`, one factory reading `ENV`, one lifecycle
 * hook releasing what it built.
 */
import { Test } from '@nestjs/testing';
import { ConfigModule } from '../../src/config/config.module';
import { ParserClient } from '../../src/parser/parser.client';
import { PARSER_CLIENT, ParserModule } from '../../src/parser/parser.module';
import { fixedEnv } from '../support/fixed-env';

describe('ParserModule', () => {
  it('provides a ParserClient built from env.PARSER_BASE_URL under the PARSER_CLIENT token', async () => {
    const moduleRef = await Test.createTestingModule({
      imports: [ConfigModule.withEnv(fixedEnv({ PARSER_BASE_URL: 'http://parser-under-test:80' })), ParserModule],
    }).compile();

    const client = moduleRef.get(PARSER_CLIENT);
    expect(client).toBeInstanceOf(ParserClient);

    await moduleRef.close();
  });

  it('closes the client (its private undici Agent) on module destroy, so a live keep-alive socket cannot keep the process from exiting', async () => {
    const moduleRef = await Test.createTestingModule({
      imports: [ConfigModule.withEnv(fixedEnv()), ParserModule],
    }).compile();
    const client = moduleRef.get<ParserClient>(PARSER_CLIENT);
    const closeSpy = jest.spyOn(client, 'close');

    await moduleRef.close();

    expect(closeSpy).toHaveBeenCalledTimes(1);
  });
});
