import type { OnApplicationBootstrap } from '@nestjs/common';
import type { LaneLogger } from '../lanes/lane.runner';
import { probeCookie, type ProbeSource } from './f4-probe';

export interface ProbeHealthOptions {
  probeSecUserId: string;
  /** False on an API-only node. Defaults to true. */
  enabled?: boolean;
}

/**
 * Checks, once at boot, that the F4 probe target is usable.
 *
 * WHY THIS EXISTS -- phase-07 Risks row 1, and it is F4 turned inside out.
 *
 * The probe's `failed` verdict requires POSITIVE evidence: it is written only
 * when a probe of `PROBE_SEC_USER_ID` succeeds. So a probe target that has
 * itself gone private, been deleted, or been renamed can never produce that
 * evidence. Every 3x400 then resolves to `blocked`, every operator is sent to
 * rotate a cookie that is fine, and the system is indistinguishable from one
 * with a genuinely banned cookie. That is exactly the outcome F4 was designed to
 * prevent, reached through the mechanism that prevents it.
 *
 * Nothing at runtime can break the ambiguity: "the probe failed" has two causes
 * and the failing observation is the same. The one moment it CAN be broken is
 * boot, right after an operator has installed a cookie they believe in.
 *
 * DELIBERATE DEVIATION, recorded in phase-07 §Outcome: the risk register says
 * "fail fast", and this does not. A fatal check refuses to boot the whole
 * service whenever Douyin or the parser is momentarily unreachable -- taking
 * down job creation, browsing, and the cookie endpoint, none of which involve
 * the probe -- to prevent a misdirected operator. The prevented harm is
 * strictly smaller than the introduced harm, so this reports at error level
 * instead, naming the consequence rather than just the symptom, and records the
 * verdict on `healthy` so a later readiness endpoint (phase 10) can surface it
 * without repeating the request.
 *
 * ONE request, at boot, only where lanes run. An API-only node never diagnoses
 * anything, and on a fleet of them this would be one probe per node per deploy
 * against an IP-based rate limit.
 */
export class ProbeHealthCheck implements OnApplicationBootstrap {
  /**
   * `true` once a probe has succeeded, `false` once one has failed, `null` while
   * the check has not run (including on a node with lanes disabled).
   *
   * Three states rather than a boolean, because "not checked" must not read as
   * "unhealthy": a phase-10 readiness probe keying on `=== false` would
   * otherwise mark every API-only node unready forever.
   */
  healthy: boolean | null = null;

  /**
   * Resolves when the check has finished. Always resolves, never rejects.
   *
   * Exposed because `onApplicationBootstrap` deliberately does NOT await the
   * probe (see there), which would otherwise make the check unobservable from a
   * test without a sleep -- and a test that sleeps is a test that is sometimes
   * measuring nothing.
   */
  done: Promise<void> = Promise.resolve();

  private readonly enabled: boolean;

  /**
   * Constructed by a factory in `lanes.module.ts` rather than by Nest's class
   * resolution, so there are no `@Inject` decorators here. That is not
   * incidental: the factory is where `enabled` is derived from `LANES_ENABLED`
   * and `probeSecUserId` from `PROBE_SEC_USER_ID`, and decorators claiming a
   * different wiring would be documentation that is false.
   */
  constructor(
    private readonly logger: LaneLogger,
    private readonly parser: ProbeSource,
    private readonly opts: ProbeHealthOptions,
  ) {
    this.enabled = opts.enabled ?? true;
  }

  /**
   * Starts the check and RETURNS IMMEDIATELY. Nest awaits this hook, so awaiting
   * the probe here would hold `app.init()` open for the parser's whole timeout
   * (30s) whenever the parser is down -- delaying `/health`, and with it
   * compose's `depends_on: service_healthy` for the web container. This check was
   * made non-fatal precisely so an unreachable Douyin could not stop the service
   * from serving; blocking boot on it would give most of that harm back.
   *
   * Nothing waits on the result at runtime: it is a log line and a field. So
   * fire-and-forget is not a shortcut here, it is the correct coupling.
   */
  onApplicationBootstrap(): void {
    if (!this.enabled) return;
    // `.catch`, and it is not defensive noise. NOTHING awaits `done` in
    // production -- it exists so a test can be deterministic -- so any rejection
    // out of `run()` becomes an UNHANDLED REJECTION, which in Node is a process
    // crash. That would mean a diagnostic explicitly designed to be non-fatal
    // taking the whole service down, which is worse than the failure it reports.
    //
    // `run()` already catches every probe failure, so the only way through here
    // is the logger itself throwing -- and phase 4's review recorded the same
    // shape: the outermost catch is not allowed to be the thing that breaks.
    this.done = this.run().catch(() => undefined);
  }

  private async run(): Promise<void> {
    const probeSecUserId = this.opts.probeSecUserId;

    let describeFailure: string;
    try {
      const result = await probeCookie(this.parser, probeSecUserId);
      if (!result.global) {
        this.healthy = true;
        // Named as "reachable", not "the cookie is good": a successful probe
        // says both, but the thing this check is FOR is the target, and a log
        // line claiming more than it measured is how a comment starts lying.
        this.logger.log(
          'F4 probe target is reachable with the current cookie, so a `failed` diagnosis is ' +
            'able to be reached: a later 3x400 that resolves to `blocked` really means the ' +
            'cookie, the signing, or the network.',
        );
        return;
      }
      describeFailure = result.reason;
    } catch (err: unknown) {
      // `probeCookie` rethrows anything that is not a ParserError, because a
      // TypeError is a bug on this side rather than a diagnosis. At boot it must
      // still not be fatal -- but it must not be recorded as healthy either.
      describeFailure = err instanceof Error ? err.message : String(err);
    }

    this.healthy = false;
    this.logger.error(
      'F4 PROBE TARGET IS NOT USABLE. Until this succeeds, every job that hits three ' +
        'consecutive HTTP 400s will be reported as `blocked` -- including profiles that are ' +
        'merely private -- because the `failed` verdict requires a SUCCESSFUL probe as its ' +
        'evidence. Operators will be sent to rotate a cookie that may be perfectly good. ' +
        `Check PROBE_SEC_USER_ID still names a public, live profile. Probe said: ${describeFailure}`,
    );
  }
}
