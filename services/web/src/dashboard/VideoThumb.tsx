import { useState, type ReactNode } from 'react';
import { Icon } from '../components/Icon';

/**
 * The first of `urls` whose image loads, else `placeholder`: shown while there is no URL yet, for a
 * video that has no picture (not downloaded, or its thumbnail not made yet: the link answers 404),
 * and when every image fails to load.
 */
export function VideoThumb({ urls, className, placeholder }: { urls: ReadonlyArray<string | undefined>; className: string; placeholder: ReactNode }) {
  const [failed, setFailed] = useState<ReadonlySet<string>>(() => new Set());
  const url = urls.find((u): u is string => u !== undefined && !failed.has(u));
  if (url === undefined) return <>{placeholder}</>;
  return <img data-testid="video-thumb" src={url} alt="" loading="lazy" onError={() => setFailed((prev) => new Set(prev).add(url))} className={className} />;
}

/** A small 9:16 picture of a video for a list row or a header, with a neutral placeholder. */
export function SmallThumb({ url, size = 'sm' }: { url: string | undefined; size?: 'sm' | 'md' }) {
  const box = size === 'sm' ? 'h-16 w-9' : 'h-32 w-[4.5rem]';
  return (
    <VideoThumb
      urls={[url]}
      className={`${box} shrink-0 rounded bg-surface-layer-2 object-cover`}
      placeholder={
        <span data-testid="thumb-placeholder" title="Chưa có ảnh" className={`${box} flex shrink-0 items-center justify-center rounded bg-surface-layer-2 text-text-muted`}>
          <Icon name="video" size={size === 'sm' ? 14 : 22} />
          <span className="sr-only">Chưa có ảnh</span>
        </span>
      }
    />
  );
}
