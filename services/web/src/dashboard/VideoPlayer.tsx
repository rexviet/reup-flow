import { useEffect, useRef, useState } from 'react';

/**
 * Plays a signed URL that is renewed every few minutes. A renewed URL never replaces one that is
 * playing fine: it waits for a pause, the end, or an error, then resumes from the same second
 * (and keeps playing if it was playing). A player that already failed takes the new URL at once.
 */
export function VideoPlayer({ url, label }: { url: string | undefined; label: string }) {
  const ref = useRef<HTMLVideoElement>(null);
  const pending = useRef<string | undefined>(undefined);
  const resumeAt = useRef(0);
  const autoplay = useRef(false);
  const [src, setSrc] = useState<string | undefined>(url);

  function swap(next: string | undefined) {
    const video = ref.current;
    resumeAt.current = video?.currentTime ?? 0;
    // A network error mid-play leaves `paused` false, so this keeps playing across the swap.
    autoplay.current = video !== null && !video.paused && !video.ended;
    pending.current = undefined;
    setSrc(next);
  }

  useEffect(() => {
    if (url === src) {
      pending.current = undefined;
      return;
    }
    const video = ref.current;
    if (!src || !video || video.paused || video.ended || video.error !== null) {
      if (!video || video.paused || video.ended) autoplay.current = false;
      swap(url);
      if (video && video.error !== null) autoplay.current = true;
    } else {
      pending.current = url;
    }
  }, [url, src]);

  function applyPending() {
    if (pending.current && pending.current !== src) swap(pending.current);
  }

  if (!src) return <p className="flex aspect-video items-center justify-center rounded bg-surface-container-lowest text-xs text-text-muted">{label}: chưa có file.</p>;
  return (
    <video
      ref={ref}
      src={src}
      controls
      preload="metadata"
      aria-label={label}
      className="aspect-video w-full rounded bg-black"
      onPause={applyPending}
      onEnded={applyPending}
      onError={applyPending}
      onLoadedMetadata={() => {
        const video = ref.current;
        if (!video) return;
        if (resumeAt.current > 0) video.currentTime = resumeAt.current;
        if (autoplay.current) {
          autoplay.current = false;
          // The browser may refuse without a gesture; the controls are still there.
          void Promise.resolve(video.play?.()).catch(() => undefined);
        }
      }}
    />
  );
}
