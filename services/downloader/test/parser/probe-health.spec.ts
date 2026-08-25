/**
 * phase-07 Risks, row 1: "`PROBE_SEC_USER_ID` trỏ profile đã chết -> probe luôn
 * fail -> mọi thứ thành `blocked`."
 *
 * This is F4 INVERTED, and it is worth being precise about why it is dangerous
 * rather than merely wrong. The probe's `failed` verdict is the one that needs
 * positive evidence: it is written only when the probe SUCCEEDS. So a probe
 * target that has itself gone private or been deleted can never produce that
 * evidence -- every 3x400 becomes `blocked`, every operator is sent to rotate a
 * cookie, and nothing anywhere reports a problem. The system looks exactly like
 * one with a genuinely banned cookie.
 *
 * Nothing in the running system can detect that on its own: "the probe failed"
 * is the observation, and it has two causes. The only moment the ambiguity can
 * be broken is at boot, against a cookie an operator has just installed.
 *
 * WHY THIS IS LOUD RATHER THAN FATAL -- a deliberate deviation from the risk
 * register's "fail fast", recorded in phase-07 §Outcome. A fatal check makes the
 * whole service refuse to boot whenever Douyin is unreachable, which takes down
 * job creation and browsing -- neither of which touches the probe -- for a
 * transient network fault. The failure mode it prevents (a misdirected operator)
 * is strictly less severe than the one it would introduce (no service at all).
 * So it reports, at error level, naming the consequence.
 */
import { ParserRejectedError, ParserUnavailableError } from '../../src/parser/parser.errors';
import { ProbeHealthCheck } from '../../src/parser/probe-health';
import type { ProbeSource } from '../../src/parser/f4-probe';
import type { UserProfile } from '../../src/parser/parser.client';

const PROBE_ID = 'MS4wLjABAAAA_known_public';

function recorder() {
  const lines: { level: string; message: string }[] = [];
  return {
    lines,
    logger: {
      log: (message: string) => lines.push({ level: 'log', message }),
      warn: (message: string) => lines.push({ level: 'warn', message }),
      error: (message: string) => lines.push({ level: 'error', message }),
    },
    at: (level: string) => lines.filter((l) => l.level === level).map((l) => l.message),
  };
}

function source(outcome: () => Promise<UserProfile>): ProbeSource & { calls: number } {
  return {
    calls: 0,
    async fetchUserProfile(): Promise<UserProfile> {
      this.calls += 1;
      return outcome();
    },
  };
}

describe('the boot-time probe health check', () => {
  it('says OK when the probe target answers, so a later `failed` verdict is trustworthy', async () => {
    const rec = recorder();
    const parser = source(() => Promise.resolve({ nickname: 'public', statusCode: 0 }));

    const check = new ProbeHealthCheck(rec.logger, parser, { probeSecUserId: PROBE_ID });
    check.onApplicationBootstrap();
    await check.done;

    expect(check.healthy).toBe(true);
    expect(rec.at('error')).toHaveLength(0);
    expect(rec.at('log').join('\n')).toMatch(/probe target is reachable/i);
  });

  it('reports at ERROR level, naming the consequence, when the probe target is unusable', async () => {
    // The consequence has to be IN THE LINE. "probe failed" alone reads as a
    // transient blip; what an operator needs to know is that F4 is degraded and
    // every diagnosis from now on will say `blocked`.
    const rec = recorder();
    const parser = source(() =>
      Promise.reject(new ParserRejectedError(400, 'parser refused with HTTP 400')),
    );

    const check = new ProbeHealthCheck(rec.logger, parser, { probeSecUserId: PROBE_ID });
    check.onApplicationBootstrap();
    await check.done;

    expect(check.healthy).toBe(false);
    const errors = rec.at('error').join('\n');
    expect(errors).toMatch(/blocked/);
    expect(errors).toMatch(/PROBE_SEC_USER_ID/);
  });

  it('does NOT throw, so an unreachable Douyin cannot stop the API from booting', async () => {
    // The deliberate deviation, asserted rather than described: a check that
    // threw here would make `docker compose up` fail on a network blip and take
    // job creation and browsing down with it.
    const rec = recorder();
    const parser = source(() => Promise.reject(new ParserUnavailableError('no answer')));

    const check = new ProbeHealthCheck(rec.logger, parser, { probeSecUserId: PROBE_ID });

    expect(check.onApplicationBootstrap()).toBeUndefined();
    await expect(check.done).resolves.toBeUndefined();
    expect(check.healthy).toBe(false);
  });

  it('does not hold BOOTSTRAP open while the probe is in flight', async () => {
    // Nest awaits `onApplicationBootstrap`, so awaiting the probe there would
    // delay `app.init()` -- and therefore `/health`, and therefore compose's
    // `depends_on: service_healthy` for the web container -- by the parser's
    // whole 30s timeout whenever the parser is down. The check was made
    // non-fatal precisely so an unreachable Douyin could not stop the service
    // from serving; blocking boot on it gives most of that harm back.
    //
    // Measured by having the probe NEVER settle: the hook must still return,
    // and `healthy` must still be `null` afterwards because nothing is known yet.
    const rec = recorder();
    let release: (() => void) | undefined;
    const parser = source(
      () =>
        new Promise<UserProfile>((resolve) => {
          release = () => resolve({ nickname: 'public', statusCode: 0 });
        }),
    );

    const check = new ProbeHealthCheck(rec.logger, parser, { probeSecUserId: PROBE_ID });
    const returned = check.onApplicationBootstrap();

    // THE assertion, and it is the return VALUE rather than a timing
    // observation on purpose. Nest awaits whatever this hook hands back, so
    // "does not block bootstrap" is exactly "hands back nothing to await". An
    // earlier version measured it by checking `healthy` was still null, which a
    // hook that awaited a never-settling probe satisfies just as well -- it
    // simply hangs instead of failing, and a hang names nothing (plan §5 rule 6).
    expect(returned).toBeUndefined();
    // The probe DID start, so this is not "nothing happened".
    expect(check.healthy).toBeNull();
    expect(parser.calls).toBe(1);
    release!();
    await check.done;
    expect(check.healthy).toBe(true);
  });

  it('treats a Douyin refusal as unhealthy -- a 200 is not the same as an answer', async () => {
    // The shape a naive check misses: HTTP 200 all the way, envelope intact,
    // only `status_code` says no. A check that keyed on "did it throw?" would
    // report a banned cookie as a healthy probe target.
    const rec = recorder();
    const parser = source(() => Promise.resolve({ nickname: null, statusCode: 2190 }));

    const check = new ProbeHealthCheck(rec.logger, parser, { probeSecUserId: PROBE_ID });
    check.onApplicationBootstrap();
    await check.done;

    expect(check.healthy).toBe(false);
    expect(rec.at('error').join('\n')).toMatch(/2190/);
  });

  it('swallows a non-parser error too, and still says the check did not pass', async () => {
    // `probeCookie` deliberately rethrows anything that is not a ParserError,
    // because a TypeError is a bug on this side rather than a diagnosis. At
    // boot that must still not be fatal -- but it must not read as healthy
    // either.
    const rec = recorder();
    const parser = source(() => Promise.reject(new TypeError('bad base url')));

    const check = new ProbeHealthCheck(rec.logger, parser, { probeSecUserId: PROBE_ID });
    check.onApplicationBootstrap();
    await check.done;

    expect(check.healthy).toBe(false);
    expect(rec.at('error').join('\n')).toMatch(/bad base url/);
  });

  it('costs exactly one request at boot', async () => {
    const rec = recorder();
    const parser = source(() => Promise.resolve({ nickname: 'public', statusCode: 0 }));

    const check = new ProbeHealthCheck(rec.logger, parser, { probeSecUserId: PROBE_ID });
    check.onApplicationBootstrap();
    await check.done;

    expect(parser.calls).toBe(1);
  });

  it('never rejects, so a broken logger cannot crash the process', async () => {
    // Nothing awaits `done` in production, so a rejection here is an unhandled
    // rejection -- a process crash caused by a check that was deliberately made
    // non-fatal. `run()` already swallows every probe failure, so the remaining
    // way through is the logger itself, which is exactly the "last line of
    // defence must not break" finding phase 4's review recorded.
    const exploding = {
      log: () => {
        throw new Error('logger is broken');
      },
      warn: () => {},
      error: () => {
        throw new Error('logger is broken');
      },
    };
    const parser = source(() => Promise.resolve({ nickname: 'public', statusCode: 0 }));

    const check = new ProbeHealthCheck(exploding, parser, { probeSecUserId: PROBE_ID });
    check.onApplicationBootstrap();

    await expect(check.done).resolves.toBeUndefined();
  });

  it('does nothing at all when this process runs no lanes', async () => {
    // An API-only node never diagnoses anything, so the request would buy
    // nothing -- and on a fleet of API nodes it would be one probe per node per
    // deploy against an IP-based rate limit.
    const rec = recorder();
    const parser = source(() => Promise.resolve({ nickname: 'public', statusCode: 0 }));

    const check = new ProbeHealthCheck(rec.logger, parser, {
      probeSecUserId: PROBE_ID,
      enabled: false,
    });
    check.onApplicationBootstrap();
    await check.done;

    expect(parser.calls).toBe(0);
    expect(check.healthy).toBeNull();
  });
});
