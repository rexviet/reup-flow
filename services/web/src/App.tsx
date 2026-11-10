import { useCallback, useEffect, useState } from 'react';
import { cancelJob, createJob, getCookieStatus, listJobs, retryJob } from './api/client';
import { CookieBadge } from './components/CookieBadge';
import { JobCard } from './components/JobCard';
import { JobForm } from './components/JobForm';
import { usePolling } from './hooks/usePolling';
import { WorkspacePage } from './workspace/WorkspacePage';

import { ReviewProjectsPage } from './reviews/ReviewProjectsPage';

const POLL_INTERVAL_MS = 1000; // §8.1: "Polling 1s"

type Tab = 'jobs' | 'workspace' | 'reviews';
const TAB_LABELS: Record<Tab, string> = { jobs: 'Jobs', workspace: 'Workspace', reviews: 'Review' };

function tabFromHash(): Tab {
  return window.location.hash === '#reviews' ? 'reviews' : window.location.hash === '#workspace' ? 'workspace' : 'jobs';
}

export default function App() {
  const [tab, setTab] = useState<Tab>(tabFromHash);

  useEffect(() => {
    const onHash = () => setTab(tabFromHash());
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  function selectTab(next: Tab) {
    setTab(next);
    window.location.hash = next === 'jobs' ? '' : next;
  }

  return (
    <div className={tab === 'workspace' ? 'fixed inset-0 overflow-hidden bg-stone-100' : 'mx-auto max-w-3xl px-4 py-6'}>
      <nav aria-label="Trang" className={tab === 'workspace' ? 'absolute left-3 top-3 z-20 flex gap-1 rounded-lg border border-gray-300 bg-white px-1' : 'mb-4 flex gap-1 border-b border-gray-200'}>
        {(Object.keys(TAB_LABELS) as Tab[]).map((t) => (
          <button
            key={t}
            type="button"
            aria-current={tab === t ? 'page' : undefined}
            onClick={() => selectTab(t)}
            className={`min-h-11 -mb-px border-b-2 px-3 py-2 text-sm ${
              tab === t ? 'border-blue-600 font-medium text-gray-900' : 'border-transparent text-gray-500 hover:text-gray-800'
            }`}
          >
            {TAB_LABELS[t]}
          </button>
        ))}
      </nav>
      {tab === 'workspace' ? <WorkspacePage /> : tab === 'reviews' ? <ReviewProjectsPage /> : <JobsPage />}
    </div>
  );
}

function JobsPage() {
  const cookieFetcher = useCallback(() => getCookieStatus(), []);
  const jobsFetcher = useCallback(() => listJobs(100, 0), []);

  const cookie = usePolling(cookieFetcher, POLL_INTERVAL_MS);
  const jobs = usePolling(jobsFetcher, POLL_INTERVAL_MS);

  const [pendingActionIds, setPendingActionIds] = useState<Set<string>>(new Set());

  function withPending(id: string, run: () => Promise<unknown>) {
    setPendingActionIds((prev) => new Set(prev).add(id));
    void run().finally(() => {
      setPendingActionIds((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    });
  }

  async function handleCreate(url: string) {
    await createJob(url);
    // Next 1s poll tick picks up the new job -- no optimistic row inserted
    // here, so the list is always exactly what the server has.
  }

  function handleCancel(id: string) {
    withPending(id, () => cancelJob(id));
  }

  function handleRetry(id: string) {
    withPending(id, () => retryJob(id));
  }

  const items = jobs.data?.items ?? [];

  return (
    <div>
      <header className="mb-6 flex items-center justify-between gap-4">
        <h1 className="text-lg font-semibold text-gray-900">Douyin Downloader</h1>
        {/* §8.3: always visible, never in a menu. */}
        <CookieBadge status={cookie.data} loading={cookie.loading} />
      </header>

      <div className="mb-6">
        <JobForm onSubmit={handleCreate} />
      </div>

      <div className="flex flex-col gap-3">
        {items.length === 0 ? (
          <p className="text-sm text-gray-500">Chưa có job nào.</p>
        ) : (
          items.map((job) => (
            <JobCard
              key={job.job_id}
              job={job}
              onCancel={handleCancel}
              onRetry={handleRetry}
              actionPending={pendingActionIds.has(job.job_id)}
            />
          ))
        )}
      </div>
    </div>
  );
}
