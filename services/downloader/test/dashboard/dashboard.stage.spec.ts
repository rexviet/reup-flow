import {
  attentionOf,
  computeStage,
  currentGeneration,
  LOST_AFTER_MS,
  type OpenGeneration,
  type StageFacts,
} from '../../src/dashboard/dashboard.stage';

const NOW = Date.parse('2026-11-12T10:00:00Z');
const MIN = 60_000;

const facts = (over: Partial<StageFacts> = {}): StageFacts => ({
  videoStatus: 'done',
  outbox: { generation: 1, state: 'pending', dispatchedAtMs: null },
  runs: [],
  decisions: [],
  renders: [],
  publications: [],
  reviewStatus: [],
  ...over,
});
const run = (generation: number, status: string, hasOutputKey = false) => ({ generation, status, hasOutputKey });
const open = (generation: number, name?: string, attempt = 1, atMs = NOW - MIN): OpenGeneration => ({
  generation,
  pending: name ? [{ name, attempt, atMs }] : [],
});
const ctx = (o?: readonly OpenGeneration[]) => ({ nowMs: NOW, stuckMs: 30 * MIN, open: o });

describe('computeStage', () => {
  it.each([
    ['pending', 'downloading'],
    ['downloading', 'downloading'],
    ['failed', 'download_failed'],
    ['skipped', 'download_failed'],
  ])('download status %s is %s', (videoStatus, stage) => {
    expect(computeStage(facts({ videoStatus, outbox: null }), [])).toMatchObject({ stage, currentGen: null });
  });

  it('a downloaded video with no outbox row and no run is held', () => {
    expect(computeStage(facts({ outbox: null }), [])).toEqual({ currentGen: null, stage: 'held' });
  });

  it.each([
    ['held', 'held'],
    ['pending', 'queued'],
    ['dispatched', 'queued'],
    ['rejected', 'dispatch_failed'],
    ['dead', 'dispatch_failed'],
  ])('outbox %s with nothing running is %s', (state, stage) => {
    const f = facts({ outbox: { generation: 1, state, dispatchedAtMs: NOW - MIN } });
    expect(computeStage(f, []).stage).toBe(stage);
  });

  it.each([
    ['ingest', 'prepare'],
    ['agent__localizer_x', 'translate'],
    ['write_dubbed_script', 'dub'],
    ['review_before_render', 'review'],
    ['something_new', 'unknown'],
  ])('an open workflow running %s is localizing at step %s', (activity, step) => {
    const f = facts({ outbox: { generation: 1, state: 'dispatched', dispatchedAtMs: NOW - MIN } });
    expect(computeStage(f, [open(1, activity)])).toMatchObject({ stage: 'localizing', step });
  });

  it('an open workflow between activities shows the step as unknown, not idle', () => {
    const f = facts({ outbox: { generation: 1, state: 'dispatched', dispatchedAtMs: NOW - MIN } });
    expect(computeStage(f, [open(1)])).toMatchObject({ stage: 'localizing', step: 'unknown' });
  });

  it('a run row ends the localizing stage even if the workflow is still listed', () => {
    const f = facts({ runs: [run(1, 'ready_for_review')] });
    expect(computeStage(f, [open(1, 'publish')]).stage).toBe('awaiting_review');
  });

  it.each([
    ['failed', 'run_failed'],
    ['skipped_ad', 'skipped'],
    ['skipped_no_video', 'skipped'],
    ['ready_for_review', 'awaiting_review'],
    ['needs_human', 'awaiting_review'],
    ['mystery_status', 'run_failed'],
  ])('run status %s is %s', (status, stage) => {
    expect(computeStage(facts({ runs: [run(1, status)] }), []).stage).toBe(stage);
  });

  it('a legacy published run counts as rendered only when it has an output', () => {
    expect(computeStage(facts({ runs: [run(1, 'published', true)] }), []).stage).toBe('rendered');
    expect(computeStage(facts({ runs: [run(1, 'published', false)] }), []).stage).toBe('run_failed');
  });

  it('a returned decision is returned', () => {
    const f = facts({ runs: [run(1, 'ready_for_review')], decisions: [{ generation: 1, decision: 'returned' }] });
    expect(computeStage(f, []).stage).toBe('returned');
  });

  it.each([
    [undefined, 'render_queued'],
    ['queued', 'render_queued'],
    ['rendering', 'rendering'],
    ['failed', 'render_failed'],
    ['rendered', 'rendered'],
  ])('an approved run whose render is %s is %s', (state, stage) => {
    const f = facts({
      runs: [run(1, 'ready_for_review')],
      decisions: [{ generation: 1, decision: 'approved' }],
      renders: state ? [{ generation: 1, state }] : [],
    });
    expect(computeStage(f, []).stage).toBe(stage);
  });

  it('a published publication outranks the render state', () => {
    const f = facts({
      runs: [run(1, 'ready_for_review')],
      decisions: [{ generation: 1, decision: 'approved' }],
      renders: [{ generation: 1, state: 'rendered' }],
      publications: [{ generation: 1, state: 'published' }],
    });
    expect(computeStage(f, []).stage).toBe('published');
  });

  describe('current generation', () => {
    it('is the newest of outbox, runs and open workflows', () => {
      const f = facts({ outbox: { generation: 1, state: 'dispatched', dispatchedAtMs: null }, runs: [run(3, 'failed')] });
      expect(currentGeneration(f, [open(7, 'ingest')])).toBe(7);
      expect(currentGeneration(f, [])).toBe(3);
    });

    it('a canary run newer than the outbox decides the stage, not the outbox', () => {
      // Real data: outbox g1 dispatched, runs g3 awaiting review.
      const f = facts({
        outbox: { generation: 1, state: 'dispatched', dispatchedAtMs: NOW - 60 * MIN },
        runs: [run(1, 'needs_human'), run(3, 'ready_for_review')],
      });
      expect(computeStage(f, [])).toMatchObject({ currentGen: 3, stage: 'awaiting_review' });
    });

    it('a workflow open on a newer generation than any run is localizing that generation', () => {
      const f = facts({
        outbox: { generation: 1, state: 'dispatched', dispatchedAtMs: NOW - 60 * MIN },
        runs: [run(1, 'ready_for_review')],
      });
      expect(computeStage(f, [open(2, 'transcribe')])).toMatchObject({ currentGen: 2, stage: 'localizing', step: 'prepare' });
    });

    it('does not apply the outbox state to a generation the outbox does not hold', () => {
      const f = facts({ outbox: { generation: 1, state: 'dead', dispatchedAtMs: null }, runs: [run(2, 'ready_for_review')] });
      expect(computeStage(f, []).stage).toBe('awaiting_review');
    });
  });

  describe('Temporal unreadable', () => {
    it('a dispatched video shows localizing with an unknown step, never idle', () => {
      const f = facts({ outbox: { generation: 1, state: 'dispatched', dispatchedAtMs: NOW - 60 * MIN } });
      expect(computeStage(f, undefined)).toMatchObject({ stage: 'localizing', step: 'unknown', unknown: true });
    });

    it('marks any stage past the download as a best guess, since a running canary is invisible', () => {
      expect(computeStage(facts({ runs: [run(1, 'failed')] }), undefined)).toMatchObject({ stage: 'run_failed', unknown: true });
      expect(computeStage(facts({ videoStatus: 'downloading', outbox: null }), undefined).unknown).toBeUndefined();
    });

    it('stages that do not depend on Temporal are unchanged', () => {
      expect(computeStage(facts(), undefined).stage).toBe('queued');
      expect(computeStage(facts({ runs: [run(1, 'failed')] }), undefined).stage).toBe('run_failed');
    });
  });
});

describe('attentionOf', () => {
  const codes = (f: StageFacts, o?: readonly OpenGeneration[]) => {
    const result = computeStage(f, o);
    return attentionOf(f, result, ctx(o)).map((a) => a.code);
  };

  it('is empty for a healthy queued video', () => {
    expect(codes(facts(), [])).toEqual([]);
  });

  it.each([
    [facts({ videoStatus: 'failed', outbox: null }), 'download_failed'],
    [facts({ outbox: { generation: 1, state: 'rejected', dispatchedAtMs: null } }), 'dispatch_failed'],
    [facts({ outbox: { generation: 1, state: 'dead', dispatchedAtMs: null } }), 'dispatch_dead'],
    [facts({ runs: [run(1, 'failed')] }), 'run_failed'],
    [
      facts({
        runs: [run(1, 'ready_for_review')],
        decisions: [{ generation: 1, decision: 'approved' }],
        renders: [{ generation: 1, state: 'failed' }],
      }),
      'render_failed',
    ],
    [facts({ runs: [run(1, 'needs_human')] }), 'needs_human'],
  ])('flags case %#', (f, code) => {
    expect(codes(f, [])).toContain(code);
  });

  it('flags mismatches and unsaved changes while awaiting review', () => {
    const f = facts({
      runs: [run(1, 'ready_for_review')],
      reviewStatus: [{ generation: 1, mismatchCount: 2, dirty: true }],
    });
    expect(codes(f, [])).toEqual(expect.arrayContaining(['mismatch', 'dirty']));
  });

  it('ignores mismatches once the revision is approved', () => {
    const f = facts({
      runs: [run(1, 'ready_for_review')],
      decisions: [{ generation: 1, decision: 'approved' }],
      reviewStatus: [{ generation: 1, mismatchCount: 2, dirty: true }],
    });
    expect(codes(f, [])).toEqual([]);
  });

  it('flags an activity on its second attempt and one that runs too long', () => {
    const f = facts({ outbox: { generation: 1, state: 'dispatched', dispatchedAtMs: NOW - 60 * MIN } });
    expect(codes(f, [open(1, 'transcribe', 2)])).toContain('retrying');
    expect(codes(f, [open(1, 'transcribe', 1, NOW - 31 * MIN)])).toContain('stuck');
    expect(codes(f, [open(1, 'transcribe', 1, NOW - 29 * MIN)])).toEqual([]);
  });

  it('flags a lost video: dispatched over ten minutes ago, no workflow, no run', () => {
    const lost = facts({ outbox: { generation: 1, state: 'dispatched', dispatchedAtMs: NOW - LOST_AFTER_MS - MIN } });
    expect(codes(lost, [])).toContain('lost');
    const fresh = facts({ outbox: { generation: 1, state: 'dispatched', dispatchedAtMs: NOW - 2 * MIN } });
    expect(codes(fresh, [])).not.toContain('lost');
  });

  it('does not call a video lost when Temporal could not be read', () => {
    const f = facts({ outbox: { generation: 1, state: 'dispatched', dispatchedAtMs: NOW - 60 * MIN } });
    expect(codes(f, undefined)).toEqual([]);
  });

  it('flags an older generation still waiting for a decision', () => {
    const f = facts({
      outbox: { generation: 1, state: 'dispatched', dispatchedAtMs: null },
      runs: [run(1, 'ready_for_review'), run(3, 'ready_for_review')],
      decisions: [{ generation: 3, decision: 'approved' }],
      renders: [{ generation: 3, state: 'rendered' }],
    });
    expect(codes(f, [])).toContain('old_generation_awaiting_review');
    const many = facts({
      runs: [run(1, 'needs_human'), run(2, 'needs_human'), run(3, 'ready_for_review')],
    });
    const manyItems = attentionOf(many, computeStage(many, []), ctx([]));
    expect(manyItems.filter((i) => i.code === 'old_generation_awaiting_review')).toMatchObject([
      { reason: expect.stringContaining('g1, g2') },
    ]);
    const projectless = facts({
      runs: [
        { ...run(1, 'needs_human'), hasProject: false },
        run(3, 'ready_for_review'),
      ],
    });
    expect(codes(projectless, [])).not.toContain('old_generation_awaiting_review');
    const decided = { ...f, decisions: [...f.decisions, { generation: 1, decision: 'returned' }] };
    expect(codes(decided, [])).not.toContain('old_generation_awaiting_review');
  });

  it('gives every item a Vietnamese reason and action', () => {
    const f = facts({ runs: [run(1, 'failed')] });
    const [item] = attentionOf(f, computeStage(f, []), ctx([]));
    expect(item.reason).toMatch(/\S/);
    expect(item.action).toMatch(/\S/);
  });
});
