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
