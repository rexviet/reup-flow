import type { WorkspaceSnapshot } from '../api/types';
import { ALL_DESKS, deskLabel } from './workspace.layout';
import { videoColor } from './videoColor';

export interface RunSelection { awemeId: string; generation: number }

export function activeRuns(snapshot: WorkspaceSnapshot | null) {
  return (snapshot?.runs ?? []).filter((r) => r.phase === 'starting' || ALL_DESKS.some((d) => d.id === r.phase));
}

export function RunList({ snapshot, selected, onSelect }: {
  snapshot: WorkspaceSnapshot | null;
  selected: RunSelection | null;
  onSelect: (awemeId: string, generation: number) => void;
}) {
  const runs = activeRuns(snapshot);
  return (
    <section aria-label="Video đang chạy" className="rounded-lg border border-gray-200 bg-white p-4">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="font-semibold text-gray-900">Video đang chạy <span className="text-gray-500">({runs.length})</span></h2>
        <p className="text-sm text-gray-600">Chọn video để theo dõi đường đi của nó.</p>
      </div>
      {snapshot?.sources.temporal === 'down' ? <p className="text-sm text-amber-800">Không đọc được Temporal, chưa xác định được video đang chạy.</p> : runs.length === 0 ? (
        <p className="text-sm text-gray-600">{snapshot ? 'Chưa có video đang chạy.' : 'Đang tải video…'}</p>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {runs.map((r) => {
            const chosen = r.awemeId === selected?.awemeId && r.generation === selected.generation;
            return <button key={`${r.awemeId}:${r.generation}`} type="button" aria-pressed={chosen}
              onClick={() => onSelect(r.awemeId, r.generation)}
              className={`min-h-20 rounded-lg border p-3 text-left focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-600 ${chosen ? 'border-blue-600 bg-blue-50' : 'border-gray-200 hover:bg-gray-50'}`}>
              <span className="flex items-center gap-2 font-semibold text-gray-900"><span className="h-3 w-3 shrink-0 rounded-full" style={{ background: videoColor(r.awemeId) }} />Video …{r.awemeId.slice(-8)}<span className="text-xs font-normal text-gray-600">g{r.generation}</span></span>
              <span className="mt-1 block text-sm text-gray-700">{r.phase === 'starting' ? 'Đang chờ bước tiếp theo' : `Đang ở ${deskLabel(r.phase)}`}</span>
            </button>;
          })}
        </div>
      )}
    </section>
  );
}
