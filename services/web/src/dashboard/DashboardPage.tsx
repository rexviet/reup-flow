import { useCallback, useMemo, useState } from 'react';
import { getCookieStatus, retryJob } from '../api/client';
import type { DashboardActionName, VideoListQuery } from '../api/types';
import { usePolling } from '../hooks/usePolling';
import { ActionNotice } from './ActionNotice';
import { AttentionList } from './AttentionList';
import { AutoQueueSwitch } from './AutoQueueSwitch';
import { BulkActionBar } from './BulkActionBar';
import { ChannelsPanel } from './ChannelsPanel';
import { HealthBar } from './HealthBar';
import { EMPTY_FILTERS, PAGE_SIZE, PipelineTable, type PipelineFilters } from './PipelineTable';
import { useAttention, useChannels, useHealth, useJobs, useVideoList } from './useDashboard';
import { useVideoActions } from './useVideoActions';

function queryOf(f: PipelineFilters): VideoListQuery {
  return {
    ...(f.stage ? { stage: f.stage } : {}),
    ...(f.q ? { q: f.q } : {}),
    ...(f.channel ? { channel: f.channel } : {}),
    ...(f.from ? { from: f.from } : {}),
    ...(f.to ? { to: f.to } : {}),
    limit: PAGE_SIZE,
    offset: f.offset,
  };
}

export function DashboardPage() {
  const [filters, setFilters] = useState<PipelineFilters>(EMPTY_FILTERS);
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());

  const health = useHealth();
  const list = useVideoList(useMemo(() => queryOf(filters), [filters]));
  const attention = useAttention();
  const channels = useChannels();
  const jobs = useJobs();
  const cookieFetcher = useCallback(() => getCookieStatus(), []);
  const cookie = usePolling(cookieFetcher, 10_000, { pauseWhenHidden: true });

  const refreshList = list.refresh;
  const refreshAttention = attention.refresh;
  const refreshChannels = channels.refresh;
  const refreshJobs = jobs.refresh;
  const refreshAll = useCallback(() => {
    refreshList();
    refreshAttention();
    refreshChannels();
    refreshJobs();
  }, [refreshList, refreshAttention, refreshChannels, refreshJobs]);
  const [retryingJobs, setRetryingJobs] = useState<ReadonlySet<string>>(new Set());
  const [jobError, setJobError] = useState<string | null>(null);
  const actions = useVideoActions(refreshAll);

  const items = list.data?.items ?? [];
  const selectedItems = items.filter((i) => selected.has(i.aweme_id));

  function retryBlockedJob(id: string) {
    setJobError(null);
    setRetryingJobs((prev) => new Set(prev).add(id));
    void retryJob(id)
      .catch((err: unknown) => setJobError(err instanceof Error ? err.message : String(err)))
      .finally(() => {
        setRetryingJobs((prev) => {
          const next = new Set(prev);
          next.delete(id);
          return next;
        });
        refreshAll();
      });
  }

  function toggle(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  }

  function toggleAll(ids: string[], on: boolean) {
    setSelected((prev) => {
      const next = new Set(prev);
      for (const id of ids) {
        if (on) next.add(id);
        else next.delete(id);
      }
      return next;
    });
  }

  async function runBulk(action: DashboardActionName) {
    const res = await actions.perform(action, selectedItems);
    // Videos that went through leave the selection; the skipped ones stay so the admin can see which.
    if (res) {
      const done = new Set(res.done);
      setSelected((prev) => new Set([...prev].filter((id) => !done.has(id))));
    }
  }

  return (
    <div className="flex flex-col gap-3 sm:gap-4">
      <header className="flex flex-col gap-3">
        <h1 className="text-xl font-bold tracking-tight text-text-primary">Bảng điều khiển</h1>
        <HealthBar health={health.data} error={health.error} />
        <AutoQueueSwitch />
      </header>

      <AttentionList
        data={attention.data}
        busy={actions.runner.busy}
        retryingJobs={retryingJobs}
        onAction={(action, item) => void actions.perform(action, [item])}
        onRetryJob={retryBlockedJob}
      />
      {jobError ? (
        <p role="alert" className="text-xs text-status-error-text">
          Không thử lại được job: {jobError}
        </p>
      ) : null}

      <ActionNotice runner={actions.runner} />

      <PipelineTable
        data={list.data}
        error={list.error}
        filters={filters}
        onFiltersChange={(next) => {
          setFilters(next);
          setSelected(new Set());
        }}
        channels={channels.data?.items ?? []}
        selected={selected}
        onToggle={toggle}
        onToggleAll={toggleAll}
      />

      <BulkActionBar selected={selectedItems} busy={actions.runner.busy} onRun={(a) => void runBulk(a)} onClear={() => setSelected(new Set())} />

      <ChannelsPanel cookie={cookie.data} cookieLoading={cookie.loading} jobs={jobs.data} channels={channels.data} onChanged={refreshAll} />
      {actions.dialog}
    </div>
  );
}
