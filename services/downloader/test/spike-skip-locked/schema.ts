import { pgTable, serial, text } from 'drizzle-orm/pg-core';

/**
 * Spike-only table (phase-00 §0.4). Created and dropped by the test itself
 * (see db.ts) so the suite is fully self-contained and re-runnable against
 * a fresh Postgres. Not part of the real schema -- phase 1 owns that.
 */
export const spikeItems = pgTable('spike_items', {
  id: serial('id').primaryKey(),
  status: text('status').notNull(),
});
