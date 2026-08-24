import { eq, inArray } from 'drizzle-orm';
import type { NodePgDatabase, NodePgTransaction } from 'drizzle-orm/node-postgres';
import type { TablesRelationalConfig } from 'drizzle-orm';
import { spikeItems } from './schema';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type SpikeExecutor = NodePgDatabase<any> | NodePgTransaction<any, TablesRelationalConfig>;

/**
 * SPIKE 1 (GATE) claim query -- the real shape phase 1's `videos` claim
 * will use:
 *
 *   UPDATE videos SET ... WHERE id IN (
 *     SELECT id FROM videos WHERE status = 'pending'
 *     ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED
 *   ) RETURNING ...
 *
 * Expressed entirely through the Drizzle query builder -- zero `sql`
 * template fragments. See db.ts and skip-locked.spec.ts for the concurrent
 * runtime proof; `.toSQL()` text alone does NOT prove this works.
 */
export function claimOne(tx: SpikeExecutor) {
  const claimable = tx
    .select({ id: spikeItems.id })
    .from(spikeItems)
    .where(eq(spikeItems.status, 'pending'))
    .orderBy(spikeItems.id)
    .limit(1)
    .for('update', { skipLocked: true });

  return tx
    .update(spikeItems)
    .set({ status: 'claimed' })
    .where(inArray(spikeItems.id, claimable))
    .returning();
}
