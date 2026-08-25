/**
 * F4 -- THE PROBE, at the level where its four causes are actually
 * distinguished.
 *
 * plan.md §5 rule 9, applied to exactly this file: "a test double that cannot
 * fail the way the real thing fails means a green suite says nothing about that
 * case". For the probe that rule is unusually sharp, because the probe's entire
 * job is to tell four causes apart:
 *
 *   | real cause                        | how it reaches us                  |
 *   |-----------------------------------|------------------------------------|
 *   | cookie banned                     | HTTP 200, Douyin `status_code` != 0|
 *   | `a_bogus` stale / crawler bug      | HTTP 400 from the fork             |
 *   | parser cannot reach Douyin        | no answer at all                   |
 *   | target profile private / deleted   | the probe SUCCEEDS                 |
 *
 * So the fake below can produce all four, plus the fifth shape that must NOT be
 * read as either: a 200 whose body carries no `status_code`. If any one of those
 * five could not be constructed here, the probe's tests would be measuring an
 * easier world than the one it runs in -- which is how this repo has shipped
 * false greens nine times (plan §5).
 *
 * The one thing this file does NOT test is what the LANE does with a verdict;
 * that is test/lanes/f4-probe.spec.ts, because "which status is written" is a
 * property of the lane and would be a tautology here.
 */
import { ParserRejectedError, ParserUnavailableError } from '../../src/parser/parser.errors';
import { probeCookie, type ProbeSource } from '../../src/parser/f4-probe';
import type { UserProfile } from '../../src/parser/parser.client';

const PROBE_ID = 'MS4wLjABAAAA_known_public';

/**
 * A probe target that can fail every way the real one can.
 *
 * Constructed per test from one of the five outcomes above -- never a single
 * "sometimes throws" stub, because a stub that can only produce one failure
 * shape is the exact instrument rule 9 is about.
 */
function source(outcome: () => Promise<UserProfile>): ProbeSource & { calls: string[] } {
  const calls: string[] = [];
  return {
    calls,
    async fetchUserProfile(secUserId: string): Promise<UserProfile> {
      calls.push(secUserId);
      return outcome();
    },
  };
}

const healthy = (): Promise<UserProfile> =>
  Promise.resolve({ nickname: 'a known public account', statusCode: 0 });

/** Douyin's own refusal. HTTP 200 all the way down; only `status_code` says no. */
const douyinRefused = (): Promise<UserProfile> =>
  Promise.resolve({ nickname: null, statusCode: 2190 });

/** A 200 envelope with no Douyin verdict in it at all. */
const noStatusCode = (): Promise<UserProfile> =>
  Promise.resolve({ nickname: null, statusCode: null });

const forkThrew = (): Promise<UserProfile> =>
  Promise.reject(new ParserRejectedError(400, 'parser refused GET handler_user_profile with HTTP 400'));

const routeMismatch = (): Promise<UserProfile> =>
  Promise.reject(new ParserRejectedError(422, 'parser refused GET handler_user_profile with HTTP 422'));

const noAnswer = (): Promise<UserProfile> =>
  Promise.reject(new ParserUnavailableError('parser did not answer within 30000ms'));

describe('the F4 probe reads the cookie, not the profile that failed', () => {
  it('a successful probe means the cookie is fine, so the fault is THIS profile', async () => {
    const parser = source(healthy);

    const result = await probeCookie(parser, PROBE_ID);

    expect(result.verdict).toBe('cookie-healthy');
    // The half the lane acts on: NOT global, so the job is `failed` and nobody
    // goes to rotate a working cookie.
    expect(result.global).toBe(false);
    expect(parser.calls).toEqual([PROBE_ID]);
  });

  it('a Douyin-level refusal on a KNOWN-PUBLIC profile means the cookie is banned', async () => {
    // The shape that is easiest to miss: HTTP 200 the whole way, envelope
    // intact, `nickname` merely absent. Only `status_code` says no. If
    // `ParserClient` ever stopped carrying that field this would read as a
    // healthy probe and every banned cookie would be reported as a private
    // profile -- F4's mistake, in reverse.
    const result = await probeCookie(source(douyinRefused), PROBE_ID);

    expect(result.verdict).toBe('cookie-refused');
    expect(result.global).toBe(true);
    expect(result.reason).toMatch(/2190/);
  });

  it('an HTTP 400 on a known-public profile is the parser itself, not the profile', async () => {
    const result = await probeCookie(source(forkThrew), PROBE_ID);

    expect(result.verdict).toBe('parser-broken');
    expect(result.global).toBe(true);
    // The remediation is not "rotate the cookie": a fork that cannot sign a
    // request for a profile anyone can read is a signing problem (`a_bogus`)
    // or a crawler bug, and the fix is upstream.
    expect(result.reason).toMatch(/a_bogus/);
  });

  it('a non-400 refusal is named as a contract mismatch, not as a cookie problem', async () => {
    // 404/422 means we and the fork disagree about the route or its parameters.
    // Reporting that as a banned cookie sends an operator to rotate a cookie
    // over a bug on this side.
    const result = await probeCookie(source(routeMismatch), PROBE_ID);

    expect(result.verdict).toBe('parser-broken');
    expect(result.global).toBe(true);
    expect(result.reason).toMatch(/422/);
  });

  it('no answer at all is the network, and says to wait rather than to change anything', async () => {
    const result = await probeCookie(source(noAnswer), PROBE_ID);

    expect(result.verdict).toBe('parser-unreachable');
    expect(result.global).toBe(true);
    expect(result.reason).toMatch(/reach/i);
  });

  it('a 200 with NO status_code is inconclusive -- never read as healthy', async () => {
    // parser.client.ts refuses to default a missing `status_code` to 0
    // ("inventing success that was never observed is the same mistake as
    // inventing a cause for a 400"). This is the consequence of that refusal:
    // an unreadable probe must not license the `failed` verdict, because
    // `failed` is the one that writes a profile off.
    const result = await probeCookie(source(noStatusCode), PROBE_ID);

    expect(result.verdict).toBe('inconclusive');
    expect(result.global).toBe(true);
    expect(result.reason).toMatch(/could not/i);
  });

  it('costs exactly one request, whatever the outcome', async () => {
    // The probe runs on the error path precisely so it does not touch the F2
    // budget on the happy path -- a probe that retried would put that back.
    for (const outcome of [healthy, douyinRefused, forkThrew, noAnswer, noStatusCode]) {
      const parser = source(outcome);
      await probeCookie(parser, PROBE_ID);
      expect(parser.calls).toHaveLength(1);
    }
  });

  it('lets a non-parser error out rather than reporting a verdict it did not measure', async () => {
    // A programming error (a TypeError from a bad base URL, say) is not a
    // diagnosis. Swallowing it into `blocked` would hide a bug on this side
    // behind a story about Douyin.
    const boom = (): Promise<UserProfile> => Promise.reject(new TypeError('not a parser error'));

    await expect(probeCookie(source(boom), PROBE_ID)).rejects.toThrow(TypeError);
  });
});
