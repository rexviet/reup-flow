import { renderHook, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { usePolling } from './usePolling';

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('usePolling', () => {
  it('calls the fetcher immediately, then every 1s', async () => {
    const fetcher = vi.fn().mockResolvedValue('v1');
    renderHook(() => usePolling(fetcher, 1000));

    await act(async () => {
      await Promise.resolve();
    });
    expect(fetcher).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(1000);
      await Promise.resolve();
    });
    expect(fetcher).toHaveBeenCalledTimes(2);

    await act(async () => {
      vi.advanceTimersByTime(1000);
      await Promise.resolve();
    });
    expect(fetcher).toHaveBeenCalledTimes(3);
  });

  it('exposes the latest resolved value via `data`', async () => {
    const fetcher = vi.fn().mockResolvedValue({ hello: 'world' });
    const { result } = renderHook(() => usePolling(fetcher, 1000));

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.data).toEqual({ hello: 'world' });
  });

  it('DoD: polling continues after one tick fails (a transient network drop), with no reconnect code needed', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce('ok-1')
      .mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValueOnce('ok-2');

    const { result } = renderHook(() => usePolling(fetcher, 1000));

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.data).toBe('ok-1');

    await act(async () => {
      vi.advanceTimersByTime(1000);
      await Promise.resolve();
      await Promise.resolve();
    });
    // The failed tick must not stop the interval and must not crash the hook.
    expect(result.current.data).toBe('ok-1');
    expect(result.current.error).toBeInstanceOf(Error);

    await act(async () => {
      vi.advanceTimersByTime(1000);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(fetcher).toHaveBeenCalledTimes(3);
    expect(result.current.data).toBe('ok-2');
    expect(result.current.error).toBeNull();
  });

  it('stops polling on unmount', async () => {
    const fetcher = vi.fn().mockResolvedValue('v1');
    const { unmount } = renderHook(() => usePolling(fetcher, 1000));
    await act(async () => {
      await Promise.resolve();
    });
    unmount();

    fetcher.mockClear();
    await act(async () => {
      vi.advanceTimersByTime(5000);
      await Promise.resolve();
    });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it('refetches at once when the key changes and keeps the old data until the answer arrives', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce('a').mockResolvedValueOnce('b');
    const { result, rerender } = renderHook(({ k }) => usePolling(fetcher, 60_000, { key: k }), { initialProps: { k: 'one' } });
    await act(async () => {
      await Promise.resolve();
    });
    expect(result.current.data).toBe('a');

    rerender({ k: 'two' });
    expect(result.current.data).toBe('a');
    await act(async () => {
      await Promise.resolve();
    });
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(result.current.data).toBe('b');
  });

  it('refresh() fetches outside the interval', async () => {
    const fetcher = vi.fn().mockResolvedValue('v');
    const { result } = renderHook(() => usePolling(fetcher, 60_000));
    await act(async () => {
      await Promise.resolve();
    });
    await act(async () => {
      result.current.refresh();
      await Promise.resolve();
    });
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('ignores a slow answer that arrives after a newer one', async () => {
    let resolveSlow: (v: string) => void = () => {};
    const fetcher = vi
      .fn()
      .mockImplementationOnce(() => new Promise<string>((resolve) => { resolveSlow = resolve; }))
      .mockResolvedValueOnce('fresh');
    const { result } = renderHook(() => usePolling(fetcher, 60_000));
    await act(async () => {
      result.current.refresh();
      await Promise.resolve();
    });
    expect(result.current.data).toBe('fresh');
    await act(async () => {
      resolveSlow('stale');
      await Promise.resolve();
    });
    expect(result.current.data).toBe('fresh');
  });
});
