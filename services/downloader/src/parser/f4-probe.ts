import { ParserRejectedError, ParserUnavailableError } from './parser.errors';
import type { UserProfile } from './parser.client';

/**
 * F4 -- THE ONE OBSERVATION A FAILING CALL CANNOT SUPPLY.
 *
 * `parser.errors.ts` explains why an HTTP 400 from the fork carries exactly zero
 * diagnostic information: every route catches `Exception`, binds `e`, and then
 * never uses it, so `ErrorResponseModel.message` is the pydantic class default
 * `"An error occurred."` for a banned cookie, a private profile, a deleted
 * account, a stale `a_bogus`, and a genuine crawler bug alike. Plan §0 principle
 * 2 rules out the obvious fix (patch the fork to propagate `str(e)`) because the
 * patch budget is what keeps the fork mergeable when Douyin changes `a_bogus`.
 *
 * So the cause is recovered from OUTSIDE the failing call: ask the same parser,
 * with the same cookie, for a profile that is KNOWN to be public
 * (`PROBE_SEC_USER_ID`). Whatever happens to that request is about the cookie,
 * the signing, or the network -- it cannot be about the profile that failed.
 *
 * WHY THIS MATTERS MORE THAN IT LOOKS. IDEA.md §6.2 says "3 consecutive errors
 * -> blocked", full stop. Implemented literally, a private profile is reported
 * as `blocked`, and `blocked` is the word that sends an operator to rotate a
 * cookie. They rotate a perfectly good cookie, the job fails again, and the
 * actual cause -- a profile nobody can read -- is never in front of them. The
 * cost of getting this wrong is not a crashed process; it is a person doing the
 * wrong thing with confidence. Plan §7 records the deviation from §6.2.
 *
 * THE PROBE RUNS ONLY ON THE ERROR PATH. It is the third of three signal tiers
 * (phase-07 §7.1): the structural signal (empty `aweme_list` + `has_more`, phase
 * 5) is an HTTP 200 and certain, so it needs no probe; the probe is reached only
 * after `MAX_CONSECUTIVE_REJECTIONS` HTTP 400s, which is why it costs nothing
 * against the F2 request budget in normal operation.
 */

/**
 * Just enough of `ParserClient` to run a probe.
 *
 * Structural, like `PostPageSource` and `VideoUrlSource`, and for the same
 * reason -- but here the reason is sharper than testability. plan §5 rule 9: a
 * double that cannot fail the way the real thing fails makes a green suite
 * silent about that case. A probe has FIVE distinguishable outcomes, so the
 * seam has to be narrow enough that a test can drive all five. See
 * test/parser/f4-probe.spec.ts.
 */
export interface ProbeSource {
  fetchUserProfile(secUserId: string): Promise<UserProfile>;
}

/**
 * What the probe learned. Five values, because there are five distinguishable
 * things a probe of a known-public profile can tell us -- and collapsing any two
 * of them loses a remediation.
 */
export type ProbeVerdict =
  /** The probe worked. The cookie is good, so the fault is the target profile. */
  | 'cookie-healthy'
  /** HTTP 200, and Douyin itself said no. The cookie is being refused. */
  | 'cookie-refused'
  /** The fork answered with a refusal on a profile anybody can read. */
  | 'parser-broken'
  /** No verdict was reached at all -- the parser or the network is down. */
  | 'parser-unreachable'
  /** A 200 that carried no Douyin verdict. Unreadable, so nothing is claimed. */
  | 'inconclusive';

export interface ProbeResult {
  verdict: ProbeVerdict;
  /**
   * Whether the fault is GLOBAL (this cookie / this parser / this network) as
   * opposed to specific to the profile that failed.
   *
   * This is the single bit the lane acts on, and it is derived here rather than
   * in the lane on purpose: the mapping from cause to remediation is the whole
   * substance of F4, and a second copy of it next to the `markBlocked` call
   * would be the copy that drifts. `false` for exactly one verdict --
   * `cookie-healthy` -- because that is the only one that has RULED OUT the
   * global explanations. In particular `inconclusive` is global: a probe that
   * could not be read has ruled nothing out, and `failed` writes a profile off.
   */
  global: boolean;
  /** Operator-facing text. Names the remediation, never a guess dressed as one. */
  reason: string;
}

/**
 * Runs the probe once and says what it means.
 *
 * ONE request, no retries, whatever the outcome -- the lane already spent three
 * requests reaching here, and a probe that retried would be spending the F2
 * budget on the error path it was designed to stay off.
 *
 * Anything that is not a `ParserError` is rethrown rather than folded into a
 * verdict. A `TypeError` out of this call is a bug on this side, and reporting
 * it as "Douyin refused the cookie" would bury it under a plausible story --
 * the same failure mode F4 itself is about.
 */
export async function probeCookie(
  parser: ProbeSource,
  probeSecUserId: string,
): Promise<ProbeResult> {
  let profile: UserProfile;
  try {
    profile = await parser.fetchUserProfile(probeSecUserId);
  } catch (err: unknown) {
    if (err instanceof ParserUnavailableError) {
      return {
        verdict: 'parser-unreachable',
        global: true,
        reason:
          'the parser could not be reached at all while probing a known-public profile, so no ' +
          'Douyin-level verdict exists. Nothing here is about this profile and nothing needs ' +
          'changing: check that the parser container and its network are up, then /retry.',
      };
    }
    if (err instanceof ParserRejectedError) {
      return {
        verdict: 'parser-broken',
        global: true,
        reason:
          `the parser refused a probe of a known-public profile with HTTP ${err.status}. A ` +
          'profile anybody can read failing means the fault is in the parser rather than in ' +
          'the profile: most likely a stale `a_bogus` signature (pull upstream and rebuild the ' +
          'fork) or a crawler bug. Rotating the cookie will not help.',
      };
    }
    throw err;
  }

  // HTTP 200 from the fork does NOT mean Douyin agreed. The fork stamps its own
  // `code: 200` envelope on whatever Douyin sent, so a refusal arrives looking
  // like a completely healthy exchange and `status_code` is the only place it
  // shows. `parser.client.ts` carries the field through unchanged for exactly
  // this line.
  if (profile.statusCode === null) {
    return {
      verdict: 'inconclusive',
      global: true,
      reason:
        'the probe of a known-public profile answered without a readable Douyin status_code, so ' +
        'it could not confirm the cookie either way. Treated as a global fault rather than as a ' +
        'problem with this profile, because the alternative would write a profile off on the ' +
        'strength of an unreadable answer.',
    };
  }

  if (profile.statusCode !== 0) {
    return {
      verdict: 'cookie-refused',
      global: true,
      reason:
        `Douyin refused a probe of a known-public profile with status_code ${profile.statusCode}, ` +
        'which means the cookie is being refused rather than that this profile is unreachable. ' +
        'Push a fresh cookie and /retry; the checkpoint is kept.',
    };
  }

  return {
    verdict: 'cookie-healthy',
    global: false,
    reason:
      'a probe of a known-public profile succeeded with the same cookie, so the cookie is still ' +
      'good and the fault is this profile specifically -- private, deleted, or otherwise not ' +
      'accessible. Do NOT rotate the cookie for this.',
  };
}
