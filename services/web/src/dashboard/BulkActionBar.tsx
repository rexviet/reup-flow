import { useState } from 'react';
import type { CollectionItem, DashboardActionName, VideoListItem } from '../api/types';
import { Icon, type IconName } from '../components/Icon';
import { ACTION_LABELS, ACTIONS_BY_STAGE, COLLECTION_KIND_LABELS, FINAL_STAGES } from './format';

const BULK_ACTIONS: DashboardActionName[] = ['retry_download', 'localize', 'resume', 'rerun', 'approve', 'rerender', 'mark_published'];

const ACTION_ICONS: Partial<Record<DashboardActionName, IconName>> = {
  retry_download: 'refresh',
  resume: 'play',
  localize: 'play',
  rerun: 'refresh',
  approve: 'check-circle',
  rerender: 'video',
  mark_published: 'upload',
};

const ACTION_HINTS: Partial<Record<DashboardActionName, string>> = {
  resume: 'Giữ các bước đã xong, chạy lại từ bước bị lỗi.',
  rerun: 'Chạy lại từ đầu, tạo một lượt xử lý mới.',
};

export interface BulkActionBarProps {
  selected: VideoListItem[];
  busy: boolean;
  onRun: (action: DashboardActionName) => void;
  onClear: () => void;
  collections: CollectionItem[];
  onMove: (collectionId: string) => void;
}

/**
 * "Lưu vào thư mục" for the selected videos that have a final. It is off only when every one of
 * them sits in a collection known to have no folder; the server skips the others one by one with
 * a reason. A bulk save names no generation, so each video saves its current one.
 */
function saveState(selected: VideoListItem[], collections: CollectionItem[]): { shown: boolean; disabled: boolean; hint?: string } {
  const finals = selected.filter((v) => FINAL_STAGES.includes(v.stage));
  if (finals.length === 0) return { shown: false, disabled: true };
  const byId = new Map(collections.map((c) => [c.id, c]));
  const homeless = finals.filter((v) => {
    const c = byId.get(v.collection.id);
    return c !== undefined && c.save_subdir === null;
  });
  if (homeless.length === 0) return { shown: true, disabled: false };
  const names = [...new Set(homeless.map((v) => `«${v.collection.name}»`))].join(', ');
  const hint = `Collection ${names} chưa có thư mục lưu; đặt thư mục trong panel Collections.`;
  return homeless.length === finals.length
    ? { shown: true, disabled: true, hint }
    : { shown: true, disabled: false, hint: `${homeless.length} video sẽ bị bỏ qua: ${hint}` };
}

/** An action is offered when at least one selected video is at a stage where it makes sense. */
function offered(selected: VideoListItem[], action: DashboardActionName): boolean {
  return selected.some((v) => ACTIONS_BY_STAGE[v.stage]?.includes(action));
}

/**
 * Only the actions that fit the stages of the selected videos are shown (a failed download gets
 * "Tải lại", not "Duyệt"); the server still checks each video and reports the ones it skips.
 * `approve` binds one revision to one video, so it is offered for a single selected video only;
 * a bulk approve would approve revisions the admin never looked at.
 */
export function BulkActionBar({ selected, busy, onRun, onClear, collections, onMove }: BulkActionBarProps) {
  const [target, setTarget] = useState('');
  if (selected.length === 0) return null;
  const single = selected.length === 1 ? selected[0] : null;
  const save = saveState(selected, collections);
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
        {BULK_ACTIONS.filter((action) => offered(selected, action)).map((action) => {
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
                    : ACTION_HINTS[action]
              }
              onClick={() => onRun(action)}
              className={`btn ${action === 'mark_published' ? 'btn-success' : 'btn-neutral'}`}
            >
              <Icon name={ACTION_ICONS[action] ?? 'circle'} />
              {ACTION_LABELS[action]}
            </button>
          );
        })}
        {save.shown ? (
          <>
            <button type="button" disabled={busy || save.disabled} title={save.hint} onClick={() => onRun('save_to_folder')} className="btn btn-neutral">
              <Icon name="download" />
              {ACTION_LABELS.save_to_folder}
            </button>
            {save.hint ? (
              <span data-testid="bulk-save-hint" className="max-w-xs text-xs text-status-warning-text">
                {save.hint}
              </span>
            ) : null}
          </>
        ) : null}
        <label htmlFor="bulk-collection" className="sr-only">
          Collection đích
        </label>
        <select id="bulk-collection" value={target} onChange={(e) => setTarget(e.target.value)} className="field w-auto max-w-[14rem]">
          <option value="">Chọn collection…</option>
          {collections.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name} ({COLLECTION_KIND_LABELS[c.kind] ?? c.kind})
            </option>
          ))}
        </select>
        <button
          type="button"
          disabled={busy || target === ''}
          onClick={() => {
            onMove(target);
            setTarget('');
          }}
          className="btn btn-neutral"
        >
          <Icon name="tag" />
          Chuyển collection
        </button>
        <button type="button" onClick={onClear} className="btn btn-ghost md:ml-1">
          Bỏ chọn
        </button>
      </div>
    </div>
  );
}
