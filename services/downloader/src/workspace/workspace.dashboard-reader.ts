import type { Client } from '@temporalio/client';
import {
  pendingFromDescribe,
  TemporalWorkflowReader,
  type PendingActivity,
  type WorkflowInspection,
} from './workspace.temporal';

export interface OpenWorkflow {
  workflowId: string;
  startedAtMs: number;
}

export type WorkflowStatus = 'running' | 'closed' | 'not_found';

/**
 * What the dashboard needs from Temporal. Read only, like everything under
 * `src/workspace`. Kept apart from `TemporalReader`: the workspace page lists a
 * capped, cached set of open workflows, while the dashboard must see every open
 * localization workflow (including the Vbee dubbing one) and must never evict
 * what the workspace has cached.
 */
export interface DashboardTemporal {
  /** Every running localization workflow, paged to the end. Throws when Temporal is unreadable. */
  listOpenLocalizationIds(): Promise<OpenWorkflow[]>;
  /** One workflow by id. Throws when Temporal is unreadable, so callers never read "unknown" as "closed". */
  workflowStatus(workflowId: string): Promise<WorkflowStatus>;
  /** The activities a running workflow is executing right now; empty when it is between activities. */
  pendingActivities(workflowId: string): Promise<PendingActivity[]>;
  /** Full activity history, for the video detail timeline; `null` when no workflow has this id. */
  inspect(workflowId: string): Promise<WorkflowInspection | null>;
}

/** Workflows the starter launches with a `video-<aweme>-g<gen>` id. */
const OPEN_LOCALIZATION_QUERY =
  'ExecutionStatus = "Running" AND (WorkflowType = "VideoLocalizationWorkflow" OR WorkflowType = "VbeeDubbingWorkflow")';

const MAX_CACHED_DETAILS = 200;

function isNotFound(err: unknown): boolean {
  return err instanceof Error && err.name === 'WorkflowNotFoundError';
}

export class TemporalDashboardReader implements DashboardTemporal {
  /** Its own history cache: nothing here touches the workspace reader's. */
  private detail: TemporalWorkflowReader;
  private inspected = 0;

  constructor(private readonly client: Client) {
    this.detail = new TemporalWorkflowReader(client);
  }

  async listOpenLocalizationIds(): Promise<OpenWorkflow[]> {
    const open: OpenWorkflow[] = [];
    for await (const info of this.client.workflow.list({ query: OPEN_LOCALIZATION_QUERY })) {
      open.push({ workflowId: info.workflowId, startedAtMs: info.startTime.getTime() });
    }
    return open;
  }

  async workflowStatus(workflowId: string): Promise<WorkflowStatus> {
    try {
      const description = await this.client.workflow.getHandle(workflowId).describe();
      return description.status.name === 'RUNNING' ? 'running' : 'closed';
    } catch (err) {
      if (isNotFound(err)) return 'not_found';
      throw err;
    }
  }

  async pendingActivities(workflowId: string): Promise<PendingActivity[]> {
    try {
      const description = await this.client.workflow.getHandle(workflowId).describe();
      return pendingFromDescribe(description.raw.pendingActivities);
    } catch (err) {
      if (isNotFound(err)) return [];
      throw err;
    }
  }

  async inspect(workflowId: string): Promise<WorkflowInspection | null> {
    // The wrapped reader only evicts inside its own listOpen, which is never called here,
    // so bound the cache by starting over once it has seen a lot of workflows.
    if (++this.inspected > MAX_CACHED_DETAILS) {
      this.detail = new TemporalWorkflowReader(this.client);
      this.inspected = 0;
    }
    return this.detail.find(workflowId);
  }
}
