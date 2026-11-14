import { useEffect, useState } from 'react';
import type { ChannelItem, CollectionItem, Stage, VideoListItem, VideoListResponse } from '../api/types';
import { STAGES } from '../api/types';
import { Icon } from '../components/Icon';
import {
  COLLECTION_KIND_LABELS,
  formatDateTime,
  formatDuration,
  relativeTime,
  STAGE_LABELS,
  STAGE_TONES,
  TONE_CLASSES,
  TONE_ICONS,
} from './format';
import { RenderedGrid } from './VideoGrid';
import { StageBadge } from './StageBadge';
import { useThumbLinks } from './useMediaLinks';
import { SmallThumb } from './VideoThumb';

export interface PipelineFilters {
  stage?: Stage;
  q: string;
  channel: string;
  /** A collection id, or '' for every collection. */
  collection: string;
  from: string;
  to: string;
  offset: number;
}

export const EMPTY_FILTERS: PipelineFilters = { q: '', channel: '', collection: '', from: '', to: '', offset: 0 };
export const PAGE_SIZE = 50;

/** Delays a fast-changing text value so typing in the search box does not send a request per key. */
function useDebounced<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return debounced;
}

export interface PipelineTableProps {
  data: VideoListResponse | null;
  error: Error | null;
  filters: PipelineFilters;
  onFiltersChange: (next: PipelineFilters) => void;
  channels: ChannelItem[];
  collections: CollectionItem[];
  selected: ReadonlySet<string>;
  onToggle: (awemeId: string) => void;
  onToggleAll: (awemeIds: string[], on: boolean) => void;
}

export function PipelineTable({
  data,
  error,
  filters,
  onFiltersChange,
  channels,
  collections,
  selected,
  onToggle,
  onToggleAll,
}: PipelineTableProps) {
  const [text, setText] = useState(filters.q);
  const debounced = useDebounced(text, 300);
  useEffect(() => {
    if (debounced !== filters.q) onFiltersChange({ ...filters, q: debounced, offset: 0 });
    // Only the debounced text drives this; `filters` is read for the other fields.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debounced]);

  const items = data?.items ?? [];
  const thumbs = useThumbLinks(items.map((i) => i.aweme_id));
  const counts = data?.counts_by_stage;
  const allOn = items.length > 0 && items.every((i) => selected.has(i.aweme_id));
  const change = (patch: Partial<PipelineFilters>) => onFiltersChange({ ...filters, ...patch, offset: 0 });

  const totalAll = counts ? STAGES.reduce((sum, s) => sum + (counts[s] ?? 0), 0) : 0;
  const chipBase = 'inline-flex min-h-9 shrink-0 items-center gap-1.5 rounded px-2.5 py-1 font-mono text-xs font-semibold ring-1 ring-inset';

  return (
    <section aria-labelledby="pipeline-title" className="card flex flex-col gap-3">
      <h2 id="pipeline-title" className="card-title">
        Pipeline {data ? `(${data.total})` : ''}
      </h2>

      {data && (data.sources.postgres === 'down' || data.sources.temporal === 'down') ? (
        <p role="alert" className="rounded bg-status-warning-bg p-2 text-xs text-status-warning-text">
          {data.sources.postgres === 'down' ? 'Không đọc được Postgres. ' : ''}
          {data.sources.temporal === 'down' ? 'Không đọc được Temporal, giai đoạn là ước đoán.' : ''}
        </p>
      ) : null}
      {error ? <p className="text-xs text-status-warning-text">Mất kết nối, đang hiện dữ liệu cũ.</p> : null}

      <div className="flex gap-1.5 overflow-x-auto p-1" role="group" aria-label="Lọc theo giai đoạn">
        <button
          type="button"
          data-testid="chip-all"
          aria-pressed={filters.stage === undefined}
          onClick={() => change({ stage: undefined })}
          className={`${chipBase} ${
            filters.stage === undefined ? 'bg-surface-container-highest text-primary ring-primary/50' : 'bg-surface-container-low text-text-secondary ring-transparent hover:ring-border-prominent'
          }`}
        >
          <span>Tất cả</span> <span className="rounded bg-surface-layer-1 px-1 text-text-primary">{totalAll}</span>
        </button>
        {STAGES.filter((s) => (counts?.[s] ?? 0) > 0 || filters.stage === s).map((s) => {
          const on = filters.stage === s;
          return (
            <button
              key={s}
              type="button"
              data-testid={`chip-${s}`}
              aria-pressed={on}
              onClick={() => change({ stage: on ? undefined : s })}
              className={`${chipBase} ${on ? 'bg-primary-container text-on-primary-container ring-primary' : `${TONE_CLASSES[STAGE_TONES[s]]} hover:brightness-125`}`}
            >
              <Icon name={TONE_ICONS[STAGE_TONES[s]]} size={13} />
              <span>{STAGE_LABELS[s]}</span> <span>{counts?.[s] ?? 0}</span>
            </button>
          );
        })}
      </div>

      <div className="grid gap-2 rounded bg-surface-container-low p-2 sm:grid-cols-2 lg:grid-cols-[1fr_12rem_12rem_10rem_10rem]">
        <div className="relative sm:col-span-2 lg:col-span-1">
          <label htmlFor="pipeline-q" className="sr-only">
            Tìm kiếm
          </label>
          <Icon name="search" size={16} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-text-muted" />
          <input id="pipeline-q" type="search" placeholder="Tìm aweme hoặc mô tả" value={text} onChange={(e) => setText(e.target.value)} className="field pl-9" />
        </div>
        <div>
          <label htmlFor="pipeline-channel" className="sr-only">
            Kênh
          </label>
          <select id="pipeline-channel" value={filters.channel} onChange={(e) => change({ channel: e.target.value })} className="field">
            <option value="">Mọi kênh</option>
            {channels.map((c) => (
              <option key={c.sec_user_id} value={c.sec_user_id}>
                {c.nickname ?? c.sec_user_id}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="pipeline-collection" className="sr-only">
            Lọc theo collection
          </label>
          <select id="pipeline-collection" value={filters.collection} onChange={(e) => change({ collection: e.target.value })} className="field">
            <option value="">Mọi collection</option>
            {collections.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name} ({COLLECTION_KIND_LABELS[c.kind] ?? c.kind})
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="pipeline-from" className="sr-only">
            Từ ngày
          </label>
          <input id="pipeline-from" type="date" value={filters.from} onChange={(e) => change({ from: e.target.value })} className="field" />
        </div>
        <div>
          <label htmlFor="pipeline-to" className="sr-only">
            Đến ngày
          </label>
          <input id="pipeline-to" type="date" value={filters.to} onChange={(e) => change({ to: e.target.value })} className="field" />
        </div>
      </div>

      {filters.stage === 'rendered' ? (
        <RenderedGrid items={items} loading={data === null} selected={selected} onToggle={onToggle} thumbs={thumbs} />
      ) : (
        <div className="relative overflow-x-auto rounded bg-surface-layer-1">
          <table className="w-full min-w-[52rem] border-collapse text-left text-sm">
            <thead>
              <tr className="bg-surface-container-low font-mono text-[11px] uppercase tracking-wider text-text-muted">
                <th scope="col" className="w-10 p-2 text-center">
                  <input
                    type="checkbox"
                    aria-label="Chọn tất cả"
                    checked={allOn}
                    onChange={(e) =>
                      onToggleAll(
                        items.map((i) => i.aweme_id),
                        e.target.checked,
                      )
                    }
                    className="h-5 w-5 cursor-pointer"
                  />
                </th>
                <th scope="col" className="p-2 font-semibold">Video</th>
                <th scope="col" className="p-2 font-semibold">Collection</th>
                <th scope="col" className="p-2 font-semibold">Gen</th>
                <th scope="col" className="p-2 font-semibold">Giai đoạn</th>
                <th scope="col" className="p-2 font-semibold">Ở giai đoạn này</th>
                <th scope="col" className="p-2 font-semibold">Cập nhật</th>
                <th scope="col" className="p-2 text-right font-semibold">Chi tiết</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border-subtle/60">
              {items.length === 0 ? (
                <tr>
                  <td colSpan={8} className="p-4 text-text-secondary">
                    {data ? 'Không có video nào khớp.' : 'Đang tải…'}
                  </td>
                </tr>
              ) : (
                items.map((item) => <Row key={item.aweme_id} item={item} thumb={thumbs[item.aweme_id]} checked={selected.has(item.aweme_id)} onToggle={onToggle} />)
              )}
            </tbody>
          </table>
        </div>

      )}

      {data && data.total > PAGE_SIZE ? (
        <div className="flex items-center justify-between gap-2 text-xs text-text-secondary">
          <button
            type="button"
            disabled={filters.offset === 0}
            onClick={() => onFiltersChange({ ...filters, offset: Math.max(0, filters.offset - PAGE_SIZE) })}
            className="btn btn-ghost"
          >
            Trang trước
          </button>
          <span className="font-mono">
            {filters.offset + 1}-{Math.min(data.total, filters.offset + PAGE_SIZE)} / {data.total}
          </span>
          <button
            type="button"
            disabled={filters.offset + PAGE_SIZE >= data.total}
            onClick={() => onFiltersChange({ ...filters, offset: filters.offset + PAGE_SIZE })}
            className="btn btn-ghost"
          >
            Trang sau
          </button>
        </div>
      ) : null}
    </section>
  );
}

function Row({ item, thumb, checked, onToggle }: { item: VideoListItem; thumb: string | undefined; checked: boolean; onToggle: (id: string) => void }) {
  const duration = formatDuration(item.duration_ms);
  return (
    <tr data-testid="pipeline-row" data-aweme-id={item.aweme_id} className={`transition-colors hover:bg-surface-container ${checked ? 'bg-surface-container-high/60' : ''}`}>
      <td className="p-2 text-center">
        <input type="checkbox" aria-label={`Chọn ${item.aweme_id}`} checked={checked} onChange={() => onToggle(item.aweme_id)} className="h-5 w-5 cursor-pointer" />
      </td>
      <td className="max-w-sm p-2">
        <div className="flex items-center gap-2">
          <a href={`#/video/${item.aweme_id}`} tabIndex={-1} aria-hidden="true" className="shrink-0">
            <SmallThumb url={thumb} />
          </a>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-1.5">
              <a href={`#/video/${item.aweme_id}`} className="font-mono text-xs font-semibold text-secondary hover:underline">
                {item.aweme_id}
              </a>
              <span className="max-w-[10rem] truncate rounded bg-surface-layer-2 px-1 font-mono text-[11px] text-text-muted">{item.channel ?? item.sec_user_id}</span>
              {duration ? <span className="rounded bg-surface-layer-2 px-1 font-mono text-[11px] text-text-muted" title="Thời lượng video">{duration}</span> : null}
            </div>
            <p className="truncate text-xs text-text-primary" title={item.desc ?? undefined}>
              {item.desc ?? '(không có mô tả)'}
            </p>
          </div>
        </div>
      </td>
      <td data-testid="collection-cell" className="max-w-[12rem] p-2 text-xs">
        <p className="truncate text-text-primary" title={item.collection.name}>
          {item.collection.name}
        </p>
        {item.mix_episode !== null ? <p className="font-mono text-[11px] text-text-muted">Tập {item.mix_episode}</p> : null}
      </td>
      <td className="p-2">
        <span className="rounded bg-surface-layer-2 px-1 font-mono text-[11px] text-text-primary">{item.current_gen === null ? '-' : `g${item.current_gen}`}</span>
      </td>
      <td className="p-2">
        <div className="flex flex-col gap-1">
          <StageBadge item={item} />
          {item.mismatch_count !== null && item.mismatch_count > 0 ? (
            <span className="inline-flex items-center gap-1 font-mono text-[11px] text-status-warning-text">
              <Icon name="warning" size={11} />
              {item.mismatch_count} cue lệch
            </span>
          ) : null}
          {item.attention.length > 0 ? (
            <span className="inline-flex items-center gap-1 text-[11px] text-status-error-text" title={item.attention.map((a) => a.reason).join('\n')}>
              <Icon name="warning" size={11} />
              <span aria-hidden="true">!</span>
              <span className="sr-only">{item.attention.map((a) => a.reason).join('; ')}</span>
            </span>
          ) : null}
        </div>
      </td>
      <td className="whitespace-nowrap p-2 font-mono text-xs text-text-secondary" title={formatDateTime(item.stage_since)}>
        {relativeTime(item.stage_since)}
      </td>
      <td className="whitespace-nowrap p-2 font-mono text-xs text-text-muted" title={formatDateTime(item.updated_at)}>
        {relativeTime(item.updated_at)} trước
      </td>
      <td className="p-2 text-right">
        <a href={`#/video/${item.aweme_id}`} aria-label={`Chi tiết ${item.aweme_id}`} title="Xem chi tiết" className="inline-flex min-h-9 min-w-9 items-center justify-center rounded bg-surface-layer-2 text-text-primary hover:bg-surface-bright">
          <Icon name="eye" />
        </a>
      </td>
    </tr>
  );
}
