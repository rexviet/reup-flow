import { useCallback, useState } from 'react';
import { getRunDetail } from '../api/client';
import { usePolling } from '../hooks/usePolling';
import { OfficeMap, type OfficeMapProps } from './OfficeMap';
import { activeRuns } from './RunList';
import { deskLabel } from './workspace.layout';
import { useEventFeed } from './useEventFeed';
import { useWorkspaceSnapshot } from './useWorkspaceSnapshot';

/** Poll the selected video's state without rendering a separate detail panel. */
function FocusedOffice({ selected, ...props }: OfficeMapProps & {
  selected: { awemeId: string; generation: number };
}) {
  const { awemeId, generation } = selected;
  const fetcher = useCallback(() => getRunDetail(awemeId, generation), [awemeId, generation]);
  const { data, error } = usePolling(fetcher, 2000, { pauseWhenHidden: true });
  const detail = data && error ? { ...data, sources: { ...data.sources, temporal: 'down' as const } } : data;
  return <OfficeMap {...props} selected={selected} detail={detail} />;
}

export function WorkspacePage() {
  const { snapshot, loading, staleSeconds } = useWorkspaceSnapshot();
  const feed = useEventFeed(snapshot);
  const runs = activeRuns(snapshot).slice().sort((a, b) => a.awemeId.localeCompare(b.awemeId));
  const [selection, setSelection] = useState<string>();
  const key = (run: { awemeId: string; generation: number }) => `${run.awemeId}:${run.generation}`;
  const value = selection === undefined ? (runs[0] ? key(runs[0]) : '') : selection;
  const focus = runs.find((run) => key(run) === value) ?? null;
  const status = !snapshot ? (loading ? 'Đang kết nối…' : 'Không có dữ liệu') :
    staleSeconds > 0 ? 'Dữ liệu cũ' : snapshot.sources.temporal === 'down' ? 'Mất kết nối' : 'Trực tiếp';
  const office: OfficeMapProps = {
    snapshot, bubbles: feed.bubbles, handoffs: feed.handoffs, fullscreen: true,
    onSelect: (awemeId, generation) => setSelection(key({ awemeId, generation })),
  };

  return (
    <main aria-label="Workspace" className="relative h-full w-full">
      {focus ? <FocusedOffice key={key(focus)} {...office} selected={focus} /> : <OfficeMap {...office} />}
      <section aria-label="Video đang chạy" className="absolute right-3 top-16 z-10 max-w-[calc(100%-1.5rem)] sm:top-3 sm:max-w-sm">
        <label htmlFor="workspace-video" className="sr-only">Video đang theo dõi</label>
        <select id="workspace-video" value={focus ? value : ''} onChange={(e) => setSelection(e.target.value)}
          className="min-h-11 max-w-full rounded-lg border border-gray-300 bg-white px-3 text-sm text-gray-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-600">
          <option value="">Tất cả video ({runs.length})</option>
          {runs.map((run) => <option key={key(run)} value={key(run)}>Video …{run.awemeId.slice(-8)} · {run.phase === 'starting' ? 'đang chờ' : deskLabel(run.phase)}</option>)}
        </select>
      </section>
      <div role="status" className="pointer-events-none absolute bottom-3 left-3 rounded-lg border border-gray-200 bg-white px-3 py-2 text-xs text-gray-700">
        <span aria-hidden="true" className={`mr-2 inline-block h-2 w-2 rounded-full ${status === 'Trực tiếp' ? 'bg-green-600' : 'bg-amber-600'}`} />{status}
      </div>
    </main>
  );
}
