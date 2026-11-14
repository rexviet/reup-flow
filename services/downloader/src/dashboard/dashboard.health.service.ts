import { Inject, Injectable } from '@nestjs/common';
import type Redis from 'ioredis';
import { ENV } from '../config/config.module';
import type { Env } from '../config/env';
import { CookieService } from '../cookie/cookie.service';
import { REDIS } from '../redis/redis.module';
import { TASK_QUEUE_PROBE, type TaskQueueProbe } from '../temporal/task-queue-probe';
import { buildHealth, type HealthReport } from './dashboard.health';
import { DashboardRepository } from './dashboard.repository';

/** Probes are cheap but several tabs poll: one read serves everyone inside this window. */
export const HEALTH_CACHE_MS = 5000;

@Injectable()
export class DashboardHealthService {
  private cached?: { atMs: number; value: Promise<HealthReport> };

  constructor(
    @Inject(DashboardRepository) private readonly repo: DashboardRepository,
    @Inject(REDIS) private readonly redis: Redis,
    @Inject(CookieService) private readonly cookie: CookieService,
    @Inject(TASK_QUEUE_PROBE) private readonly queueProbe: TaskQueueProbe,
    @Inject(ENV) private readonly env: Env,
  ) {}

  report(): Promise<HealthReport> {
    const nowMs = Date.now();
    if (this.cached && nowMs - this.cached.atMs < HEALTH_CACHE_MS) return this.cached.value;
    const value = buildHealth({
      nowMs: () => nowMs,
      pingPostgres: () => this.repo.ping(),
      pingRedis: async () => {
        await this.redis.ping();
      },
      cookieStatus: () => this.cookie.status('douyin'),
      queueProbe: this.queueProbe,
      minioEndpoint: this.env.S3_ENDPOINT,
      fetchImpl: fetch,
      hostWorkers: () => this.repo.hostWorkers(),
    });
    this.cached = { atMs: nowMs, value };
    return value;
  }
}
