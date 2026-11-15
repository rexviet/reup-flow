import { CANCELLABLE_STATUSES, RETRYABLE_STATUSES, type JobResponse } from '../api/types';
import { ProgressBars } from './ProgressBars';

export interface JobCardProps {
  job: JobResponse;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
  /** Disables both buttons while a cancel/retry request for THIS job is in flight. */
  actionPending?: boolean;
}

const STATUS_STYLES: Record<string, string> = {
  pending: 'bg-status-idle-bg text-status-idle-text',
  enumerating: 'bg-status-active-bg text-status-active-text',
  downloading: 'bg-status-active-bg text-status-active-text',
  blocked: 'bg-status-error-bg text-status-error-text',
  failed: 'bg-status-error-bg text-status-error-text',
  cancelled: 'bg-status-idle-bg text-text-muted',
  completed: 'bg-status-success-bg text-status-success-text',
};

export function JobCard({ job, onCancel, onRetry, actionPending }: JobCardProps) {
  const canCancel = (CANCELLABLE_STATUSES as string[]).includes(job.status);
  const canRetry = (RETRYABLE_STATUSES as string[]).includes(job.status);

  return (
    <div className="rounded bg-surface-container-low p-3" data-testid="job-card" data-job-id={job.job_id}>
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="truncate font-mono text-xs font-semibold text-text-primary" title={job.sec_user_id}>
          {job.sec_user_id}
        </span>
        <span className={`rounded px-1.5 py-0.5 font-mono text-[11px] font-semibold ${STATUS_STYLES[job.status] ?? 'bg-status-idle-bg text-status-idle-text'}`}>
          {job.status}
        </span>
      </div>

      <ProgressBars job={job} />

      {job.error ? (
        <p className="mt-2 rounded bg-status-error-bg p-2 text-xs text-status-error-text" data-testid="job-error">
          {job.error}
        </p>
      ) : null}

      <div className="mt-3 flex gap-2">
        {canCancel ? (
          <button
            type="button"
            className="btn btn-ghost"
            disabled={actionPending}
            onClick={() => onCancel(job.job_id)}
          >
            Hủy
          </button>
        ) : null}
        {canRetry ? (
          <button
            type="button"
            className="btn btn-neutral"
            disabled={actionPending}
            onClick={() => onRetry(job.job_id)}
          >
            Thử lại
          </button>
        ) : null}
      </div>
    </div>
  );
}
