import type { DashboardActionName, VideoListItem } from '../api/types';
import { Icon, type IconName } from '../components/Icon';
import { ACTION_LABELS } from './format';

const BULK_ACTIONS: DashboardActionName[] = ['localize', 'rerun', 'approve', 'rerender', 'mark_published'];

const ACTION_ICONS: Partial<Record<DashboardActionName, IconName>> = {
  localize: 'play',
  rerun: 'refresh',
  approve: 'check-circle',
  rerender: 'video',
  mark_published: 'upload',
};

export interface BulkActionBarProps {
  selected: VideoListItem[];
  busy: boolean;
  onRun: (action: DashboardActionName) => void;
  onClear: () => void;
}

/**
 * `approve` binds one revision to one video, so it is offered for a single selected video only;
 * a bulk approve would approve revisions the admin never looked at.
 */
export function BulkActionBar({ selected, busy, onRun, onClear }: BulkActionBarProps) {
  if (selected.length === 0) return null;
  const single = selected.length === 1 ? selected[0] : null;
  return (
    <div
      role="toolbar"
      aria-label="Thao tác hàng loạt"
      className="sticky bottom-2 z-10 flex flex-col gap-2 rounded border-l-4 border-l-primary bg-surface-layer-2 p-2 shadow-lg ring-1 ring-border-prominent md:flex-row md:items-center md:justify-between"
    >
      <div className="flex items-center gap-2">
        <span aria-hidden="true" className="flex h-6 min-w-6 items-center justify-center rounded bg-primary-container px-1 font-mono text-[11px] font-bold text-on-primary-container">
          {selected.length}
        </span>
        <span className="text-sm font-medium text-text-primary">Đã chọn {selected.length} video</span>
      </div>
      <div className="flex flex-wrap items-center gap-1.5">
        {BULK_ACTIONS.map((action) => {
          const needsOne = action === 'approve' && single === null;
          const noRevision = action === 'approve' && single !== null && single.revision === null;
          const disabled = busy || needsOne || noRevision;
          return (
            <button
              key={action}
              type="button"
              disabled={disabled}
              title={
                needsOne
                  ? 'Duyệt chỉ áp dụng cho một video mỗi lần.'
                  : noRevision
                    ? 'Video chưa có bản review để duyệt.'
                    : undefined
              }
              onClick={() => onRun(action)}
              className={`btn ${action === 'mark_published' ? 'btn-success' : 'btn-neutral'}`}
            >
              <Icon name={ACTION_ICONS[action] ?? 'circle'} />
              {ACTION_LABELS[action]}
            </button>
          );
        })}
        <button type="button" onClick={onClear} className="btn btn-ghost md:ml-1">
          Bỏ chọn
        </button>
      </div>
    </div>
  );
}
