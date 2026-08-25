import { Test } from '@nestjs/testing';
import type Redis from 'ioredis';
import { ConfigModule } from '../../src/config/config.module';
import { RedisModule, REDIS } from '../../src/redis/redis.module';
import { fixedEnv } from '../support/fixed-env';

/**
 * Integration suite: exercises RedisModule against the real docker-compose
 * Redis (localhost:6379). The behaviour under test -- "a dead Redis fails
 * the boot probe promptly instead of retrying forever" -- is precisely
 * ioredis's own retry/connect machinery, which no mock client can stand in
 * for without reimplementing it.
 */
describe('RedisModule', () => {
  it('boots successfully, provides a working REDIS client, and disconnects on destroy', async () => {
    const moduleRef = await Test.createTestingModule({
      imports: [ConfigModule.withEnv(fixedEnv()), RedisModule],
    }).compile();
    const app = moduleRef.createNestApplication();

    await app.init();

    const client = app.get<Redis>(REDIS);
    expect(await client.ping()).toBe('PONG');

    await app.close();

    // After OnModuleDestroy has disconnected the client, a command must not
    // silently succeed against a live connection that was supposed to be
    // gone.
    await expect(client.ping()).rejects.toThrow();
  });

  it('fails fast at boot (does not hang) when Redis is unreachable', async () => {
    // Port 6399: nothing listens there in the compose stack.
    const badEnv = fixedEnv({ REDIS_URL: 'redis://localhost:6399' });
    const moduleRef = await Test.createTestingModule({
      imports: [ConfigModule.withEnv(badEnv), RedisModule],
    }).compile();
    const app = moduleRef.createNestApplication();

    await expect(app.init()).rejects.toThrow();

    await app.close().catch(() => {
      /* app never finished initialising; nothing more to tear down */
    });
    // The assertion that actually matters here is implicit: this test's own
    // jest timeout (the suite default) is what proves "does not hang" --
    // a retryStrategy that retries forever would time out this test rather
    // than reject.
  }, 10000);
});
