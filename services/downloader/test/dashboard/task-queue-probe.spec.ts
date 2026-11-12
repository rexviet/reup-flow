import { POLLER_FRESH_MS, TemporalTaskQueueProbe } from '../../src/temporal/task-queue-probe';

type Client = ConstructorParameters<typeof TemporalTaskQueueProbe>[0];

const NOW = 1_700_000_000_000;
const ts = (ms: number) => ({ seconds: Math.floor(ms / 1000), nanos: 0 });

function client(respond: (req: { taskQueue: { name: string }; taskQueueType: number }) => unknown) {
  const calls: Array<{ namespace: string; taskQueue: { name: string }; taskQueueType: number }> = [];
  const c = {
    workflowService: {
      describeTaskQueue: async (req: { namespace: string; taskQueue: { name: string }; taskQueueType: number }) => {
        calls.push(req);
        const out = respond(req);
        if (out instanceof Error) throw out;
        return out;
      },
    },
  } as unknown as Client;
  return { c, calls };
}

describe('TemporalTaskQueueProbe', () => {
  it('counts pollers seen within the last minute', async () => {
    const { c } = client(() => ({
      pollers: [
        { lastAccessTime: ts(NOW - 5_000) },
        { lastAccessTime: ts(NOW - POLLER_FRESH_MS + 1_000) },
        { lastAccessTime: ts(NOW - POLLER_FRESH_MS - 5_000) },
        {},
      ],
    }));
    expect(await new TemporalTaskQueueProbe(c, 'default', () => NOW).freshPollers('tts', 'activity')).toBe(2);
  });

  it('asks for the activity or workflow poller list in the configured namespace', async () => {
    const { c, calls } = client(() => ({ pollers: [] }));
    const probe = new TemporalTaskQueueProbe(c, 'ns1', () => NOW);
    await probe.freshPollers('stt', 'activity');
    await probe.freshPollers('workflows', 'workflow');
    expect(calls.map((x) => [x.namespace, x.taskQueue.name, x.taskQueueType])).toEqual([
      ['ns1', 'stt', 2],
      ['ns1', 'workflows', 1],
    ]);
  });

  it('is zero when nobody polls and throws when Temporal is unreadable', async () => {
    expect(await new TemporalTaskQueueProbe(client(() => ({})).c, 'default').freshPollers('io', 'activity')).toBe(0);
    const down = client(() => new Error('unavailable')).c;
    await expect(new TemporalTaskQueueProbe(down, 'default').freshPollers('io', 'activity')).rejects.toThrow('unavailable');
  });
});
