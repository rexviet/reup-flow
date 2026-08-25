/**
 * Injection tokens for the lane layer, in a file of their own.
 *
 * Separate from `lanes.module.ts` on purpose: `lane.supervisor.ts` needs the
 * tokens and the module needs the supervisor, so putting the tokens in the
 * module file would make the two import each other. A tokens-only module has
 * no imports and so cannot participate in a cycle.
 */

/** The array of `LaneRunner`s this process should run. */
export const LANES = 'LANES';

/** The `LaneLogger` every lane and the supervisor share. */
export const LANE_LOGGER = 'LANE_LOGGER';

/**
 * The `DOWNLOAD_CONCURRENCY` download lanes, as their own provider.
 *
 * Separate from `LANES` so the count has ONE place it is decided (a factory
 * that reads the env var) rather than being spread through the array literal
 * that assembles every lane. §6.1's "concurrency = number of loops" then reads
 * as a single `Array.from`, and `test/lanes/lanes.module.spec.ts` can assert the
 * number without also asserting the composition of everything else.
 */
export const DOWNLOAD_LANES = 'DOWNLOAD_LANES';
