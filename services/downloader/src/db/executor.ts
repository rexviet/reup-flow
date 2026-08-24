import type { TablesRelationalConfig } from 'drizzle-orm';
import type { NodePgDatabase, NodePgTransaction } from 'drizzle-orm/node-postgres';

/**
 * Anything a repository function can run against: the pooled database
 * handle, or an open transaction.
 *
 * Every repository function here takes one of these as its first argument
 * rather than closing over a module-level connection. That is what lets the
 * claim tests drive N genuinely concurrent transactions through the exact
 * same code path production uses -- a repository that owned its own
 * connection could only ever be tested serially, which is precisely the
 * false green phase 0 caught.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type Executor = NodePgDatabase<any> | NodePgTransaction<any, TablesRelationalConfig>;
