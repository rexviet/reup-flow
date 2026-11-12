import { and, eq, inArray, sql } from 'drizzle-orm';
import type { Executor } from '../db/executor';
import { dashboardActions, localizationOutbox, profiles, videos } from '../db/schema';

export type LocalizeSkipReason = 'no_outbox_row' | 'not_held';

export interface LocalizeSkip {
  aweme_id: string;
  reason: LocalizeSkipReason;
  /** Current outbox state for `not_held`, so the caller can say why. */
  state?: string;
}

export interface LocalizeResult {
  localized: string[];
  skipped: LocalizeSkip[];
}

/**
 * The only path from `held` to `pending`. Only `held` rows move, so a second
 * call, a row already dispatched, rejected or dead, and a video that has no
 * outbox row yet (still downloading) are all left alone and reported as skipped.
 * One transaction: the state flips and the action log commit together.
 */
export async function localize(
  exec: Executor,
  awemeIds: readonly string[],
  source = 'dashboard',
): Promise<LocalizeResult> {
  const ids = [...new Set(awemeIds)];
  if (ids.length === 0) return { localized: [], skipped: [] };

  return exec.transaction(async (tx) => {
    const moved = await tx
      .update(localizationOutbox)
      .set({ state: 'pending', nextAttemptAt: sql`now()` })
      .where(and(inArray(localizationOutbox.awemeId, ids), eq(localizationOutbox.state, 'held')))
      .returning({ awemeId: localizationOutbox.awemeId });
    const localized = moved.map((r) => r.awemeId);

    const movedSet = new Set(localized);
    const rest = ids.filter((id) => !movedSet.has(id));
    const skipped: LocalizeSkip[] = [];
    if (rest.length > 0) {
      const existing = await tx
        .select({ awemeId: localizationOutbox.awemeId, state: localizationOutbox.state })
        .from(localizationOutbox)
        .where(inArray(localizationOutbox.awemeId, rest));
      const stateById = new Map(existing.map((r) => [r.awemeId, r.state]));
      for (const id of rest) {
        const state = stateById.get(id);
        skipped.push(
          state === undefined
            ? { aweme_id: id, reason: 'no_outbox_row' }
            : { aweme_id: id, reason: 'not_held', state },
        );
      }
    }

    if (localized.length > 0) {
      await tx
        .insert(dashboardActions)
        .values(localized.map((awemeId) => ({ awemeId, action: 'localize', detail: { source } })));
    }
    return { localized, skipped };
  });
}

/** Sets a channel's auto-localize switch. False when the channel is unknown. */
export async function setProfileAutoLocalize(
  exec: Executor,
  secUserId: string,
  autoLocalize: boolean,
): Promise<boolean> {
  const rows = await exec
    .update(profiles)
    .set({ autoLocalize })
    .where(eq(profiles.secUserId, secUserId))
    .returning({ secUserId: profiles.secUserId });
  return rows.length > 0;
}

/** Records the per-video override read by `markDone`. Overrides the channel switch. */
export async function setLocalizeOnDone(
  exec: Executor,
  awemeId: string,
  localizeOnDone: boolean,
): Promise<void> {
  await exec.update(videos).set({ localizeOnDone }).where(eq(videos.awemeId, awemeId));
}
