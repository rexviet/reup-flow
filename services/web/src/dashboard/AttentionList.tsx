import type { AttentionResponse, DashboardActionName, VideoListItem } from '../api/types';
import { Icon } from '../components/Icon';
import { ACTION_LABELS } from './format';

/** The one action that fixes each kind of concern, when there is one. */
const SUGGESTED: Record<string, DashboardActionName | undefined> = {
  dispatch_dead: 'rerun',
  run_failed: 'rerun',
  lost: 'rerun',
  render_failed: 'rerender',
  // The review changed after approval: rendering again cannot help, it needs a look and a new approval.
  render_stale: 'open_studio',
  needs_human: 'open_studio',
  mismatch: 'open_studio',
  dirty: 'open_studio',
};

export interface AttentionListProps {
  data: AttentionResponse | null;
  busy: boolean;
  retryingJobs?: ReadonlySet<string>;
  onAction: (action: DashboardActionName, item: VideoListItem) => void;
  onRetryJob: (jobId: string) => void;
}

export function AttentionList({ data, busy, retryingJobs, onAction, onRetryJob }: AttentionListProps) {
  const jobs = data?.jobs ?? [];
  const items = data?.items ?? [];
  const total = jobs.length + items.length;
  return (
    <section aria-labelledby="attention-title" className="card flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <span aria-hidden="true" className={`h-2 w-2 rounded-full ${total > 0 ? 'bg-health-down motion-safe:animate-pulse' : 'bg-health-ok'}`} />
        <h2 id="attention-title" className="card-title">
          Cần xử lý khẩn cấp
        </h2>
        <span
          className={`rounded px-1.5 py-0.5 font-mono text-[11px] font-bold ${
            total > 0 ? 'bg-status-error-bg text-status-error-text' : 'bg-status-success-bg text-status-success-text'
          }`}
        >
          {total} mục
        </span>
      </div>
      {total === 0 ? (
        <p className="flex items-center gap-1.5 text-sm text-status-success-text">
          <Icon name="check-circle" size={16} />
          Không có gì cần xử lý.
        </p>
      ) : (
        <ul className="flex max-h-[340px] flex-col gap-1 overflow-y-auto pr-0.5">
          {jobs.map((j) => (
            <li
              key={j.id}
              data-testid="attention-job"
              className="flex flex-col gap-2 rounded bg-surface-container-low p-2 transition-colors hover:bg-surface-container md:flex-row md:items-center md:justify-between"
            >
              <div className="flex min-w-0 items-start gap-2">
                <Icon name="download" size={18} className="mt-0.5 text-status-error-text" />
                <div className="min-w-0">
                  <p className="truncate font-mono text-xs font-bold text-text-primary">Job {j.channel ?? j.sec_user_id}</p>
                  <p className="text-xs text-text-secondary">
                    {j.reason} {j.action}
                  </p>
                </div>
              </div>
              <button
                type="button"
                disabled={busy || retryingJobs?.has(j.id) === true}
                onClick={() => onRetryJob(j.id)}
                className="btn btn-neutral shrink-0"
              >
                <Icon name="refresh" />
                Thử lại job
              </button>
            </li>
          ))}
          {items.map((item) => {
            const suggested = item.attention.map((a) => SUGGESTED[a.code]).find((a) => a !== undefined);
            return (
              <li
                key={item.aweme_id}
                data-testid="attention-item"
                className="flex flex-col gap-2 rounded bg-surface-container-low p-2 transition-colors hover:bg-surface-container md:flex-row md:items-center md:justify-between"
              >
                <div className="flex min-w-0 items-start gap-2">
                  <Icon name="warning" size={18} className="mt-0.5 text-status-warning-text" />
                  <div className="min-w-0">
                    <a href={`#/video/${item.aweme_id}`} className="font-mono text-xs font-bold text-secondary hover:underline">
                      {item.aweme_id}
                    </a>
                    <span className="ml-2 font-mono text-[11px] text-text-muted">{item.channel ?? ''}</span>
                    {item.attention.map((a) => (
                      <p key={a.code} className="text-xs text-text-secondary">
                        {a.reason} <span className="text-text-muted">{a.action}</span>
                      </p>
                    ))}
                  </div>
                </div>
                <div className="flex shrink-0 gap-2">
                  {suggested ? (
                    <button type="button" disabled={busy} onClick={() => onAction(suggested, item)} className="btn btn-neutral">
                      {ACTION_LABELS[suggested]}
                    </button>
                  ) : null}
                  <a href={`#/video/${item.aweme_id}`} className="btn btn-ghost">
                    Chi tiết
                  </a>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
