import { useCallback, useState } from 'react';
import {
  getAttention,
  getChannels,
  getDashboardVideo,
  getDashboardVideos,
  getHealth,
  listCollections,
  listJobs,
  runAction,
} from '../api/client';
import type { ActionRequest, ActionResult, VideoListQuery } from '../api/types';
import { usePolling } from '../hooks/usePolling';

export const HEALTH_POLL_MS = 10_000;
export const LIST_POLL_MS = 3_000;

export function useHealth() {
  const fetcher = useCallback(() => getHealth(), []);
  return usePolling(fetcher, HEALTH_POLL_MS, { pauseWhenHidden: true });
}

export function useVideoList(query: VideoListQuery) {
  const key = JSON.stringify(query);
  // `key` is the query's identity: the fetcher is rebuilt only when it changes.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const fetcher = useCallback(() => getDashboardVideos(query), [key]);
  return usePolling(fetcher, LIST_POLL_MS, { pauseWhenHidden: true, key });
}

export function useAttention() {
  const fetcher = useCallback(() => getAttention(), []);
  return usePolling(fetcher, LIST_POLL_MS, { pauseWhenHidden: true });
}

export function useChannels() {
  const fetcher = useCallback(() => getChannels(), []);
  return usePolling(fetcher, LIST_POLL_MS, { pauseWhenHidden: true });
}

/** Collections change only when someone edits them here, so a slower poll is enough. */
export const COLLECTIONS_POLL_MS = 10_000;

export function useCollections() {
  const fetcher = useCallback(() => listCollections(), []);
  return usePolling(fetcher, COLLECTIONS_POLL_MS, { pauseWhenHidden: true });
}

export function useJobs() {
  const fetcher = useCallback(() => listJobs(100, 0), []);
  return usePolling(fetcher, LIST_POLL_MS, { pauseWhenHidden: true });
}

export function useVideoDetail(awemeId: string) {
  const fetcher = useCallback(() => getDashboardVideo(awemeId), [awemeId]);
  return usePolling(fetcher, LIST_POLL_MS, { pauseWhenHidden: true, key: awemeId });
}

export interface ActionRunner {
  busy: boolean;
  result: (ActionResult & { action: string }) | null;
  error: string | null;
  run: (body: ActionRequest) => Promise<ActionResult | null>;
  clear: () => void;
}

/** Posts one action and keeps its outcome (done count, skipped reasons) for display; `onSettled` refreshes the polls. */
export function useActionRunner(onSettled: () => void): ActionRunner {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ActionRunner['result']>(null);
  const [error, setError] = useState<string | null>(null);

  const run = useCallback(
    async (body: ActionRequest) => {
      setBusy(true);
      setError(null);
      try {
        const res = await runAction(body);
        setResult({ ...res, action: body.action });
        return res;
      } catch (err) {
        setResult(null);
        setError(err instanceof Error ? err.message : String(err));
        return null;
      } finally {
        setBusy(false);
        onSettled();
      }
    },
    [onSettled],
  );

  const clear = useCallback(() => {
    setResult(null);
    setError(null);
  }, []);

  return { busy, result, error, run, clear };
}
