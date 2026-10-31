import { useEffect, useRef, useState } from 'react';

export interface PollingState<T> {
  data: T | null;
  error: Error | null;
  /** True only before the first fetch has settled (success or failure). */
  loading: boolean;
}

/**
 * §8.1: "Polling 1s". DoD: "Polling tiếp tục sau khi mất mạng tạm rồi có
 * lại (không cần code reconnect)" -- a failed tick must not stop the
 * `setInterval`, must not throw out of the hook, and needs no special
 * reconnect logic: the next tick just tries again on its own.
 *
 * Fires the fetcher immediately on mount (not after the first `intervalMs`
 * wait) so the UI is not artificially blank for the first second.
 */
export interface PollingOptions {
  /** Skip ticks while the browser tab is hidden, and fetch at once when it shows again. */
  pauseWhenHidden?: boolean;
}

export function usePolling<T>(
  fetcher: () => Promise<T>,
  intervalMs: number,
  options: PollingOptions = {},
): PollingState<T> {
  const pauseWhenHidden = options.pauseWhenHidden ?? false;
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true);

  // Ref so the interval callback always calls the LATEST fetcher without
  // needing to be torn down and re-created every render (a `useCallback`less
  // caller would otherwise reset the interval's 1s clock on every parent
  // re-render, which is exactly what a poll interval must not do).
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  useEffect(() => {
    let cancelled = false;

    async function tick() {
      if (pauseWhenHidden && document.hidden) return;
      try {
        const result = await fetcherRef.current();
        if (cancelled) return;
        setData(result);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        // Deliberately does NOT clear `data`: a transient failure should not
        // blank out the last-known-good list. See the DoD comment above.
        setError(err instanceof Error ? err : new Error(String(err)));
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    void tick();
    const id = setInterval(() => {
      void tick();
    }, intervalMs);

    const onVisibility = () => {
      if (!document.hidden) void tick();
    };
    if (pauseWhenHidden) document.addEventListener('visibilitychange', onVisibility);

    return () => {
      cancelled = true;
      clearInterval(id);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [intervalMs, pauseWhenHidden]);

  return { data, error, loading };
}
