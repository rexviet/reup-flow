/**
 * Child process for `test/lanes/sigterm-shutdown.e2e-spec.ts`.
 *
 * Boots the REAL `AppModule` graph with exactly one provider swapped -- the
 * `LANES` array, replaced by a single lane that claims a video and holds it --
 * then prints a line and waits for a signal.
 *
 * Why a real child process rather than an in-process test. Everything about
 * §4.2's shutdown is already covered in-process by
 * `test/lanes/lane.supervisor.spec.ts` and
 * `test/lanes/lease-release.spec.ts`, and all of it would keep passing with
 * `app.enableShutdownHooks()` deleted: those tests call `app.close()` (or the
 * hook) directly. Nothing but a real signal at a real process can tell whether
 * Nest is LISTENING for one. Without that call `SIGTERM` kills the process
 * outright, no lane stops, and every held lease survives for the full
 * `LEASE_SECONDS` -- the exact symptom §4.2's DoD forbids, and the one that
 * would have shipped.
 *
 * `configureApp` is imported from `src/bootstrap.ts` rather than re-calling
 * `enableShutdownHooks()` here. Re-implementing it would make this harness
 * pass forever after somebody deleted the production call -- the drift
 * `createApp`'s own comment warns about.
 */
import { Test } from '@nestjs/testing';
import { AppModule } from '../../src/app.module';
import { configureApp } from '../../src/bootstrap';
import { DB } from '../../src/db/db.module';
import type { Executor } from '../../src/db/executor';
import { LaneRunner, type LaneLogger, type TickOutcome } from '../../src/lanes/lane.runner';
import { LANES } from '../../src/lanes/lanes.tokens';
import * as videosRepository from '../../src/db/videos.repository';

const LEASE_SECONDS = Number(process.env.CHILD_LEASE_SECONDS ?? '300');
const MAX_ATTEMPTS = Number(process.env.CHILD_MAX_ATTEMPTS ?? '3');

/** Written to stdout once the lane is holding a claim; the parent waits for it. */
const CLAIMED_MARKER = 'CHILD_CLAIMED';

const stderrLogger: LaneLogger = {
  // stdout is the parent's control channel, so lane logging goes to stderr.
  error: (m) => process.stderr.write(`${m}\n`),
  warn: (m) => process.stderr.write(`${m}\n`),
  log: (m) => process.stderr.write(`${m}\n`),
};

/**
 * Claims one video, registers it as held work, and then does nothing else.
 *
 * The release callback is `videosRepository.release` -- the PRODUCTION
 * function, not a stand-in. What is under test is the base class's shutdown
 * sequence and Nest's signal wiring; the give-back itself has to be the real
 * one, or the parent's assertion about the row would be measuring a fake.
 */
class HoldingLane extends LaneRunner {
  readonly name = 'holding';
  // Long enough that the lane cannot possibly tick again before the signal
  // arrives: one claim, then quiet.
  readonly idleDelayMs = 60_000;

  constructor(
    logger: LaneLogger,
    private readonly db: Executor,
  ) {
    super(logger);
  }

  async runOnce(): Promise<TickOutcome> {
    const claimed = await videosRepository.claimOne(this.db, {
      leaseSeconds: LEASE_SECONDS,
      maxAttempts: MAX_ATTEMPTS,
    });
    if (!claimed) return 'idle';
    this.hold({
      describe: `video ${claimed.awemeId}`,
      release: () =>
        videosRepository.release(this.db, claimed.awemeId, { maxAttempts: MAX_ATTEMPTS }),
    });
    process.stdout.write(`${CLAIMED_MARKER} ${claimed.awemeId}\n`);
    return 'idle';
  }
}

async function main(): Promise<void> {
  const moduleRef = await Test.createTestingModule({ imports: [AppModule] })
    .overrideProvider(LANES)
    .useFactory({
      inject: [DB],
      factory: (db: Executor) => [new HoldingLane(stderrLogger, db)],
    })
    .compile();

  const app = moduleRef.createNestApplication();
  // The production configuration, from the production module. See the file
  // comment for why this is imported and not re-implemented.
  configureApp(app);
  await app.init();
  process.stdout.write('CHILD_READY\n');
}

main().catch((err: unknown) => {
  process.stderr.write(`child failed: ${String(err)}\n`);
  process.exit(1);
});
