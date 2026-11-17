import { useCallback, useEffect, useRef, useState, type MouseEvent as ReactMouseEvent } from 'react';
import { addWatermark, deleteWatermark, getCoverPreview, getWatermarkImage, listWatermarks } from '../api/client';
import type { CollectionWatermark, CoverPreview } from '../api/types';
import { useConfirm } from './ConfirmDialog';

/** The API's limit; the picker stops offering a cut when it is reached. */
export const MAX_WATERMARKS = 10;
/** The API refuses a region smaller than this on either side. */
const MIN_SIDE = 8;

interface Region {
  x: number;
  y: number;
  w: number;
  h: number;
}

type Frame = { kind: 'loading' } | { kind: 'ready'; data: CoverPreview } | { kind: 'missing' } | { kind: 'error'; message: string };

const message = (err: unknown): string => (err instanceof Error ? err.message : String(err));

/** `m:ss.s` of a position in milliseconds. */
export function formatTime(ms: number): string {
  const s = Math.max(0, ms) / 1000;
  return `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, '0')}`;
}

/** The rectangle between two points, in whole pixels, kept inside a `width`×`height` frame. */
export function regionBetween(a: { x: number; y: number }, b: { x: number; y: number }, width: number, height: number): Region {
  const clampX = (v: number) => Math.min(width, Math.max(0, Math.round(v)));
  const clampY = (v: number) => Math.min(height, Math.max(0, Math.round(v)));
  const [x1, x2] = [clampX(a.x), clampX(b.x)].sort((p, q) => p - q);
  const [y1, y2] = [clampY(a.y), clampY(b.y)].sort((p, q) => p - q);
  return { x: x1, y: y1, w: x2 - x1, h: y2 - y1 };
}

/** A `width`×`height` canvas drawn by `draw`, as PNG. */
function drawPng(width: number, height: number, draw: (ctx: CanvasRenderingContext2D) => void): Promise<Blob> {
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext('2d');
  if (!ctx) return Promise.reject(new Error('Trình duyệt không vẽ được canvas.'));
  draw(ctx);
  return new Promise((resolve, reject) => {
    // Throws a SecurityError when the video is from another origin (the canvas is tainted).
    canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error('Không tạo được ảnh PNG.'))), 'image/png');
  });
}

/** Crops `region` of the video's current frame, at the video's own resolution, as PNG. */
function cropPng(video: HTMLVideoElement, region: Region): Promise<Blob> {
  return drawPng(region.w, region.h, (ctx) => ctx.drawImage(video, region.x, region.y, region.w, region.h, 0, 0, region.w, region.h));
}

/** An image file (PNG, JPEG, WebP…) as PNG at its own size: the worker reads PNG only. */
async function fileToPng(file: File): Promise<{ png: Blob; width: number; height: number }> {
  let bitmap: ImageBitmap;
  try {
    bitmap = await createImageBitmap(file);
  } catch {
    throw new Error('Không đọc được file ảnh.');
  }
  try {
    const png = await drawPng(bitmap.width, bitmap.height, (ctx) => ctx.drawImage(bitmap, 0, 0));
    return { png, width: bitmap.width, height: bitmap.height };
  } finally {
    bitmap.close();
  }
}

/** A whole number of pixels typed in a field, or null. */
const pixelsOf = (raw: string): number | null => (/^\d+$/.test(raw.trim()) ? Number(raw.trim()) : null);

/**
 * The watermark images of a collection, and a picker to cut a new one: seek the
 * collection's preview video to a moment that shows the watermark, drag a box over
 * it, and the box is cut from that frame at the video's own resolution and uploaded.
 * A run of the collection then finds and blurs every image it holds.
 */
export function WatermarkPicker({ collectionId }: { collectionId: string }) {
  const [frame, setFrame] = useState<Frame>({ kind: 'loading' });
  const [items, setItems] = useState<CollectionWatermark[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [natural, setNatural] = useState<{ width: number; height: number } | null>(null);
  const [durationMs, setDurationMs] = useState(0);
  const [timeMs, setTimeMs] = useState(0);
  const [region, setRegion] = useState<Region | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const video = useRef<HTMLVideoElement>(null);
  const surface = useRef<HTMLDivElement>(null);
  const { ask, dialog } = useConfirm();

  const reload = useCallback(async () => {
    try {
      setItems((await listWatermarks(collectionId)).items);
      setListError(null);
    } catch (err) {
      setListError(message(err));
    }
  }, [collectionId]);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const data = await getCoverPreview(collectionId);
        if (alive) setFrame({ kind: 'ready', data });
      } catch (err) {
        if (!alive) return;
        if ((err as { status?: unknown } | null)?.status === 404) setFrame({ kind: 'missing' });
        else setFrame({ kind: 'error', message: message(err) });
      }
    })();
    void reload();
    return () => {
      alive = false;
    };
  }, [collectionId, reload]);

  /** A mouse position over the frame, in the video's own pixels. */
  function toPixels(clientX: number, clientY: number): { x: number; y: number } | null {
    const rect = surface.current?.getBoundingClientRect();
    if (!rect || !natural || rect.width <= 0 || rect.height <= 0) return null;
    return {
      x: ((clientX - rect.left) / rect.width) * natural.width,
      y: ((clientY - rect.top) / rect.height) * natural.height,
    };
  }

  function startDrag(e: ReactMouseEvent<HTMLDivElement>) {
    if (e.button !== 0 || !natural) return;
    const start = toPixels(e.clientX, e.clientY);
    if (!start) return;
    e.preventDefault();
    video.current?.pause();
    setNotice(null);
    setSaveError(null);
    setRegion(regionBetween(start, start, natural.width, natural.height));
    // On the window, so a drag that leaves the frame still follows the mouse and ends.
    const move = (ev: MouseEvent) => {
      const at = toPixels(ev.clientX, ev.clientY);
      if (at) setRegion(regionBetween(start, at, natural.width, natural.height));
    };
    const up = (ev: MouseEvent) => {
      move(ev);
      window.removeEventListener('mousemove', move);
      window.removeEventListener('mouseup', up);
    };
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', up);
  }

  function seek(ms: number) {
    setTimeMs(ms);
    if (video.current) video.current.currentTime = ms / 1000;
  }

  async function save() {
    const v = video.current;
    if (frame.kind !== 'ready' || !v || !natural || !region) return;
    setSaving(true);
    setSaveError(null);
    setNotice(null);
    try {
      const png = await cropPng(v, region);
      await addWatermark(
        collectionId,
        {
          aweme_id: frame.data.aweme_id,
          time_ms: Math.round(v.currentTime * 1000),
          frame_width: natural.width,
          frame_height: natural.height,
          ...region,
        },
        png,
      );
      setRegion(null);
      setNotice('Đã lưu ảnh mẫu.');
      await reload();
    } catch (err) {
      setSaveError(`Không lưu được ảnh mẫu: ${message(err)}`);
    } finally {
      setSaving(false);
    }
  }

  async function remove(item: CollectionWatermark) {
    if (!(await ask(`Xoá ảnh watermark ${item.w}×${item.h} này?`))) return;
    try {
      await deleteWatermark(collectionId, item.id);
      await reload();
    } catch (err) {
      setListError(`Không xoá được ảnh: ${message(err)}`);
    }
  }

  const full = (items?.length ?? 0) >= MAX_WATERMARKS;
  const tooSmall = region !== null && (region.w < MIN_SIDE || region.h < MIN_SIDE);
  const pct = (v: number, of: number) => `${(v / of) * 100}%`;

  return (
    <div className="flex flex-col gap-4">
      <p className="text-xs text-text-secondary">
        Tua tới chỗ watermark hiện rõ, kéo khung ôm sát nó rồi lưu. Mỗi kiểu watermark (chữ khác, đậm hơn, góc khác hẳn) cần một ảnh
        riêng. Collection không có ảnh nào thì không làm mờ gì.
      </p>

      {frame.kind === 'loading' ? <p className="text-xs text-text-secondary">Đang tải video…</p> : null}
      {frame.kind === 'missing' ? (
        <p data-testid="watermark-picker-missing" className="text-xs text-text-secondary">
          Chưa có video nào của collection có project review, nên chưa cắt được ảnh mẫu.
        </p>
      ) : null}
      {frame.kind === 'error' ? (
        <p role="alert" className="text-xs text-status-warning-text">
          Không tải được video: {frame.message}
        </p>
      ) : null}
      {frame.kind === 'ready' && !frame.data.source_url ? (
        <p role="alert" className="text-xs text-status-warning-text">
          Không mở được video gốc {frame.data.aweme_id}.
        </p>
      ) : null}

      {frame.kind === 'ready' && frame.data.source_url ? (
        <figure className="flex flex-col gap-2">
          <div
            className="relative mx-auto max-w-full overflow-hidden rounded bg-black"
            style={{
              aspectRatio: `${natural?.width ?? frame.data.width} / ${natural?.height ?? frame.data.height}`,
              ...((natural?.width ?? frame.data.width) >= (natural?.height ?? frame.data.height)
                ? { width: '100%' }
                : { height: 'min(60vh, 32rem)' }),
            }}
          >
            <video
              ref={video}
              src={frame.data.source_url}
              muted
              playsInline
              preload="auto"
              aria-label="Video để cắt ảnh watermark"
              className="absolute inset-0 h-full w-full object-fill"
              onLoadedMetadata={(e) => {
                const v = e.currentTarget;
                setNatural({ width: v.videoWidth, height: v.videoHeight });
                setDurationMs(Number.isFinite(v.duration) ? Math.floor(v.duration * 1000) : 0);
                v.currentTime = frame.data.sample_ms / 1000;
                setTimeMs(frame.data.sample_ms);
              }}
              onTimeUpdate={(e) => setTimeMs(Math.round(e.currentTarget.currentTime * 1000))}
            />
            <div
              ref={surface}
              data-testid="watermark-picker-surface"
              aria-label="Kéo để chọn vùng watermark"
              className="absolute inset-0 cursor-crosshair"
              onMouseDown={startDrag}
            >
              {region && natural ? (
                <div
                  data-testid="watermark-picker-region"
                  aria-hidden="true"
                  className="absolute border-2 border-status-warning-text bg-white/10"
                  style={{
                    left: pct(region.x, natural.width),
                    top: pct(region.y, natural.height),
                    width: pct(region.w, natural.width),
                    height: pct(region.h, natural.height),
                  }}
                />
              ) : null}
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              className="btn btn-ghost text-xs"
              disabled={!natural}
              onClick={() => (video.current?.paused ? void video.current.play() : video.current?.pause())}
            >
              Phát / Dừng
            </button>
            <input
              type="range"
              aria-label="Vị trí trong video"
              min={0}
              max={durationMs}
              step={100}
              value={Math.min(timeMs, durationMs)}
              disabled={!natural || durationMs <= 0}
              onChange={(e) => seek(Number(e.target.value))}
              className="min-w-0 flex-1"
            />
            <span className="shrink-0 font-mono text-[11px] text-text-muted">
              {formatTime(timeMs)} / {formatTime(durationMs)}
            </span>
          </div>
          <figcaption className="font-mono text-[11px] text-text-muted">
            Video {frame.data.aweme_id}
            {natural ? ` · ${natural.width}×${natural.height}` : ''}
            {region ? ` · vùng ${region.w}×${region.h} tại (${region.x}, ${region.y})` : ''}
          </figcaption>
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              className="btn btn-primary"
              disabled={saving || !region || tooSmall || full}
              onClick={() => void save()}
            >
              {saving ? 'Đang lưu…' : 'Lưu ảnh mẫu'}
            </button>
            {region ? (
              <button type="button" className="btn btn-ghost" disabled={saving} onClick={() => setRegion(null)}>
                Bỏ chọn
              </button>
            ) : null}
            {tooSmall ? (
              <span className="text-xs text-status-warning-text">
                Vùng phải rộng và cao ít nhất {MIN_SIDE} px.
              </span>
            ) : null}
            {full ? (
              <span className="text-xs text-status-warning-text">Đã đủ {MAX_WATERMARKS} ảnh, xoá bớt rồi thêm.</span>
            ) : null}
            {notice ? (
              <span role="status" className="text-xs text-status-success-text">
                {notice}
              </span>
            ) : null}
            {saveError ? (
              <span role="alert" className="text-xs text-status-error-text">
                {saveError}
              </span>
            ) : null}
          </div>
        </figure>
      ) : null}

      <WatermarkUpload
        collectionId={collectionId}
        frameSize={natural ?? (frame.kind === 'ready' ? { width: frame.data.width, height: frame.data.height } : null)}
        full={full}
        onAdded={reload}
      />

      <section aria-label="Ảnh watermark của collection" className="flex flex-col gap-2">
        <h3 className="text-xs font-medium text-text-secondary">
          Ảnh mẫu ({items?.length ?? 0}/{MAX_WATERMARKS})
        </h3>
        {listError ? (
          <p role="alert" className="text-xs text-status-error-text">
            {listError}
          </p>
        ) : null}
        {items && items.length === 0 ? (
          <p className="text-xs text-text-secondary">Chưa có ảnh nào: video của collection không được làm mờ watermark.</p>
        ) : null}
        <ul className="flex flex-wrap gap-3">
          {items?.map((item) => (
            <li key={item.id} data-testid="watermark-item" className="flex flex-col gap-1 rounded bg-surface-layer-2 p-2">
              <WatermarkImage item={item} />
              <span className="font-mono text-[11px] text-text-muted">
                {item.w}×{item.h} · {item.time_ms === null ? 'tải lên' : formatTime(item.time_ms)}
              </span>
              <button
                type="button"
                className="btn btn-ghost text-xs"
                aria-label={`Xoá ảnh watermark ${item.w}×${item.h}`}
                onClick={() => void remove(item)}
              >
                Xoá
              </button>
            </li>
          ))}
        </ul>
      </section>
      {dialog}
    </div>
  );
}

/**
 * Adds an image file from the admin's machine as it is. It needs the size of the frame it
 * belongs to (its base scale when matching), which starts as the collection's video size.
 */
function WatermarkUpload({
  collectionId,
  frameSize,
  full,
  onAdded,
}: {
  collectionId: string;
  frameSize: { width: number; height: number } | null;
  full: boolean;
  onAdded: () => Promise<void>;
}) {
  const [file, setFile] = useState<{ name: string; png: Blob; width: number; height: number } | null>(null);
  const [frameWidth, setFrameWidth] = useState('');
  const [frameHeight, setFrameHeight] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // Bumped after an upload so the file input forgets the file it held.
  const [inputKey, setInputKey] = useState(0);

  // Follows the video (its review size, then its own size once loaded) until the admin types one.
  const typed = useRef(false);
  const knownWidth = frameSize?.width;
  const knownHeight = frameSize?.height;
  useEffect(() => {
    if (typed.current || !knownWidth || !knownHeight) return;
    setFrameWidth(String(knownWidth));
    setFrameHeight(String(knownHeight));
  }, [knownWidth, knownHeight]);

  async function choose(chosen: File | undefined) {
    setFile(null);
    setError(null);
    setNotice(null);
    if (!chosen) return;
    try {
      setFile({ name: chosen.name, ...(await fileToPng(chosen)) });
    } catch (err) {
      setError(message(err));
    }
  }

  const fw = pixelsOf(frameWidth);
  const fh = pixelsOf(frameHeight);
  let problem: string | null = null;
  if (file && (file.width < MIN_SIDE || file.height < MIN_SIDE)) problem = `Ảnh phải rộng và cao ít nhất ${MIN_SIDE} px.`;
  else if (!fw || !fh) problem = 'Nhập kích thước khung gốc (số px).';
  else if (file && (file.width > fw || file.height > fh)) problem = 'Ảnh lớn hơn khung gốc.';

  async function upload() {
    if (!file || !fw || !fh || problem) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await addWatermark(collectionId, { frame_width: fw, frame_height: fh, w: file.width, h: file.height }, file.png);
      setFile(null);
      setInputKey((k) => k + 1);
      setNotice('Đã tải ảnh mẫu lên.');
      await onAdded();
    } catch (err) {
      setError(`Không tải được ảnh lên: ${message(err)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-label="Tải ảnh mẫu lên" className="flex flex-col gap-2 rounded bg-surface-layer-2 p-3">
      <h3 className="text-xs font-medium text-text-secondary">Hoặc tải ảnh có sẵn lên</h3>
      <p className="text-xs text-text-secondary">
        Ảnh phải cắt sát watermark từ một khung hình gốc. Khung gốc là kích thước video nó được cắt ra (mặc định là video ở trên).
      </p>
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-xs text-text-secondary">
          File ảnh
          <input
            key={inputKey}
            type="file"
            accept="image/png,image/jpeg,image/webp"
            disabled={busy || full}
            onChange={(e) => void choose(e.target.files?.[0])}
            className="text-xs"
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-text-secondary">
          Khung gốc rộng
          <input type="text" inputMode="numeric" value={frameWidth} onChange={(e) => { typed.current = true; setFrameWidth(e.target.value); }} className="field w-24 font-mono" />
        </label>
        <label className="flex flex-col gap-1 text-xs text-text-secondary">
          Khung gốc cao
          <input type="text" inputMode="numeric" value={frameHeight} onChange={(e) => { typed.current = true; setFrameHeight(e.target.value); }} className="field w-24 font-mono" />
        </label>
        <button type="button" className="btn btn-primary" disabled={busy || full || !file || problem !== null} onClick={() => void upload()}>
          {busy ? 'Đang tải lên…' : 'Tải ảnh lên'}
        </button>
      </div>
      {file ? (
        <span className="font-mono text-[11px] text-text-muted">
          {file.name} · {file.width}×{file.height}
        </span>
      ) : null}
      {file && problem ? <span className="text-xs text-status-warning-text">{problem}</span> : null}
      {notice ? (
        <span role="status" className="text-xs text-status-success-text">
          {notice}
        </span>
      ) : null}
      {error ? (
        <span role="alert" className="text-xs text-status-error-text">
          {error}
        </span>
      ) : null}
    </section>
  );
}

/** The image of one item, fetched with the API key and shown from a blob URL. */
function WatermarkImage({ item }: { item: CollectionWatermark }) {
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    let url: string | null = null;
    void getWatermarkImage(item.image_url)
      .then((blob) => {
        if (!alive) return;
        url = URL.createObjectURL(blob);
        setSrc(url);
      })
      .catch(() => {
        if (alive) setFailed(true);
      });
    return () => {
      alive = false;
      if (url) URL.revokeObjectURL(url);
    };
  }, [item.image_url]);

  if (failed) return <span className="text-xs text-status-warning-text">Không tải được ảnh</span>;
  if (!src) return <span className="text-xs text-text-secondary">Đang tải…</span>;
  return (
    <img
      src={src}
      alt={`Watermark ${item.w}×${item.h} ${item.aweme_id === null ? 'tải lên từ file' : `cắt từ video ${item.aweme_id}`}`}
      // Shown at most 240 px wide; a small cut keeps its own size.
      className="max-h-24 max-w-[240px] bg-black object-contain"
    />
  );
}
