import { createPool } from '../../src/db/db.module';

/**
 * No database needed: what is under test is the listener, not a connection.
 * Without it, `emit('error')` throws exactly as pg's idle-client handler did
 * when Postgres restarted, and the process exited.
 */
describe('createPool', () => {
  it('logs an error on an idle client instead of throwing it', async () => {
    const warnings: string[] = [];
    const pool = createPool('postgres://u:p@127.0.0.1:1/db', {
      warn: (message: string) => warnings.push(message),
    });
    const terminated = Object.assign(
      new Error('terminating connection due to administrator command'),
      { code: '57P01' },
    );

    expect(() => pool.emit('error', terminated)).not.toThrow();
    expect(warnings).toEqual([
      'idle Postgres connection dropped: terminating connection due to administrator command',
    ]);
    await pool.end();
  });

  it('a pool without the listener would have thrown (the crash being prevented)', () => {
    const { Pool } = jest.requireActual<typeof import('pg')>('pg');
    const bare = new Pool({ connectionString: 'postgres://u:p@127.0.0.1:1/db' });
    expect(() => bare.emit('error', new Error('terminating connection'))).toThrow(
      'terminating connection',
    );
    return bare.end();
  });
});
