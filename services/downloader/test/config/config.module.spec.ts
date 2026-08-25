import { Test } from '@nestjs/testing';
import { ConfigModule, ENV } from '../../src/config/config.module';
import type { Env } from '../../src/config/env';
import { fixedEnv } from '../support/fixed-env';

const REQUIRED_ENV_VARS: Record<string, string> = {
  API_KEY: 'test-key-not-real',
  DATABASE_URL: 'postgres://reup:reup@localhost:5432/reup',
  REDIS_URL: 'redis://localhost:6379',
  PARSER_BASE_URL: 'http://parser:80',
  PROBE_SEC_USER_ID: 'probe-sec-user-id',
};

describe('ConfigModule', () => {
  describe('withEnv', () => {
    it('provides the exact Env object injected, bypassing process.env entirely', async () => {
      const env = fixedEnv({ API_KEY: 'fixed-env-key' });

      const moduleRef = await Test.createTestingModule({
        imports: [ConfigModule.withEnv(env)],
      }).compile();

      expect(moduleRef.get(ENV)).toBe(env);
    });
  });

  describe('default provider', () => {
    const originalEnv = { ...process.env };

    beforeEach(() => {
      for (const [key, value] of Object.entries(REQUIRED_ENV_VARS)) {
        process.env[key] = value;
      }
    });

    afterEach(() => {
      process.env = { ...originalEnv };
    });

    it('parses process.env via parseEnv()', async () => {
      const moduleRef = await Test.createTestingModule({
        imports: [ConfigModule],
      }).compile();

      const env = moduleRef.get<Env>(ENV);
      expect(env.API_KEY).toBe(REQUIRED_ENV_VARS.API_KEY);
      expect(env.DATABASE_URL).toBe(REQUIRED_ENV_VARS.DATABASE_URL);
    });

    it('throws EnvValidationError at module compile time when process.env is invalid', async () => {
      delete process.env.DATABASE_URL;

      await expect(
        Test.createTestingModule({ imports: [ConfigModule] }).compile(),
      ).rejects.toThrow(/DATABASE_URL/);
    });
  });
});
