import { buildHealth, DISK_DEGRADED_BYTES, DISK_DOWN_BYTES, hostWorkerViews, QUEUE_NAMES, type HealthDeps } from '../../src/dashboard/dashboard.health';
import type { HostWorkerRow } from '../../src/dashboard/dashboard.repository';

const NOW = Date.parse('2026-11-12T10:00:00Z');
const GIB = 1024 ** 3;

function deps(over: Partial<HealthDeps> = {}): HealthDeps {
  return {
    nowMs: () => NOW,
    pingPostgres: async () => undefined,
    pingRedis: async () => undefined,
    cookieStatus: async () => ({ present: true, age_seconds: 60, last_ok_at: null }),
    queueProbe: { freshPollers: async () => 1 },
    minioEndpoint: 'http://minio:9000',
    fetchImpl: (async () => new Response('ok', { status: 200 })) as typeof fetch,
    hostWorkers: async () => [
      {
        name: 'review-worker',
        seenAtMs: NOW - 10_000,
        info: { disk_free_bytes: 100 * GIB, voicestudio: { ok: true } },
      },
    ],
    ...over,
  };
}

describe('buildHealth', () => {
  it('is ok everywhere when every probe answers', async () => {
    const h = await buildHealth(deps());
    expect(h).toMatchObject({
      postgres: { status: 'ok' },
      redis: { status: 'ok' },
      cookie: { status: 'ok' },
      temporal: { status: 'ok' },
      minio: { status: 'ok' },
      voicestudio: { status: 'ok' },
      host_worker: { status: 'ok' },
      disk: { status: 'ok' },
    });
    expect(Object.keys(h.queues)).toEqual([...QUEUE_NAMES]);
    expect(Object.values(h.queues).every((q) => q.status === 'ok')).toBe(true);
  });

  it('marks a queue without pollers down and the others untouched', async () => {
    const h = await buildHealth(deps({ queueProbe: { freshPollers: async (q) => (q === 'tts' ? 0 : 2) } }));
    expect(h.queues.tts).toMatchObject({ status: 'down', pollers: 0 });
    expect(h.queues.stt.status).toBe('ok');
    expect(h.temporal.status).toBe('ok');
  });

  it('reports the ingest queue down when the host ingest worker is not polling', async () => {
    const h = await buildHealth(deps({ queueProbe: { freshPollers: async (q) => (q === 'ingest' ? 0 : 2) } }));
    expect(h.queues.ingest).toMatchObject({ status: 'down', pollers: 0 });
    expect(h.queues.media).toMatchObject({ status: 'ok' });
  });

  it('marks Temporal down and queues unknown when no queue can be read', async () => {
    const h = await buildHealth(deps({ queueProbe: { freshPollers: async () => { throw new Error('down'); } } }));
    expect(h.temporal.status).toBe('down');
    expect(Object.values(h.queues).every((q) => q.status === 'unknown')).toBe(true);
  });

  it('marks postgres, redis, cookie and MinIO down independently, each with a reason', async () => {
    const h = await buildHealth(
      deps({
        pingPostgres: async () => { throw new Error('x'); },
        pingRedis: async () => { throw new Error('x'); },
        cookieStatus: async () => { throw new Error('x'); },
        fetchImpl: (async () => { throw new Error('refused'); }) as typeof fetch,
      }),
    );
    for (const key of ['postgres', 'redis', 'cookie', 'minio'] as const) {
      expect(h[key].status).toBe('down');
      expect(h[key].reason).toMatch(/\S/);
    }
    expect(h.temporal.status).toBe('ok');
  });

  it('treats a non-2xx MinIO answer as down and a missing endpoint as unknown', async () => {
    const bad = await buildHealth(deps({ fetchImpl: (async () => new Response('', { status: 503 })) as typeof fetch }));
    expect(bad.minio.status).toBe('down');
    const none = await buildHealth(deps({ minioEndpoint: undefined }));
    expect(none.minio.status).toBe('unknown');
  });

  it('flags a missing cookie down and an old one degraded', async () => {
    expect((await buildHealth(deps({ cookieStatus: async () => ({ present: false, age_seconds: null, last_ok_at: null }) }))).cookie.status).toBe('down');
    expect((await buildHealth(deps({ cookieStatus: async () => ({ present: true, age_seconds: 20 * 3600, last_ok_at: null }) }))).cookie.status).toBe('degraded');
  });

  it('shows VoiceStudio down from the worker probe, never from a direct call', async () => {
    const fetchImpl = jest.fn(async () => new Response('ok')) as unknown as typeof fetch;
    const h = await buildHealth(
      deps({
        fetchImpl,
        hostWorkers: async () => [{ name: 'review-worker', seenAtMs: NOW, info: { voicestudio: { ok: false }, disk_free_bytes: 50 * GIB } }],
      }),
    );
    expect(h.voicestudio.status).toBe('down');
    expect(fetchImpl).toHaveBeenCalledTimes(1); // only MinIO
  });

  it('grades the disk by free space', async () => {
    const at = (free: number) => deps({ hostWorkers: async () => [{ name: 'review-worker', seenAtMs: NOW, info: { disk_free_bytes: free } }] });
    expect((await buildHealth(at(DISK_DOWN_BYTES - 1))).disk.status).toBe('down');
    expect((await buildHealth(at(DISK_DEGRADED_BYTES - 1))).disk.status).toBe('degraded');
    expect((await buildHealth(at(DISK_DEGRADED_BYTES))).disk.status).toBe('ok');
  });

  it('is down after two minutes without a heartbeat and then VoiceStudio and disk are unknown', async () => {
    const h = await buildHealth(
      deps({ hostWorkers: async () => [{ name: 'review-worker', seenAtMs: NOW - 121_000, info: { voicestudio: { ok: true }, disk_free_bytes: 100 * GIB } }] }),
    );
    expect(h.host_worker.status).toBe('down');
    expect(h.voicestudio.status).toBe('unknown');
    expect(h.disk.status).toBe('unknown');
  });

  it('is down with no heartbeat row, and unknown when the row cannot be read', async () => {
    expect((await buildHealth(deps({ hostWorkers: async () => [] }))).host_worker.status).toBe('down');
    expect((await buildHealth(deps({ hostWorkers: async () => { throw new Error('x'); } }))).host_worker.status).toBe('unknown');
  });

  it('does not wait for a probe that never answers', async () => {
    jest.useFakeTimers();
    try {
      const pending = buildHealth(deps({ pingRedis: () => new Promise(() => undefined) }));
      await jest.advanceTimersByTimeAsync(3500);
      expect((await pending).redis.status).toBe('down');
    } finally {
      jest.useRealTimers();
    }
  });
});

describe('hostWorkerViews', () => {
  const row = (name: string, info: Record<string, unknown>, ageMs = 10_000): HostWorkerRow => ({ name, seenAtMs: NOW - ageMs, info });

  it('lists every machine with liveness, current work and version', async () => {
    const h = await buildHealth(
      deps({
        hostWorkers: async () => [
          row('review-worker', { code_version: 'def5678', protocol: 2, current: null }),
          row('render-worker-win', { code_version: 'def5678', protocol: 2, current: { aweme_id: '7001', generation: 1, progress: '35% Capturing' } }),
        ],
      }),
    );
    expect(h.host_workers).toEqual([
      {
        name: 'render-worker-win',
        alive: true,
        seen_at: new Date(NOW - 10_000).toISOString(),
        code_version: 'def5678',
        protocol: 2,
        current: { aweme_id: '7001', generation: 1, progress: '35% Capturing' },
        outdated: false,
        outdated_reason: null,
        latest_code_version: 'def5678',
      },
      expect.objectContaining({ name: 'review-worker', alive: true, current: null, outdated: false }),
    ]);
  });

  it('flags a machine that does not report its version as outdated', () => {
    const [win] = hostWorkerViews([row('render-worker-win', { disk_free_bytes: 1 }), row('review-worker', { code_version: 'def5678', protocol: 2 })], NOW);
    expect(win).toMatchObject({ code_version: null, protocol: null, outdated: true, outdated_reason: 'no_version' });
  });

  it('flags a lower protocol than the newest live machine, and a version that differs from the Mac', () => {
    const views = hostWorkerViews(
      [
        row('review-worker', { code_version: 'def5678', protocol: 3 }),
        row('render-worker-win', { code_version: 'abc1234', protocol: 2 }),
        row('render-2', { code_version: 'abc1234', protocol: 3 }),
      ],
      NOW,
    );
    const by = Object.fromEntries(views.map((v) => [v.name, v]));
    expect(by['render-worker-win']).toMatchObject({ outdated: true, outdated_reason: 'protocol', latest_code_version: 'def5678' });
    expect(by['render-2']).toMatchObject({ outdated: true, outdated_reason: 'version' });
    expect(by['review-worker']).toMatchObject({ outdated: false });
  });

  it('marks a silent machine as not alive and ignores its protocol, and trusts no unknown version', () => {
    const views = hostWorkerViews(
      [
        row('review-worker', { code_version: 'unknown', protocol: 2 }),
        row('render-worker-win', { code_version: 'abc1234', protocol: 2 }),
        row('old-box', { code_version: 'abc1234', protocol: 9 }, 10 * 60_000),
      ],
      NOW,
    );
    const by = Object.fromEntries(views.map((v) => [v.name, v]));
    expect(by['old-box']).toMatchObject({ alive: false, outdated: false });
    expect(by['render-worker-win']).toMatchObject({ alive: true, outdated: false, latest_code_version: null });
  });

  it('is empty when the heartbeats cannot be read', async () => {
    expect((await buildHealth(deps({ hostWorkers: async () => { throw new Error('x'); } }))).host_workers).toEqual([]);
  });
});
