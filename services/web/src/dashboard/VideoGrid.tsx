import type { Stage, VideoListItem } from '../api/types';
import { Icon } from '../components/Icon';
import { formatDateTime, formatDuration, relativeTime } from './format';
import { StageBadge } from './StageBadge';
import { useMediaLinks } from './useMediaLinks';
import { VideoThumb } from './VideoThumb';

/** Stages whose video has a final render, and so may have a still of it. */
const HAS_RENDER: ReadonlySet<Stage> = new Set<Stage>(['rendered', 'published']);
const NO_KINDS: never[] = [];

/**
 * One video as a picture that opens its detail page: a still of its final render when it has one,
 * else the thumbnail of its source, else a placeholder. Its stage and any warning sit on the picture.
 */
function Card({ item, thumb, checked, onToggle }: { item: VideoListItem; thumb: string | undefined; checked: boolean; onToggle: (id: string) => void }) {
  const { links } = useMediaLinks(item.aweme_id, item.current_gen, HAS_RENDER.has(item.stage) ? ['poster'] : NO_KINDS);
  const poster = links.poster?.available ? links.poster.url : undefined;
  const duration = formatDuration(item.duration_ms);
  const reasons = item.attention.map((a) => a.reason);

  return (
    <li
      data-testid="video-card"
      data-aweme-id={item.aweme_id}
      className={`group relative overflow-hidden rounded-lg bg-surface-container ring-1 ring-inset ${checked ? 'ring-2 ring-primary' : 'ring-border-subtle'}`}
    >
      <a href={`#/video/${item.aweme_id}`} aria-label={`Chi tiết ${item.aweme_id}`} className="block aspect-[3/4] w-full">
        <VideoThumb
          urls={[poster, thumb]}
          className="h-full w-full object-cover transition-transform duration-200 group-hover:scale-[1.03]"
          placeholder={
            <span data-testid="thumb-placeholder" className="flex h-full w-full flex-col items-center justify-center gap-1 bg-surface-layer-1 text-text-muted">
              <Icon name="video" size={28} />
              <span className="font-mono text-[11px]">Chưa có ảnh</span>
            </span>
          }
        />
        <span className="pointer-events-none absolute inset-x-0 bottom-0 flex flex-col gap-0.5 bg-gradient-to-t from-black/90 via-black/60 to-transparent p-2 pt-10">
          <span className="line-clamp-2 text-xs font-medium text-white" title={item.desc ?? undefined}>
            {item.desc ?? '(không có mô tả)'}
          </span>
          <span data-testid="collection-cell" className="truncate text-[11px] text-white/80" title={item.collection.name}>
            {item.collection.name}
            {item.mix_episode !== null ? ` · Tập ${item.mix_episode}` : ''}
          </span>
          <span className="flex flex-wrap items-center gap-x-2 font-mono text-[11px] text-white/70">
            <span>{item.aweme_id}</span>
            {item.current_gen !== null ? <span>g{item.current_gen}</span> : null}
            {duration ? <span title="Thời lượng video">{duration}</span> : null}
            <span title={`Cập nhật lần cuối ${formatDateTime(item.updated_at)}`}>{relativeTime(item.updated_at)} trước</span>
          </span>
        </span>
      </a>
      <input
        type="checkbox"
        aria-label={`Chọn ${item.aweme_id}`}
        checked={checked}
        onChange={() => onToggle(item.aweme_id)}
        className="absolute left-2 top-2 h-5 w-5 cursor-pointer"
      />
      <span className="pointer-events-none absolute right-2 top-2 flex max-w-[75%] flex-col items-end gap-1">
        <span className="rounded bg-surface-container/90" title={`Ở giai đoạn này ${relativeTime(item.stage_since)}`}>
          <StageBadge item={item} />
        </span>
        {item.mismatch_count !== null && item.mismatch_count > 0 ? (
          <span className="inline-flex items-center gap-1 rounded bg-surface-container/90 px-1.5 py-0.5 font-mono text-[11px] font-semibold text-status-warning-text">
            <Icon name="warning" size={12} />
            {item.mismatch_count} cue lệch
          </span>
        ) : null}
        {reasons.length > 0 ? (
          <span className="pointer-events-auto inline-flex items-center gap-1 rounded bg-surface-container/90 px-1.5 py-0.5 text-[11px] font-semibold text-status-error-text" title={reasons.join('\n')}>
            <Icon name="warning" size={12} />
            <span aria-hidden="true">Cần xử lý</span>
            <span className="sr-only">{reasons.join('; ')}</span>
          </span>
        ) : null}
      </span>
    </li>
  );
}

/** The videos as a grid of pictures, so one is recognised by its image and not its number. */
export function VideoGrid({
  items,
  loading,
  selected,
  onToggle,
  onToggleAll,
  thumbs,
}: {
  items: VideoListItem[];
  loading: boolean;
  selected: ReadonlySet<string>;
  onToggle: (awemeId: string) => void;
  onToggleAll: (awemeIds: string[], on: boolean) => void;
  /** Source thumbnails by aweme id. */
  thumbs: Readonly<Record<string, string>>;
}) {
  if (items.length === 0) {
    return <p className="rounded bg-surface-layer-1 p-4 text-sm text-text-secondary">{loading ? 'Đang tải…' : 'Không có video nào khớp.'}</p>;
  }
  const allOn = items.every((i) => selected.has(i.aweme_id));
  return (
    <div className="flex flex-col gap-2">
      <label className="inline-flex w-max cursor-pointer items-center gap-2 text-xs text-text-secondary">
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
        Chọn cả trang
      </label>
      <ul data-testid="video-grid" className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6">
        {items.map((item) => (
          <Card key={item.aweme_id} item={item} thumb={thumbs[item.aweme_id]} checked={selected.has(item.aweme_id)} onToggle={onToggle} />
        ))}
      </ul>
    </div>
  );
}
