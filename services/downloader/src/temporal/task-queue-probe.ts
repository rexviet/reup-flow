import type { Client } from '@temporalio/client';
import { tsToMs } from '../workspace/workspace.temporal';

/** `temporal.api.enums.v1.TaskQueueType`: the server counts pollers per kind. */
const QUEUE_KIND = { workflow: 1, activity: 2 } as const;
export type QueueKind = keyof typeof QUEUE_KIND;

/** A worker whose last poll is older than this is gone. */
export const POLLER_FRESH_MS = 60_000;

/** Read only: asks the server who is polling a task queue. */
export interface TaskQueueProbe {
  /** Pollers seen within `POLLER_FRESH_MS`. Throws when Temporal is unreadable. */
  freshPollers(queue: string, kind: QueueKind): Promise<number>;
}

export class TemporalTaskQueueProbe implements TaskQueueProbe {
  constructor(
    private readonly client: Client,
    private readonly namespace: string,
    private readonly clock: () => number = Date.now,
  ) {}

  async freshPollers(queue: string, kind: QueueKind): Promise<number> {
    const response = await this.client.workflowService.describeTaskQueue({
      namespace: this.namespace,
      taskQueue: { name: queue },
      taskQueueType: QUEUE_KIND[kind],
    });
    const nowMs = this.clock();
    return (response.pollers ?? []).filter((p) => {
      const seen = tsToMs(p.lastAccessTime);
      return seen !== undefined && nowMs - seen <= POLLER_FRESH_MS;
    }).length;
  }
}

export const TASK_QUEUE_PROBE = 'TASK_QUEUE_PROBE';
