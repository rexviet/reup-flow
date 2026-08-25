/**
 * FIX 11 (review): `cancel()` and `retry()` used to call
 * `this.jobsService.findById` TWICE on the 404/409 path -- once inside
 * `assertJobExistsOr404` and again immediately after to build the 409
 * message -- and then used the second result with a non-null assertion
 * (`existing!.status`). A row that vanishes between those two reads (a
 * concurrent operation racing the same job, however unlikely) makes the
 * second `findById` return `null`, and `existing!.status` throws a
 * `TypeError` at runtime, which Nest turns into an unhandled 500 -- looking
 * like a bug in this service when the actual story is "the job finished
 * disappearing a few milliseconds earlier".
 *
 * A pure unit test (no HTTP, no database) is what makes this race
 * reproducible on demand: the fake `JobsService` below returns a job on the
 * FIRST `findById` call and `null` on every call after, deterministically
 * simulating the vanish-between-reads window that would otherwise need
 * exact timing against a real database to hit.
 */
import { ConflictException, NotFoundException, ServiceUnavailableException } from '@nestjs/common';
import { JobsController } from '../../src/jobs/jobs.controller';
import type { JobsService } from '../../src/jobs/jobs.service';
import type { Job } from '../../src/db/schema';
import { ParserUnavailableError } from '../../src/parser/parser.errors';

function fakeJob(overrides: Partial<Job> = {}): Job {
  return {
    id: 'job-1',
    secUserId: 'sec-1',
    status: 'pending',
    totalVideos: null,
    enumeratedCount: 0,
    downloadedCount: 0,
    failedCount: 0,
    maxCursor: 0,
    leaseExpiresAt: null,
    error: null,
    createdAt: new Date('2026-08-24T00:00:00Z'),
    updatedAt: new Date('2026-08-24T00:00:00Z'),
    ...overrides,
  };
}

/**
 * Returns a job on the Nth call and beyond `vanishesAfterCall` calls,
 * returns null instead -- simulating the row disappearing partway through a
 * handler that reads it more than once.
 */
function findByIdThatVanishes(vanishesAfterCall: number): {
  findById: jest.Mock<Promise<Job | null>, [string]>;
} {
  let calls = 0;
  const findById = jest.fn(async (_id: string): Promise<Job | null> => {
    calls++;
    return calls <= vanishesAfterCall ? fakeJob({ status: 'completed' }) : null;
  });
  return { findById };
}

describe('JobsController cancel()/retry(): a row vanishing between reads must not 500', () => {
  it('cancel(): does not throw a TypeError when the job disappears after the first lookup', async () => {
    const { findById } = findByIdThatVanishes(1);
    const service = {
      cancel: async () => null, // "not cancellable" or "not found" -- ambiguous by design
      findById,
    } as unknown as JobsService;
    const controller = new JobsController(service);

    // The old two-fetch implementation crashes here with a raw TypeError
    // ("Cannot read properties of null (reading 'status')"), which Nest
    // reports as an unhandled 500 rather than either domain exception below.
    await expect(controller.cancel('job-1')).rejects.not.toThrow(TypeError);
  });

  it('cancel(): calls findById at most once per request (reuses the fetch instead of re-reading)', async () => {
    const { findById } = findByIdThatVanishes(5); // job stays present the whole time
    const service = {
      cancel: async () => null,
      findById,
    } as unknown as JobsService;
    const controller = new JobsController(service);

    await expect(controller.cancel('job-1')).rejects.toBeInstanceOf(ConflictException);
    expect(findById).toHaveBeenCalledTimes(1);
  });

  it('retry(): does not throw a TypeError when the job disappears after the first lookup', async () => {
    const { findById } = findByIdThatVanishes(1);
    const service = {
      retry: async () => null,
      findById,
    } as unknown as JobsService;
    const controller = new JobsController(service);

    await expect(controller.retry('job-1')).rejects.not.toThrow(TypeError);
  });

  it('retry(): calls findById at most once per request (reuses the fetch instead of re-reading)', async () => {
    const { findById } = findByIdThatVanishes(5);
    const service = {
      retry: async () => null,
      findById,
    } as unknown as JobsService;
    const controller = new JobsController(service);

    await expect(controller.retry('job-1')).rejects.toBeInstanceOf(ConflictException);
    expect(findById).toHaveBeenCalledTimes(1);
  });

  it('cancel(): a job that is truly gone by the single lookup reports 404, not a crash', async () => {
    const service = {
      cancel: async () => null,
      findById: async () => null,
    } as unknown as JobsService;
    const controller = new JobsController(service);

    await expect(controller.cancel('job-1')).rejects.toBeInstanceOf(NotFoundException);
  });
});

/**
 * FIX 11 (review): `ParserClient.get`'s connection-failure message embeds
 * `this.options.baseUrl` (parser.client.ts's `did not answer GET ... at
 * ${this.options.baseUrl} ...`) -- internal compose-network topology, not
 * something an API client needs or should see. `mapParserError` used to
 * hand that message straight through as the 503 response body. The detail
 * is still worth keeping SOMEWHERE (an operator debugging connectivity
 * wants it), just not in the HTTP response -- so it must still be
 * reachable from the thrown error (e.g. via a `cause`/logged separately),
 * while the exception's own `message` -- what actually serialises into the
 * response body -- must not contain it.
 */
describe('JobsController POST /api/jobs: parser-unavailable 503 must not disclose internal topology', () => {
  it('does not include the parser base URL in the 503 exception message', async () => {
    const internalDetail =
      'parser did not answer GET /api/douyin/web/get_sec_user_id at http://parser:80 within 30000ms, or the address could not be used at all';
    const service = {
      createFromUrl: async () => {
        throw new ParserUnavailableError(internalDetail);
      },
    } as unknown as JobsService;
    const controller = new JobsController(service);

    let caught: unknown;
    try {
      await controller.create({ url: 'https://www.douyin.com/user/x' });
    } catch (err) {
      caught = err;
    }

    expect(caught).toBeInstanceOf(ServiceUnavailableException);
    expect((caught as ServiceUnavailableException).message).not.toContain('http://parser:80');
  });
});
