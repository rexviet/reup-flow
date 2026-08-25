/**
 * Does the boot-time F4 probe check actually RUN in production?
 *
 * `test/parser/probe-health.spec.ts` proves the class behaves. That is not the
 * same claim, and the difference is the kind that has bitten this repo: the
 * check is registered with a `useFactory`, and a Nest lifecycle hook on a
 * factory-provided instance is exactly the sort of thing one assumes works.
 * `ProbeHealthCheck` is also the only guard in the service whose failure output
 * IS a log line -- if the hook never fired, nothing anywhere would look
 * different, and the phase-07 risk it exists for would be un-mitigated while
 * looking mitigated.
 *
 * So this boots the real `AppModule` twice: once with lanes on (the probe must
 * be spent) and once with lanes off (it must not be). Both halves matter --
 * "the hook ran" is only evidence if the same instrument can also show it not
 * running.
 */
import { Test } from '@nestjs/testing';
import { AppModule } from '../../src/app.module';
import { LANES } from '../../src/lanes/lanes.tokens';
import { PARSER_CLIENT } from '../../src/parser/parser.module';
import { ProbeHealthCheck } from '../../src/parser/probe-health';
import type { UserProfile } from '../../src/parser/parser.client';

jest.setTimeout(60_000);

const PROBE_ID = 'MS4wLjABAAAA_wiring_probe';
const originalEnv = { ...process.env };

/** Counts probes and never touches a network. `close` is required by ParserModule's shutdown. */
function countingParser() {
  return {
    probes: [] as string[],
    async fetchUserProfile(secUserId: string): Promise<UserProfile> {
      this.probes.push(secUserId);
      return { nickname: 'a known public account', statusCode: 0 };
    },
    async close(): Promise<void> {},
  };
}

async function boot(lanesEnabled: boolean) {
  Object.assign(process.env, {
    API_KEY: 'probe-health-wiring-key-not-real',
    DATABASE_URL: 'postgres://reup:reup@localhost:5432/reup',
    REDIS_URL: 'redis://localhost:6379/9',
    PARSER_BASE_URL: 'http://parser:80',
    PROBE_SEC_USER_ID: PROBE_ID,
    LANES_ENABLED: lanesEnabled ? 'true' : 'false',
    // Kept low purely as belt-and-braces: `LANES` is replaced below, so nothing
    // here should construct six loops, and a stray one must be cheap.
    DOWNLOAD_CONCURRENCY: '1',
  });
  const parser = countingParser();
  const moduleRef = await Test.createTestingModule({ imports: [AppModule] })
    .overrideProvider(PARSER_CLIENT)
    .useValue(parser)
    // LANES emptied, not disabled: `LANES_ENABLED` is the variable under test
    // here, so it has to keep its real value -- but letting six real loops claim
    // against the live compose stack's `public` schema as a side effect of
    // asserting a log line would be a test that damages what it measures.
    // `sigterm-shutdown.e2e-spec.ts` replaces the same provider for the same
    // reason.
    .overrideProvider(LANES)
    .useValue([])
    .compile();
  const app = moduleRef.createNestApplication();
  await app.init();
  const check = moduleRef.get(ProbeHealthCheck);
  // Awaited HERE, not inside a test, because the hook deliberately does not
  // block bootstrap (see ProbeHealthCheck). Without this the assertions would
  // race the probe, and a racing assertion that happens to pass is the kind of
  // green this repo has learned to distrust.
  await check.done;
  return { parser, check, close: () => app.close() };
}

afterAll(() => {
  process.env = { ...originalEnv };
});

describe('the F4 probe health check, as production wires it', () => {
  it('runs at bootstrap on a node that runs lanes', async () => {
    const { parser, check, close } = await boot(true);
    try {
      // The hook fired at all -- the thing a `useFactory` provider makes worth
      // asserting rather than assuming.
      expect(parser.probes).toEqual([PROBE_ID]);
      // And it read PROBE_SEC_USER_ID from env, not a constant: the id above is
      // deliberately not the value any other suite uses.
      expect(check.healthy).toBe(true);
    } finally {
      await close();
    }
  });

  it('does not run on an API-only node', async () => {
    // The negative half. Without it, "probes.length === 1" above could be true
    // of an instrument that always reports one, and the LANES_ENABLED gate
    // would be untested -- which on a fleet of API nodes is one wasted request
    // per node per deploy against an IP-based rate limit.
    const { parser, check, close } = await boot(false);
    try {
      expect(parser.probes).toEqual([]);
      expect(check.healthy).toBeNull();
    } finally {
      await close();
    }
  });
});
