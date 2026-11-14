import { useEffect, useRef, useState } from 'react';
import { getCoverPreview } from '../api/client';
import type { CoverPreview as CoverPreviewData } from '../api/types';
import { coverBox } from './cover-geometry';

/** What the worker draws when a collection sets no colour or opacity of its own. */
const DEFAULT_COVER_COLOR = '#101010';
const DEFAULT_COVER_OPACITY_PCT = 92;

export interface CoverPreviewProps {
  collectionId: string;
  /** The values being typed (null = automatic, also for a value that is not valid yet). */
  offsetPct: number | null;
  widthPct: number | null;
  heightPct: number | null;
  /** `#rrggbb`, or null for the shared colour. */
  color: string | null;
  opacityPct: number | null;
}

type State = { kind: 'loading' } | { kind: 'ready'; data: CoverPreviewData } | { kind: 'missing' } | { kind: 'error'; message: string };

/**
 * A frame of one of the collection's videos with the cover box drawn on top, from the values in the
 * form before they are saved. The frame is the source video paused on a moment with source subtitles.
 */
export function CoverPreview({ collectionId, offsetPct, widthPct, heightPct, color, opacityPct }: CoverPreviewProps) {
  const [state, setState] = useState<State>({ kind: 'loading' });
  const video = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const data = await getCoverPreview(collectionId);
        if (!alive) return;
        if (!data || !(data.width > 0) || !(data.height > 0)) setState({ kind: 'error', message: 'Câu trả lời không hợp lệ.' });
        else setState({ kind: 'ready', data });
      } catch (err) {
        if (!alive) return;
        if ((err as { status?: unknown } | null)?.status === 404) setState({ kind: 'missing' });
        else setState({ kind: 'error', message: err instanceof Error ? err.message : String(err) });
      }
    })();
    return () => {
      alive = false;
    };
  }, [collectionId]);

  if (state.kind === 'loading') return <p className="text-xs text-text-secondary">Đang tải khung xem trước…</p>;
  if (state.kind === 'missing') {
    return (
      <p data-testid="cover-preview-missing" className="text-xs text-text-secondary">
        Chưa có video nào của collection có project review để xem trước.
      </p>
    );
  }
  if (state.kind === 'error') {
    return (
      <p role="alert" className="text-xs text-status-warning-text">
        Không tải được khung xem trước: {state.message}
      </p>
    );
  }

  const { data } = state;
  const box = coverBox({ frameWidth: data.width, frameHeight: data.height, region: data.source_region, offsetPct, widthPct, heightPct });
  const pct = (px: number) => `${(px / data.height) * 100}%`;
  return (
    <figure className="flex flex-col gap-1">
      <div
        data-testid="cover-preview-frame"
        className="relative mx-auto max-w-full overflow-hidden rounded bg-black"
        // A landscape frame takes the dialog's width; a portrait one a height that fits on screen.
        style={{ aspectRatio: `${data.width} / ${data.height}`, ...(data.width >= data.height ? { width: '100%' } : { height: 'min(60vh, 32rem)' }) }}
      >
        {data.source_url ? (
          <video
            ref={video}
            src={data.source_url}
            muted
            playsInline
            preload="auto"
            aria-label="Khung hình xem trước"
            className="absolute inset-0 h-full w-full object-fill"
            onLoadedMetadata={() => {
              if (video.current) video.current.currentTime = data.sample_ms / 1000;
            }}
          />
        ) : null}
        <div
          data-testid="cover-preview-box"
          aria-hidden="true"
          className="absolute"
          style={{
            top: pct(box.top),
            height: pct(box.height),
            left: `${(100 - box.widthPct) / 2}%`,
            width: `${box.widthPct}%`,
            backgroundColor: color ?? DEFAULT_COVER_COLOR,
            opacity: (opacityPct ?? DEFAULT_COVER_OPACITY_PCT) / 100,
          }}
        />
      </div>
      <figcaption className="font-mono text-[11px] text-text-muted">
        Video {data.aweme_id} · {data.width}×{data.height}
        {data.source_url ? null : ' · không mở được video gốc'}
      </figcaption>
    </figure>
  );
}
