import { useEffect, useState } from 'react';
import type { WorkspaceEvent, WorkspaceSnapshot } from '../api/types';

export const LOG_SIZE = 50;
export const BUBBLE_MS = 6000;
export const LINE_MS = 3500;
/** An event older than this when first seen (by the server's clock) is logged but not animated. */
export const ANIMATE_WITHIN_MS = 15_000;

const BUBBLE_KINDS = new Set<WorkspaceEvent['kind']>([
  'activity_completed',
  'activity_failed',
  'llm_call_recorded',
  'gate_decision',
]);

interface Active {
  event: WorkspaceEvent;
  expiresAt: number;
}

export interface Bubbles {
  text: string;
  awemeId?: string;
}

export interface EventFeed {
  /** Newest first, at most LOG_SIZE, kept for the life of the page. */
  log: WorkspaceEvent[];
  /** Latest bubble per desk id (with the video it is about); gate decisions land on `gate`. */
  bubbles: Record<string, Bubbles>;
  /** Handoffs that are still drawn. */
  handoffs: WorkspaceEvent[];
}

/**
 * Turns the server's `events` into three views. The server's event ids are the
 * source of truth: an id seen once never produces a second bubble, however many
 * polls return it. The first snapshot fills the log and animates nothing, so
 * opening the page does not replay the last ten minutes.
 */
export function useEventFeed(snapshot: WorkspaceSnapshot | null): EventFeed {
  const [seen] = useState(() => new Set<string>());
  const [loaded, setLoaded] = useState(false);
  const [log, setLog] = useState<WorkspaceEvent[]>([]);
  const [active, setActive] = useState<Active[]>([]);

  useEffect(() => {
    if (!snapshot) return;
    const fresh = snapshot.events.filter((e) => !seen.has(e.id));
    fresh.forEach((e) => seen.add(e.id));
    setLoaded(true);
    if (fresh.length === 0) return;

    setLog((prev) =>
      [...fresh, ...prev]
        .sort((a, b) => Date.parse(b.at) - Date.parse(a.at))
        .slice(0, LOG_SIZE),
    );
    if (!loaded) return;

    const serverNow = Date.parse(snapshot.generatedAt);
    const now = Date.now();
    const animated = fresh
      .filter((e) => serverNow - Date.parse(e.at) <= ANIMATE_WITHIN_MS)
      .filter((e) => e.kind === 'handoff' || BUBBLE_KINDS.has(e.kind))
      .map((event) => ({ event, expiresAt: now + (event.kind === 'handoff' ? LINE_MS : BUBBLE_MS) }));
    if (animated.length > 0) setActive((prev) => [...prev.filter((a) => a.expiresAt > now), ...animated]);
  }, [snapshot, seen, loaded]);

  useEffect(() => {
    if (active.length === 0) return;
    const next = Math.min(...active.map((a) => a.expiresAt));
    const id = setTimeout(() => {
      const now = Date.now();
      setActive((prev) => prev.filter((a) => a.expiresAt > now));
    }, Math.max(next - Date.now(), 0) + 1);
    return () => clearTimeout(id);
  }, [active]);

  const bubbles: Record<string, Bubbles> = {};
  const handoffs: WorkspaceEvent[] = [];
  for (const { event } of active) {
    if (event.kind === 'handoff') handoffs.push(event);
    else {
      bubbles[event.kind === 'gate_decision' ? 'gate' : (event.agentId ?? 'unknown')] = {
        text: event.detail,
        awemeId: event.awemeId,
      };
    }
  }
  return { log, bubbles, handoffs };
}
