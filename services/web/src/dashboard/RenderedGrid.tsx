import { useEffect, useState } from 'react';
import type { VideoListItem } from '../api/types';
import { Icon } from '../components/Icon';
import { formatDuration, relativeTime } from './format';
import { useMediaLinks } from './useMediaLinks';

/** One rendered video as a picture (a still of its final render) that opens its detail page. */
function Card({ item, checked, onToggle }: { item: VideoListItem; checked: boolean; onToggle: (id: string) => void }) {
  const { links } = useMediaLinks(item.aweme_id, item.current_gen, ['poster']);
  const url = links.poster?.available ? links.poster.url : undefined;
  // A render from before posters existed has a link but no picture behind it; show the placeholder.
  const [broken, setBroken] = useState(false);
  useEffect(() => setBroken(false), [url]);
  const duration = formatDuration(item.duration_ms);

  return (
    <li
      data-testid="rendered-card"
      data-aweme-id={item.aweme_id}
      className={`group relative overflow-hidden rounded-lg bg-surface-container ring-1 ring-inset ${checked ? 'ring-primary' : 'ring-border-subtle'}`}
    >
      <a href={`#/video/${item.aweme_id}`} aria-label={`Chi tiết ${item.aweme_id}`} className="block aspect-[3/4] w-full">
        {url && !broken ? (
          <img src={url} alt="" loading="lazy" onError={() => setBroken(true)} className="h-full w-full object-cover transition-transform duration-200 group-hover:scale-[1.03]" />
        ) : (
          <span className="flex h-full w-full flex-col items-center justify-center gap-1 bg-surface-layer-1 text-text-muted">
            <Icon name="video" size={28} />
            <span className="font-mono text-[11px]">Chưa có ảnh</span>
          </span>
        )}
        <span className="pointer-events-none absolute inset-x-0 bottom-0 flex flex-col gap-0.5 bg-gradient-to-t from-black/85 via-black/50 to-transparent p-2 pt-8">
          <span className="line-clamp-2 text-xs font-medium text-white" title={item.desc ?? undefined}>
            {item.desc ?? '(không có mô tả)'}
          </span>
          <span className="flex flex-wrap items-center gap-x-2 font-mono text-[11px] text-white/80">
            <span>{item.aweme_id}</span>
            {item.current_gen !== null ? <span>g{item.current_gen}</span> : null}
            {duration ? <span>{duration}</span> : null}
            <span title="Cập nhật lần cuối">{relativeTime(item.updated_at)} trước</span>
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
    </li>
  );
}

/** The rendered videos as a grid of pictures, so one is recognised by its image and not its number. */
export function RenderedGrid({
  items,
  loading,
  selected,
  onToggle,
}: {
  items: VideoListItem[];
  loading: boolean;
  selected: ReadonlySet<string>;
  onToggle: (awemeId: string) => void;
}) {
  if (items.length === 0) {
    return <p className="rounded bg-surface-layer-1 p-4 text-sm text-text-secondary">{loading ? 'Đang tải…' : 'Không có video nào khớp.'}</p>;
  }
  return (
    <ul data-testid="rendered-grid" className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6">
      {items.map((item) => (
        <Card key={item.aweme_id} item={item} checked={selected.has(item.aweme_id)} onToggle={onToggle} />
      ))}
    </ul>
  );
}
