import { NotFoundException } from '@nestjs/common';
import type { VideoRecord } from '../../src/dashboard/dashboard.repository';
import { DashboardService, type DashboardStore } from '../../src/dashboard/dashboard.service';
import type { DashboardTemporal } from '../../src/workspace/workspace.dashboard-reader';
import type { PendingActivity, WorkflowInspection } from '../../src/workspace/workspace.temporal';

const NOW = Date.parse('2026-11-12T10:00:00Z');
const MIN = 60_000;

const record = (awemeId: string, over: Partial<VideoRecord> = {}): VideoRecord => ({
  awemeId,
  secUserId: 'chA',
  nickname: 'Kênh A',
  autoLocalize: false,
  desc: `mô tả ${awemeId}`,
  createTime: '2026-11-01T00:00:00.000Z',
  durationMs: 1000,
  videoStatus: 'done',
  downloadAttempts: 1,
  downloadError: null,
  outbox: { generation: 1, state: 'held', dispatchedAt: null, workflowId: null, lastError: null, dispatchAttempts: 0 },
  runs: [],
  decisions: [],
  renders: [],
  publications: [],
  reviewStatus: [],
  ...over,
});

const run = (generation: number, status: string) => ({
  generation,
  status,
  hasOutputKey: false,
  updatedAt: '2026-11-10T00:00:00.000Z',
  workflowId: `video-x-g${generation}`,
  projectKey: null,
  reviewRounds: 0,
});

function store(records: VideoRecord[] | Error, extra: Partial<DashboardStore> = {}): DashboardStore {
  const read = async <T>(value: T | Error): Promise<T> => {
    if (value instanceof Error) throw value;
    return value;
  };
  return {
    videos: async (filter) =>
      read(
        records instanceof Error
          ? records
          : records.filter((r) => (filter?.awemeId === undefined || r.awemeId === filter.awemeId) && (filter?.channel === undefined || r.secUserId === filter.channel)),
      ),
    video: async (id) => (await read(records instanceof Error ? records : records.find((r) => r.awemeId === id) ?? null)),
    llmCosts: async () => [],
    runDetails: async () => [],
    recentActions: async () => [],
    latestStudioRequest: async () => null,
    channels: async () => [],
    blockedJobs: async () => [],
    ...extra,
  };
}

function temporal(over: Partial<DashboardTemporal> = {}, open: Array<{ id: string; pending?: PendingActivity[] }> = []): DashboardTemporal {
  return {
    listOpenLocalizationIds: async () => open.map((o) => ({ workflowId: o.id, startedAtMs: NOW })),
    workflowStatus: async () => 'running',
    pendingActivities: async (id) => open.find((o) => o.id === id)?.pending ?? [],
    inspect: async () => null,
    ...over,
  };
}

// The detail payload is asserted field by field below.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Detail = Record<string, any>;
const down = (): never => {
  throw new Error('down');
};
const svc = (s: DashboardStore, t: DashboardTemporal) => new DashboardService(s, t, 30, () => NOW);
const query = (over: Record<string, unknown> = {}) => ({ limit: 50, offset: 0, ...over }) as never;

describe('DashboardService.list', () => {
  it('reports the localizing step from the running activity of the open workflow', async () => {
    const records = [record('1', { outbox: { generation: 1, state: 'dispatched', dispatchedAt: new Date(NOW - MIN).toISOString(), workflowId: 'video-1-g1', lastError: null, dispatchAttempts: 1 } })];
    const t = temporal({}, [{ id: 'video-1-g1', pending: [{ activityId: '1', name: 'synthesize_segments', attempt: 1, atMs: NOW - MIN }] }]);
    const out = await svc(store(records), t).list(query());
    expect(out.items[0]).toMatchObject({ aweme_id: '1', stage: 'localizing', step: 'dub', current_gen: 1 });
    expect(out.sources).toEqual({ postgres: 'ok', temporal: 'ok' });
  });

  it('gives each row the time of its latest stage fact and the review revision', async () => {
    const dispatchedAt = new Date(NOW - 20 * MIN).toISOString();
    const runAt = new Date(NOW - 5 * MIN).toISOString();
    const checkedAt = new Date(NOW - MIN).toISOString();
    const r = run(1, 'ready_for_review');
    const records = [
      record('1', {
        outbox: { generation: 1, state: 'dispatched', dispatchedAt, workflowId: 'video-1-g1', lastError: null, dispatchAttempts: 1 },
        runs: [{ ...r, updatedAt: runAt }],
        reviewStatus: [{ generation: 1, revision: 'rev-9', cueCount: 3, mismatchCount: 2, dirty: false, checkedAt }],
      }),
    ];
    const out = await svc(store(records), temporal()).list(query());
    expect(out.items[0]).toMatchObject({
      stage_since: runAt,
      updated_at: checkedAt,
      revision: 'rev-9',
      mismatch_count: 2,
    });
  });

  it('shows the generation a canary created, not the outbox generation', async () => {
    const records = [
      record('1', {
        outbox: { generation: 1, state: 'dispatched', dispatchedAt: new Date(NOW - 99 * MIN).toISOString(), workflowId: 'video-1-g1', lastError: null, dispatchAttempts: 1 },
        runs: [run(1, 'needs_human'), run(3, 'ready_for_review')],
      }),
      record('2', {
        outbox: { generation: 1, state: 'dispatched', dispatchedAt: new Date(NOW - 99 * MIN).toISOString(), workflowId: 'video-2-g1', lastError: null, dispatchAttempts: 1 },
        runs: [run(1, 'failed'), run(7, 'ready_for_review')],
      }),
    ];
    const out = await svc(store(records), temporal()).list(query());
    expect(out.items.map((i) => [i.aweme_id, i.current_gen, i.stage])).toEqual([
      ['1', 3, 'awaiting_review'],
      ['2', 7, 'awaiting_review'],
    ]);
  });

  it('does not show the outbox state of an older generation', async () => {
    const records = [record('1', { outbox: { generation: 1, state: 'dispatched', dispatchedAt: null, workflowId: null, lastError: null, dispatchAttempts: 1 }, runs: [run(3, 'failed')] })];
    const out = await svc(store(records), temporal()).list(query());
    expect(out.items[0]).toMatchObject({ current_gen: 3, outbox_state: null });
  });

  it('keeps Temporal up when one workflow describe hangs, and marks only that step unknown', async () => {
    jest.useFakeTimers();
    try {
      const records = [
        record('1', { outbox: { generation: 1, state: 'dispatched', dispatchedAt: new Date(NOW - MIN).toISOString(), workflowId: null, lastError: null, dispatchAttempts: 1 } }),
        record('2', { outbox: { generation: 1, state: 'dispatched', dispatchedAt: new Date(NOW - MIN).toISOString(), workflowId: null, lastError: null, dispatchAttempts: 1 } }),
      ];
      const t = temporal(
        { pendingActivities: (id) => (id === 'video-1-g1' ? new Promise(() => undefined) : Promise.resolve([{ activityId: '1', name: 'ingest', attempt: 1, atMs: NOW - MIN }])) },
        [{ id: 'video-1-g1' }, { id: 'video-2-g1' }],
      );
      const pending = svc(store(records), t).list(query());
      await jest.advanceTimersByTimeAsync(2000);
      const out = await pending;
      expect(out.sources.temporal).toBe('ok');
      expect(out.items.map((i) => [i.aweme_id, i.step])).toEqual([['1', 'unknown'], ['2', 'prepare']]);
    } finally {
      jest.useRealTimers();
    }
  });

  it('counts every stage over the filtered base set, and filters by stage, attention and page', async () => {
    const records = [
      record('1'),
      record('2'),
      record('3', { outbox: { generation: 1, state: 'pending', dispatchedAt: null, workflowId: null, lastError: null, dispatchAttempts: 0 } }),
      record('4', { runs: [run(1, 'failed')] }),
    ];
    const s = svc(store(records), temporal());
    const all = await s.list(query());
    expect(all.total).toBe(4);
    expect(all.counts_by_stage).toMatchObject({ held: 2, queued: 1, run_failed: 1, downloading: 0 });

    const held = await s.list(query({ stage: 'held', limit: 1, offset: 1 }));
    expect(held.total).toBe(2);
    expect(held.items).toHaveLength(1);
    expect(held.counts_by_stage.held).toBe(2);

    const attention = await s.list(query({ attention: true }));
    expect(attention.items.map((i) => i.aweme_id)).toEqual(['4']);
    expect((await s.list(query({ attention: false }))).total).toBe(3);
  });

  it('answers 200 shaped data when Temporal is down: running videos are unknown, not idle', async () => {
    const records = [record('1', { outbox: { generation: 1, state: 'dispatched', dispatchedAt: new Date(NOW - 60 * MIN).toISOString(), workflowId: 'video-1-g1', lastError: null, dispatchAttempts: 1 } })];
    const out = await svc(store(records), temporal({ listOpenLocalizationIds: async () => down() })).list(query());
    expect(out.sources).toEqual({ postgres: 'ok', temporal: 'down' });
    expect(out.items[0]).toMatchObject({ stage: 'localizing', step: 'unknown', unknown: true });
    expect(out.items[0].attention).toEqual([]);
  });

  it('answers an empty list with postgres down', async () => {
    const out = await svc(store(new Error('down')), temporal()).list(query());
    expect(out).toMatchObject({ items: [], total: 0, sources: { postgres: 'down', temporal: 'ok' } });
  });

  it('survives one workflow whose activities cannot be read', async () => {
    const records = [record('1', { outbox: { generation: 1, state: 'dispatched', dispatchedAt: new Date(NOW - MIN).toISOString(), workflowId: null, lastError: null, dispatchAttempts: 1 } })];
    const t = temporal({ pendingActivities: async () => down() }, [{ id: 'video-1-g1' }]);
    const out = await svc(store(records), t).list(query());
    expect(out.items[0]).toMatchObject({ stage: 'localizing', step: 'unknown' });
    expect(out.sources.temporal).toBe('ok');
  });

  it('reads Temporal once for callers inside the cache window', async () => {
    let calls = 0;
    const t = temporal({ listOpenLocalizationIds: async () => (calls++, []) });
    const s = svc(store([record('1')]), t);
    await Promise.all([s.list(query()), s.channels(), s.attention()]);
    expect(calls).toBe(1);
  });

  it('truncates a long description', async () => {
    const out = await svc(store([record('1', { desc: 'x'.repeat(5000) })]), temporal()).list(query());
    expect(out.items[0].desc).toHaveLength(200);
  });
});

describe('DashboardService.attention', () => {
  it('lists blocked jobs first-class and videos that need the admin', async () => {
    const s = store([record('1'), record('2', { runs: [run(1, 'failed')] })], {
      blockedJobs: async () => [{ id: 'j1', secUserId: 'chA', nickname: 'Kênh A', status: 'blocked', error: 'cookie', updatedAt: '2026-11-12T00:00:00.000Z' }],
    });
    const out = await svc(s, temporal()).attention();
    expect(out.jobs).toMatchObject([{ id: 'j1', status: 'blocked', channel: 'Kênh A' }]);
    expect(out.jobs[0].reason).toMatch(/cookie/i);
    expect(out.items.map((i) => i.aweme_id)).toEqual(['2']);
  });
});

describe('DashboardService.channels', () => {
  it('counts videos per channel and stage', async () => {
    const s = store([record('1'), record('2', { secUserId: 'chB' }), record('3', { runs: [run(1, 'failed')] })], {
      channels: async () => [
        { secUserId: 'chA', nickname: 'Kênh A', autoLocalize: true, lastSyncedAt: null, latestJob: null },
        { secUserId: 'chB', nickname: null, autoLocalize: false, lastSyncedAt: null, latestJob: null },
        { secUserId: 'chC', nickname: null, autoLocalize: false, lastSyncedAt: null, latestJob: null },
      ],
    });
    const { items } = await svc(s, temporal()).channels();
    expect(items.map((c) => [c.sec_user_id, c.total, c.counts_by_stage.held, c.counts_by_stage.run_failed])).toEqual([
      ['chA', 2, 1, 1],
      ['chB', 1, 1, 0],
      ['chC', 0, 0, 0],
    ]);
    expect(items[0].auto_localize).toBe(true);
  });
});

describe('DashboardService.detail', () => {
  it('lists every generation, the current one marked, with revision-bearing review status', async () => {
    const rec = record('1', {
      outbox: { generation: 1, state: 'dispatched', dispatchedAt: null, workflowId: 'video-1-g1', lastError: null, dispatchAttempts: 1 },
      runs: [run(1, 'needs_human'), { ...run(3, 'ready_for_review'), projectKey: 'review/1/g3/head.json' }],
      reviewStatus: [{ generation: 3, revision: 'rev-4', cueCount: 10, mismatchCount: 1, dirty: false, checkedAt: '2026-11-12T00:00:00.000Z' }],
      renders: [{ generation: 3, state: 'queued', approvedRevision: 'rev-4', attempts: 0, error: null, finalKey: 'k/final.mp4', srtKey: null, renderedAt: null }],
    });
    const inspection: WorkflowInspection = {
      workflowId: 'video-x-g3',
      pending: [],
      activities: [{ eventId: 5, name: 'plan_slots', queue: 'llm', scheduledAtMs: NOW - 5 * MIN, completedAtMs: NOW - 4 * MIN }],
    };
    const s = store([rec], {
      llmCosts: async () => [
        { generation: 1, stage: 'agent__localizer', calls: 2, costUsd: 0.25 },
        { generation: 3, stage: 'agent__dubber', calls: 1, costUsd: 0.5 },
      ],
      runDetails: async () => [{ generation: 3, verdict: { ok: true }, needsHuman: false, outputKey: null, reviewKey: null }],
      recentActions: async () => [{ id: 1, action: 'localize', detail: {}, createdAt: '2026-11-12T00:00:00.000Z' }],
    });
    const out = (await svc(s, temporal({ inspect: async () => inspection })).detail('1')) as Detail;
    expect(out.current_gen).toBe(3);
    expect(out.stage).toBe('awaiting_review');
    expect(out.generations.map((g: { generation: number; is_current: boolean }) => [g.generation, g.is_current])).toEqual([[3, true], [1, false]]);
    expect(out.review_status).toMatchObject({ revision: 'rev-4', mismatchCount: 1 });
    expect(out.verdict).toEqual({ ok: true });
    expect(out.llm_cost).toEqual({ total_usd: 0.5, by_stage: [{ stage: 'agent__dubber', calls: 1, cost_usd: 0.5 }] });
    expect(out.timeline).toMatchObject([{ activity: 'plan_slots', step: 'dub', state: 'done' }]);
    expect(out.dashboard_actions).toHaveLength(1);
    expect(out.render).toMatchObject({ state: 'queued', final_ready: true, srt_ready: false });
    expect(JSON.stringify(out)).not.toContain('k/final.mp4');
    expect(out.sources).toEqual({ postgres: 'ok', temporal: 'ok' });
  });

  it('is 404 only when postgres answered and the video does not exist', async () => {
    await expect(svc(store([]), temporal()).detail('9')).rejects.toBeInstanceOf(NotFoundException);
  });

  it('is a 200 with a null video and sources when postgres is down', async () => {
    expect(await svc(store(new Error('down')), temporal()).detail('9')).toEqual({
      video: null,
      sources: { postgres: 'down', temporal: 'ok' },
    });
  });

  it('keeps the page with a null timeline when Temporal is down', async () => {
    const out = (await svc(store([record('1', { runs: [run(1, 'failed')] })]), temporal({ listOpenLocalizationIds: async () => down(), inspect: async () => down() })).detail('1')) as Detail;
    expect(out.timeline).toBeNull();
    expect(out.sources.temporal).toBe('down');
    expect(out.stage).toBe('run_failed');
  });
});
