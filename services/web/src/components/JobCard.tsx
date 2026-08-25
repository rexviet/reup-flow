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
  pending: 'bg-gray-100 text-gray-700',
  enumerating: 'bg-blue-100 text-blue-700',
  downloading: 'bg-blue-100 text-blue-700',
  blocked: 'bg-red-100 text-red-700',
  failed: 'bg-red-100 text-red-700',
  cancelled: 'bg-gray-100 text-gray-500',
  completed: 'bg-green-100 text-green-700',
};

export function JobCard({ job, onCancel, onRetry, actionPending }: JobCardProps) {
  const canCancel = (CANCELLABLE_STATUSES as string[]).includes(job.status);
  const canRetry = (RETRYABLE_STATUSES as string[]).includes(job.status);

  return (
    <div className="rounded-lg border border-gray-200 p-4 shadow-sm" data-testid="job-card" data-job-id={job.job_id}>
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="truncate font-mono text-sm text-gray-800" title={job.sec_user_id}>
          {job.sec_user_id}
        </span>
        <span className={`rounded px-2 py-0.5 text-xs font-medium ${STATUS_STYLES[job.status] ?? 'bg-gray-100 text-gray-700'}`}>
          {job.status}
        </span>
      </div>

      <ProgressBars job={job} />

      {job.error ? (
        <p className="mt-2 rounded bg-red-50 p-2 text-xs text-red-800" data-testid="job-error">
          {job.error}
        </p>
      ) : null}

      <div className="mt-3 flex gap-2">
        {canCancel ? (
          <button
            type="button"
            className="rounded bg-white px-2 py-1 text-xs font-medium text-gray-700 ring-1 ring-inset ring-gray-300 hover:bg-gray-50 disabled:opacity-50"
            disabled={actionPending}
            onClick={() => onCancel(job.job_id)}
          >
            Hủy
          </button>
        ) : null}
        {canRetry ? (
          <button
            type="button"
            className="rounded bg-white px-2 py-1 text-xs font-medium text-blue-700 ring-1 ring-inset ring-blue-300 hover:bg-blue-50 disabled:opacity-50"
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
