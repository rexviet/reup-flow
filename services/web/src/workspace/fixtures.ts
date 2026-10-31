import type { RunDetail, WorkspaceAgent, WorkspaceEvent, WorkspaceSnapshot } from '../api/types';

/** A snapshot with every source up and nothing happening; tests override what they need. */
export function snapshot(overrides: Partial<WorkspaceSnapshot> = {}): WorkspaceSnapshot {
  return {
    generatedAt: '2026-10-31T07:00:00.000Z',
    sources: { temporal: 'ok', postgres: 'ok' },
    agents: [],
    events: [],
    runs: [],
    ...overrides,
  };
}

export function agent(overrides: Partial<WorkspaceAgent> & Pick<WorkspaceAgent, 'id'>): WorkspaceAgent {
  return { zone: 'llm', state: 'idle', ...overrides };
}

export function event(overrides: Partial<WorkspaceEvent> & Pick<WorkspaceEvent, 'id'>): WorkspaceEvent {
  return {
    at: '2026-10-31T06:59:59.000Z',
    kind: 'activity_completed',
    agentId: 'media',
    detail: 'ingest done in 3.5s',
    ...overrides,
  };
}

export function detail(overrides: Partial<RunDetail> = {}): RunDetail {
  return {
    awemeId: '7665948504426075877',
    generation: 7,
    sources: { temporal: 'ok', postgres: 'ok' },
    status: 'published',
    phases: [],
    llmCostUsd: 0,
    costByStage: [],
    gpuSecondsEstimate: { stt: 0, tts: 0 },
    ...overrides,
  };
}
