import { useCallback, useEffect, useRef, useState } from 'react';
import { getWorkspaceSnapshot } from '../api/client';
import type { WorkspaceSnapshot } from '../api/types';
import { usePolling } from '../hooks/usePolling';

export const SNAPSHOT_INTERVAL_MS = 2000;

export interface WorkspaceSnapshotState {
  snapshot: WorkspaceSnapshot | null;
  loading: boolean;
  /** Seconds since the last successful poll while polls are failing; 0 when current. */
  staleSeconds: number;
}

/**
 * Polls every 2 seconds and stops while the tab is hidden. When a poll fails the
 * last snapshot is kept but `staleSeconds` says how old it is, so the page never
 * shows old data as if it were new.
 */
export function useWorkspaceSnapshot(): WorkspaceSnapshotState {
  const fetcher = useCallback(() => getWorkspaceSnapshot(), []);
  const { data, error, loading } = usePolling(fetcher, SNAPSHOT_INTERVAL_MS, { pauseWhenHidden: true });

  const lastOkAt = useRef<number>(Date.now());
  useEffect(() => {
    if (data) lastOkAt.current = Date.now();
  }, [data]);

  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!error) return;
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [error]);

  const staleSeconds = error ? Math.max(1, Math.round((now - lastOkAt.current) / 1000)) : 0;
  return { snapshot: data, loading, staleSeconds };
}
