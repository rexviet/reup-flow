import { useCallback, useEffect, useState } from 'react';
import { getAutoQueue, setAutoQueue } from '../api/client';
import type { AutoQueueStatus } from '../api/types';
import { usePolling } from '../hooks/usePolling';

export const AUTO_QUEUE_POLL_MS = 10_000;

/**
 * Whether the end of a run (it reached review, or failed) starts the next held video.
 * Off, held videos stay held; the video already running still finishes.
 */
export function AutoQueueSwitch() {
  const fetcher = useCallback(() => getAutoQueue(), []);
  const polled = usePolling(fetcher, AUTO_QUEUE_POLL_MS, { pauseWhenHidden: true });
  const [status, setStatus] = useState<AutoQueueStatus | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (polled.data) setStatus(polled.data);
  }, [polled.data]);

  function flip(enabled: boolean) {
    setSaving(true);
    setError(null);
    void setAutoQueue(enabled)
      .then(setStatus)
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setSaving(false));
  }

  if (!status) {
    return (
      <p data-testid="auto-queue" className="text-xs text-text-secondary">
        {polled.error ? `Không đọc được hàng đợi tự động: ${polled.error.message}` : 'Hàng đợi tự động: đang tải…'}
      </p>
    );
  }
  return (
    <section data-testid="auto-queue" aria-label="Hàng đợi tự động" className="card flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
      <label className="flex items-center gap-2 font-medium text-text-primary">
        <input
          type="checkbox"
          role="switch"
          aria-checked={status.enabled}
          className="h-5 w-5"
          checked={status.enabled}
          disabled={saving}
          onChange={(e) => flip(e.target.checked)}
        />
        Tự chạy video đang giữ khi video trước tới bước chờ duyệt
      </label>
      <span className="text-xs text-text-secondary">
        {status.in_flight ? `Đang chạy: ${status.in_flight.aweme_id}` : 'Không có video nào đang chạy từ hàng đợi'}
        {' · '}
        {status.held} video đang giữ
      </span>
      {error ? (
        <span role="alert" className="text-xs text-status-error-text">
          Không đổi được: {error}
        </span>
      ) : null}
    </section>
  );
}
