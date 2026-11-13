import type { QueueKind, TaskQueueProbe } from '../temporal/task-queue-probe';
import type { CookieStatus } from '../cookie/cookie.service';
import type { HostWorkerRow } from './dashboard.repository';

export type HealthState = 'ok' | 'degraded' | 'down' | 'unknown';

export interface HealthItem {
  status: HealthState;
  reason: string;
}

export interface QueueHealth extends HealthItem {
  pollers?: number;
}

export const QUEUE_NAMES = ['stt', 'tts', 'ingest', 'media', 'render', 'llm', 'io', 'workflows'] as const;
export type QueueName = (typeof QUEUE_NAMES)[number];
const QUEUE_KIND_OF: Record<QueueName, QueueKind> = {
  stt: 'activity',
  tts: 'activity',
  ingest: 'activity',
  media: 'activity',
  render: 'activity',
  llm: 'activity',
  io: 'activity',
  workflows: 'workflow',
};

export interface HealthReport {
  generatedAt: string;
  postgres: HealthItem;
  redis: HealthItem;
  cookie: HealthItem;
  temporal: HealthItem;
  minio: HealthItem;
  voicestudio: HealthItem;
  queues: Record<QueueName, QueueHealth>;
  host_worker: HealthItem;
  disk: HealthItem;
}

/** The review worker on the host writes a heartbeat each loop; this long without one is down. */
export const HOST_HEARTBEAT_STALE_MS = 2 * 60 * 1000;
export const HOST_WORKER_NAME = 'review-worker';
/** Below this much free disk the host cannot hold another render. */
export const DISK_DOWN_BYTES = 5 * 1024 ** 3;
export const DISK_DEGRADED_BYTES = 20 * 1024 ** 3;
/** A cookie older than this is close to the 24h expiry. */
export const COOKIE_DEGRADED_AGE_SECONDS = 12 * 60 * 60;
export const PROBE_TIMEOUT_MS = 3000;

export interface HealthDeps {
  nowMs(): number;
  pingPostgres(): Promise<void>;
  pingRedis(): Promise<void>;
  cookieStatus(): Promise<CookieStatus>;
  queueProbe: TaskQueueProbe;
  /** `undefined` when no S3 endpoint is configured. */
  minioEndpoint: string | undefined;
  fetchImpl: typeof fetch;
  hostWorker(): Promise<HostWorkerRow | null>;
}

function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  let timer: NodeJS.Timeout;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error('timed out')), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

async function check(run: () => Promise<HealthItem>, downReason: string): Promise<HealthItem> {
  try {
    return await withTimeout(run(), PROBE_TIMEOUT_MS);
  } catch {
    return { status: 'down', reason: downReason };
  }
}

const OK: HealthItem = { status: 'ok', reason: 'Bình thường.' };

function cookieHealth(status: CookieStatus): HealthItem {
  if (!status.present) return { status: 'down', reason: 'Chưa có cookie Douyin; job tải sẽ bị chặn.' };
  if (status.age_seconds !== null && status.age_seconds > COOKIE_DEGRADED_AGE_SECONDS) {
    return { status: 'degraded', reason: 'Cookie đã cũ, sắp hết hạn 24 giờ.' };
  }
  return OK;
}

function hostHealth(row: HostWorkerRow | null, nowMs: number): HealthItem {
  if (!row) return { status: 'down', reason: 'Chưa thấy heartbeat của worker review trên máy.' };
  if (nowMs - row.seenAtMs > HOST_HEARTBEAT_STALE_MS) {
    return { status: 'down', reason: 'Worker review trên máy quá 2 phút không báo heartbeat.' };
  }
  return OK;
}

function voicestudioHealth(info: Record<string, unknown>): HealthItem {
  const vs = info.voicestudio as { ok?: unknown } | undefined;
  if (vs?.ok === true) return OK;
  if (vs?.ok === false) return { status: 'down', reason: 'VoiceStudio không trả lời worker trên máy.' };
  return { status: 'unknown', reason: 'Worker chưa báo tình trạng VoiceStudio.' };
}

function diskHealth(info: Record<string, unknown>): HealthItem {
  const free = Number(info.disk_free_bytes);
  if (info.disk_free_bytes === undefined || !Number.isFinite(free)) {
    return { status: 'unknown', reason: 'Worker chưa báo dung lượng đĩa.' };
  }
  const gib = Math.round((free / 1024 ** 3) * 10) / 10;
  if (free < DISK_DOWN_BYTES) return { status: 'down', reason: `Đĩa còn ${gib} GB, không đủ để render.` };
  if (free < DISK_DEGRADED_BYTES) return { status: 'degraded', reason: `Đĩa còn ${gib} GB.` };
  return { status: 'ok', reason: `Đĩa còn ${gib} GB.` };
}

/** Never throws: each probe that fails becomes a `down` entry with its reason. */
export async function buildHealth(deps: HealthDeps): Promise<HealthReport> {
  const nowMs = deps.nowMs();
  const unreadable = (what: string): HealthItem => ({ status: 'unknown', reason: `Không đọc được ${what}.` });

  const queueResults = await Promise.all(
    QUEUE_NAMES.map(async (name): Promise<[QueueName, { pollers?: number; error?: true }]> => {
      try {
        const pollers = await withTimeout(deps.queueProbe.freshPollers(name, QUEUE_KIND_OF[name]), PROBE_TIMEOUT_MS);
        return [name, { pollers }];
      } catch {
        return [name, { error: true }];
      }
    }),
  );
  const temporalReadable = queueResults.some(([, r]) => r.error === undefined);

  const [postgres, redis, cookie, minio, host] = await Promise.all([
    check(async () => (await deps.pingPostgres(), OK), 'Postgres không trả lời.'),
    check(async () => (await deps.pingRedis(), OK), 'Redis không trả lời.'),
    check(async () => cookieHealth(await deps.cookieStatus()), 'Không đọc được cookie từ Redis.'),
    deps.minioEndpoint === undefined
      ? Promise.resolve<HealthItem>({ status: 'unknown', reason: 'Không dùng object store S3.' })
      : check(async () => {
          const res = await deps.fetchImpl(`${deps.minioEndpoint}/minio/health/ready`, {
            signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
          });
          return res.ok ? OK : { status: 'down' as const, reason: `MinIO trả mã ${res.status}.` };
        }, 'MinIO không trả lời.'),
    (async () => {
      try {
        return { row: await withTimeout(deps.hostWorker(), PROBE_TIMEOUT_MS), failed: false };
      } catch {
        return { row: null, failed: true };
      }
    })(),
  ]);

  const queues = Object.fromEntries(
    queueResults.map(([name, r]): [QueueName, QueueHealth] => {
      if (r.error) return [name, { ...unreadable('hàng đợi Temporal') }];
      if (r.pollers === 0) return [name, { status: 'down', reason: 'Không có worker nào đang poll.', pollers: 0 }];
      return [name, { ...OK, pollers: r.pollers }];
    }),
  ) as Record<QueueName, QueueHealth>;

  const host_worker = host.failed ? unreadable('heartbeat worker trên máy') : hostHealth(host.row, nowMs);
  // Without a live heartbeat the numbers it carried are stale, so they are not shown as facts.
  const hostAlive = host_worker.status === 'ok' && host.row !== null;
  const stale: HealthItem = { status: 'unknown', reason: 'Worker trên máy không hoạt động nên không có số liệu.' };

  return {
    generatedAt: new Date(nowMs).toISOString(),
    postgres,
    redis,
    cookie,
    temporal: temporalReadable
      ? OK
      : { status: 'down', reason: 'Temporal không trả lời.' },
    minio,
    voicestudio: hostAlive && host.row ? voicestudioHealth(host.row.info) : stale,
    queues,
    host_worker,
    disk: hostAlive && host.row ? diskHealth(host.row.info) : stale,
  };
}
