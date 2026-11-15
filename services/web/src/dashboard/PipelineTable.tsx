import { useEffect, useState } from 'react';
import type { ChannelItem, CollectionItem, Stage, VideoListResponse } from '../api/types';
import { STAGES } from '../api/types';
import { Icon } from '../components/Icon';
import {
  COLLECTION_KIND_LABELS,
  STAGE_LABELS,
  STAGE_TONES,
  TONE_CLASSES,
  TONE_ICONS,
} from './format';
import { VideoGrid } from './VideoGrid';
import { useThumbLinks } from './useMediaLinks';

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

      <VideoGrid items={items} loading={data === null} selected={selected} onToggle={onToggle} onToggleAll={onToggleAll} thumbs={thumbs} />

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
