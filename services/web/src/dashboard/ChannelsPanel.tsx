import { useState } from 'react';
import { cancelJob, createJob, createVideo, retryJob, setAutoLocalize } from '../api/client';
import type { ChannelListResponse, CollectionItem, CookieStatus, JobListResponse } from '../api/types';
import { CookieBadge } from '../components/CookieBadge';
import { JobCard } from '../components/JobCard';
import { isVideoUrl, JobForm, type JobFormOptions } from '../components/JobForm';
import { formatDateTime } from './format';

export interface ChannelsPanelProps {
  cookie: CookieStatus | null;
  cookieLoading: boolean;
  jobs: JobListResponse | null;
  channels: ChannelListResponse | null;
  collections: CollectionItem[];
  onChanged: () => void;
}

export function ChannelsPanel({ cookie, cookieLoading, jobs, channels, collections, onChanged }: ChannelsPanelProps) {
  const [pendingIds, setPendingIds] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);

  function withPending(id: string, run: () => Promise<unknown>) {
    setPendingIds((prev) => new Set(prev).add(id));
    setError(null);
    void run()
      .then(onChanged)
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() =>
        setPendingIds((prev) => {
          const next = new Set(prev);
          next.delete(id);
          return next;
        }),
      );
  }

  async function handleCreate(url: string, options: JobFormOptions) {
    if (isVideoUrl(url)) {
      // One video: "Localize ngay" decides whether it is held after the download.
      // Unchecked follows the channel's switch (omitted); it does not force a hold.
      await createVideo(url, options.localizeNow ? true : undefined, options.collectionId);
    } else {
      // Unchecked leaves the channel's current switch alone instead of turning it off.
      await createJob(url, options.autoLocalize ? true : undefined, options.collectionId);
    }
    onChanged();
  }

  return (
    <div className="grid grid-cols-1 gap-3 sm:gap-4 xl:grid-cols-12">
      <section aria-labelledby="download-title" className="card flex min-w-0 flex-col gap-3 xl:col-span-5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 id="download-title" className="card-title">
            Tải link mới
          </h2>
          <CookieBadge status={cookie} loading={cookieLoading} />
        </div>
        <JobForm onSubmit={handleCreate} collections={collections} />
        {error ? (
          <p role="alert" className="text-xs text-status-error-text">
            {error}
          </p>
        ) : null}
      </section>

      <div className="flex min-w-0 flex-col gap-3 sm:gap-4 xl:col-span-7">
        <section aria-labelledby="jobs-title" className="card flex flex-col gap-3">
          <h2 id="jobs-title" className="card-title">
            Job tải gần đây
          </h2>
          {(jobs?.items ?? []).length === 0 ? (
            <p className="text-sm text-text-secondary">Chưa có job nào.</p>
          ) : (
            <div className="flex flex-col gap-2">
              {(jobs?.items ?? []).map((job) => (
                <JobCard
                  key={job.job_id}
                  job={job}
                  onCancel={(id) => withPending(id, () => cancelJob(id))}
                  onRetry={(id) => withPending(id, () => retryJob(id))}
                  actionPending={pendingIds.has(job.job_id)}
                />
              ))}
            </div>
          )}
        </section>

        <section aria-labelledby="channels-title" className="card flex flex-col gap-3">
          <h2 id="channels-title" className="card-title">
            Kênh đã theo dõi
          </h2>
          {channels && channels.items.length > 0 ? (
            <ul className="divide-y divide-border-subtle/60">
              {channels.items.map((c) => (
                <li key={c.sec_user_id} data-testid="channel-row" className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
                  <div className="min-w-0">
                    <p className="break-all font-medium text-text-primary">{c.nickname ?? c.sec_user_id}</p>
                    <p className="font-mono text-xs text-text-muted">
                      {c.total} video, đồng bộ {formatDateTime(c.last_synced_at)}
                    </p>
                  </div>
                  <label className="inline-flex min-h-11 items-center gap-2 text-xs text-text-secondary">
                    <input
                      type="checkbox"
                      className="h-5 w-5"
                      checked={c.auto_localize}
                      disabled={pendingIds.has(c.sec_user_id)}
                      onChange={(e) => withPending(c.sec_user_id, () => setAutoLocalize(c.sec_user_id, e.target.checked))}
                    />
                    Tự localize
                  </label>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-text-secondary">Chưa có kênh nào.</p>
          )}
        </section>
      </div>
    </div>
  );
}
