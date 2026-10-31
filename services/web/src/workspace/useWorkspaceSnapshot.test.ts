import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as client from '../api/client';
import { snapshot } from './fixtures';
import { SNAPSHOT_INTERVAL_MS, useWorkspaceSnapshot } from './useWorkspaceSnapshot';

vi.mock('../api/client');

function setHidden(hidden: boolean) {
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
  document.dispatchEvent(new Event('visibilitychange'));
}

const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve(); });
const advance = (ms: number) => act(async () => { vi.advanceTimersByTime(ms); await Promise.resolve(); await Promise.resolve(); });

beforeEach(() => {
  vi.useFakeTimers();
  setHidden(false);
});
afterEach(() => {
  vi.useRealTimers();
  vi.resetAllMocks();
});

describe('useWorkspaceSnapshot', () => {
  it('polls at once and then every 2 seconds', async () => {
    vi.mocked(client.getWorkspaceSnapshot).mockResolvedValue(snapshot());
    const { result } = renderHook(() => useWorkspaceSnapshot());
    await flush();
    expect(client.getWorkspaceSnapshot).toHaveBeenCalledTimes(1);
    expect(result.current.snapshot).not.toBeNull();
    await advance(SNAPSHOT_INTERVAL_MS);
    await advance(SNAPSHOT_INTERVAL_MS);
    expect(client.getWorkspaceSnapshot).toHaveBeenCalledTimes(3);
    expect(result.current.staleSeconds).toBe(0);
  });

  it('stops polling while the tab is hidden and fetches again when it shows', async () => {
    vi.mocked(client.getWorkspaceSnapshot).mockResolvedValue(snapshot());
    renderHook(() => useWorkspaceSnapshot());
    await flush();
    setHidden(true);
    await advance(SNAPSHOT_INTERVAL_MS * 5);
    expect(client.getWorkspaceSnapshot).toHaveBeenCalledTimes(1);
    setHidden(false);
    await flush();
    expect(client.getWorkspaceSnapshot).toHaveBeenCalledTimes(2);
  });

  it('keeps the last snapshot but reports how stale it is when polls fail', async () => {
    vi.mocked(client.getWorkspaceSnapshot).mockResolvedValueOnce(snapshot());
    const { result } = renderHook(() => useWorkspaceSnapshot());
    await flush();
    vi.mocked(client.getWorkspaceSnapshot).mockRejectedValue(new Error('network'));
    await advance(SNAPSHOT_INTERVAL_MS);
    await advance(SNAPSHOT_INTERVAL_MS);
    await advance(SNAPSHOT_INTERVAL_MS);
    expect(result.current.snapshot).not.toBeNull();
    expect(result.current.staleSeconds).toBeGreaterThanOrEqual(4);
  });

  it('is current again after a poll succeeds', async () => {
    vi.mocked(client.getWorkspaceSnapshot).mockResolvedValueOnce(snapshot());
    const { result } = renderHook(() => useWorkspaceSnapshot());
    await flush();
    vi.mocked(client.getWorkspaceSnapshot).mockRejectedValueOnce(new Error('x'));
    await advance(SNAPSHOT_INTERVAL_MS);
    expect(result.current.staleSeconds).toBeGreaterThan(0);
    vi.mocked(client.getWorkspaceSnapshot).mockResolvedValue(snapshot());
    await advance(SNAPSHOT_INTERVAL_MS);
    expect(result.current.staleSeconds).toBe(0);
  });
});
