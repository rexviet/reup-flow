import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { BUBBLE_MS, LINE_MS, LOG_SIZE, useEventFeed } from './useEventFeed';
import { event, snapshot } from './fixtures';
import type { WorkspaceSnapshot } from '../api/types';

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

const feed = (initial: WorkspaceSnapshot | null) =>
  renderHook(({ s }) => useEventFeed(s), { initialProps: { s: initial } });

describe('useEventFeed', () => {
  it('logs the first snapshot but animates nothing, so opening the page does not replay history', () => {
    const { result } = feed(snapshot({ events: [event({ id: 'a' }), event({ id: 'h', kind: 'handoff', toAgentId: 'stt' })] }));
    expect(result.current.log.map((e) => e.id).sort()).toEqual(['a', 'h']);
    expect(result.current.bubbles).toEqual({});
    expect(result.current.handoffs).toEqual([]);
  });

  it('opens a bubble for a new completion, not for the same event returned by the next poll', () => {
    const { result, rerender } = feed(snapshot());
    const first = snapshot({ events: [event({ id: 'a', agentId: 'dubber', detail: 'dubber done in 3.0s' })] });
    rerender({ s: first });
    expect(result.current.bubbles).toEqual({ dubber: { text: 'dubber done in 3.0s', awemeId: undefined } });
    rerender({ s: { ...first } });
    rerender({ s: { ...first } });
    expect(result.current.log).toHaveLength(1);
    act(() => void vi.advanceTimersByTime(BUBBLE_MS + 10));
    expect(result.current.bubbles).toEqual({});
    // The same id coming back after the bubble closed does not reopen it.
    rerender({ s: { ...first } });
    expect(result.current.bubbles).toEqual({});
  });

  it('opens bubbles for failure, llm call and gate decision, but not for started or retrying', () => {
    const { result, rerender } = feed(snapshot());
    rerender({
      s: snapshot({
        events: [
          event({ id: '1', kind: 'activity_failed', agentId: 'render', detail: 'render failed: RenderError' }),
          event({ id: '2', kind: 'llm_call_recorded', agentId: 'localizer', detail: 'm, 5 tokens, $0.0001' }),
          event({ id: '3', kind: 'gate_decision', agentId: undefined, detail: 'skipped: advertisement' }),
          event({ id: '4', kind: 'activity_started', agentId: 'stt', detail: 'transcribe started' }),
          event({ id: '5', kind: 'activity_retrying', agentId: 'tts', detail: 'tts: attempt 2' }),
        ],
      }),
    });
    expect(result.current.bubbles).toEqual({
      render: { text: 'render failed: RenderError', awemeId: undefined },
      localizer: { text: 'm, 5 tokens, $0.0001', awemeId: undefined },
      gate: { text: 'skipped: advertisement', awemeId: undefined },
    });
    expect(result.current.log).toHaveLength(5);
  });

  it('draws a line only for a handoff, and removes it after LINE_MS', () => {
    const { result, rerender } = feed(snapshot());
    rerender({ s: snapshot({ events: [event({ id: 'h', kind: 'handoff', agentId: 'media', toAgentId: 'stt' })] }) });
    expect(result.current.handoffs.map((h) => h.id)).toEqual(['h']);
    expect(result.current.bubbles).toEqual({});
    act(() => void vi.advanceTimersByTime(LINE_MS + 10));
    expect(result.current.handoffs).toEqual([]);
    expect(result.current.log).toHaveLength(1);
  });

  it('logs but does not animate an event that was already old when first seen', () => {
    const { result, rerender } = feed(snapshot());
    rerender({ s: snapshot({ events: [event({ id: 'old', at: '2026-10-31T06:50:00.000Z' })] }) });
    expect(result.current.bubbles).toEqual({});
    expect(result.current.log).toHaveLength(1);
  });

  it('keeps the newest LOG_SIZE events, newest first', () => {
    const { result, rerender } = feed(snapshot());
    const many = Array.from({ length: LOG_SIZE + 10 }, (_, i) =>
      event({ id: `e${i}`, at: new Date(Date.parse('2026-10-31T06:00:00Z') + i * 1000).toISOString(), kind: 'activity_started' }),
    );
    rerender({ s: snapshot({ events: many }) });
    expect(result.current.log).toHaveLength(LOG_SIZE);
    expect(result.current.log[0].id).toBe(`e${LOG_SIZE + 9}`);
  });

  it('shows no bubbles and no lines with no events', () => {
    const { result } = feed(snapshot());
    expect(result.current).toEqual({ log: [], bubbles: {}, handoffs: [] });
  });
});
