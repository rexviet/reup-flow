import { TemporalDashboardReader } from '../../src/workspace/workspace.dashboard-reader';
import { TemporalWorkflowReader } from '../../src/workspace/workspace.temporal';

type Client = ConstructorParameters<typeof TemporalDashboardReader>[0];

interface Listed {
  workflowId: string;
  startTime: Date;
}

function fakeClient(opts: { open?: Listed[]; describe?: (id: string) => unknown }) {
  const queries: string[] = [];
  const client = {
    workflow: {
      list: ({ query }: { query: string }) => {
        queries.push(query);
        return (async function* () {
          for (const item of opts.open ?? []) yield item;
        })();
      },
      getHandle: (id: string) => ({
        describe: async () => {
          const out = opts.describe?.(id);
          if (out instanceof Error) throw out;
          return out;
        },
      }),
    },
  } as unknown as Client;
  return { client, queries };
}

const notFound = () => Object.assign(new Error('not found'), { name: 'WorkflowNotFoundError' });

describe('TemporalDashboardReader.listOpenLocalizationIds', () => {
  it('returns every open workflow, not just the first ten', async () => {
    const open = Array.from({ length: 37 }, (_, i) => ({
      workflowId: `video-${1000 + i}-g1`,
      startTime: new Date(1_700_000_000_000 + i),
    }));
    const { client } = fakeClient({ open });
    const ids = await new TemporalDashboardReader(client).listOpenLocalizationIds();
    expect(ids).toHaveLength(37);
    expect(ids[0]).toEqual({ workflowId: 'video-1000-g1', startedAtMs: 1_700_000_000_000 });
  });

  it('asks for running Vbee dubbing workflows as well as localization ones', async () => {
    const { client, queries } = fakeClient({});
    await new TemporalDashboardReader(client).listOpenLocalizationIds();
    expect(queries).toHaveLength(1);
    expect(queries[0]).toContain('ExecutionStatus = "Running"');
    expect(queries[0]).toContain('VideoLocalizationWorkflow');
    expect(queries[0]).toContain('VbeeDubbingWorkflow');
  });

  it('propagates a Temporal failure instead of answering an empty list', async () => {
    const client = {
      workflow: {
        list: () =>
          (async function* (): AsyncGenerator<never> {
            yield* [];
            throw new Error('unavailable');
          })(),
      },
    } as unknown as Client;
    await expect(new TemporalDashboardReader(client).listOpenLocalizationIds()).rejects.toThrow('unavailable');
  });

  it('leaves the workspace reader cache alone', async () => {
    const { client } = fakeClient({ open: [{ workflowId: 'video-1-g1', startTime: new Date() }] });
    const workspace = new TemporalWorkflowReader(client);
    const cache = (workspace as unknown as { history: Map<string, unknown> }).history;
    cache.set('video-9-g1', { length: 3, activities: [] });
    await new TemporalDashboardReader(client).listOpenLocalizationIds();
    expect([...cache.keys()]).toEqual(['video-9-g1']);
  });
});

describe('TemporalDashboardReader.workflowStatus', () => {
  const describing = (statusName: string) => () => ({ status: { name: statusName }, raw: {} });

  it('reports running and closed', async () => {
    const run = new TemporalDashboardReader(fakeClient({ describe: describing('RUNNING') }).client);
    const done = new TemporalDashboardReader(fakeClient({ describe: describing('COMPLETED') }).client);
    expect(await run.workflowStatus('video-1-g1')).toBe('running');
    expect(await done.workflowStatus('video-1-g1')).toBe('closed');
  });

  it('reports not_found for an id Temporal has never seen', async () => {
    const reader = new TemporalDashboardReader(fakeClient({ describe: () => notFound() }).client);
    expect(await reader.workflowStatus('video-1-g1')).toBe('not_found');
  });

  it('throws, not closed, when Temporal cannot be read', async () => {
    const reader = new TemporalDashboardReader(fakeClient({ describe: () => new Error('connection refused') }).client);
    await expect(reader.workflowStatus('video-1-g1')).rejects.toThrow('connection refused');
  });
});

describe('TemporalDashboardReader.pendingActivities', () => {
  it('reads pending activities from describe only', async () => {
    const describe = () => ({
      status: { name: 'RUNNING' },
      raw: {
        pendingActivities: [
          {
            activityId: '5',
            activityType: { name: 'transcribe' },
            attempt: 2,
            lastStartedTime: { seconds: 1_700_000_000, nanos: 0 },
          },
        ],
      },
    });
    const reader = new TemporalDashboardReader(fakeClient({ describe }).client);
    expect(await reader.pendingActivities('video-1-g1')).toMatchObject([
      { name: 'transcribe', attempt: 2, atMs: 1_700_000_000_000 },
    ]);
  });

  it('is empty for a workflow that no longer exists', async () => {
    const reader = new TemporalDashboardReader(fakeClient({ describe: () => notFound() }).client);
    expect(await reader.pendingActivities('video-1-g1')).toEqual([]);
  });
});
