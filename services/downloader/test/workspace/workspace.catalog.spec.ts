import { AGENTS, resolveAgent } from '../../src/workspace/workspace.catalog';

/** Every activity name seen in the history of 12 real workflows (spike report). */
const SEEN_IN_REAL_HISTORY = [
  'ingest', 'assemble_audio', 'layout_captions', 'render', 'publish', 'transcribe',
  'voice_rate', 'voice_profile', 'plan_synthesis', 'synthesize_segments', 'check_voice',
  'classify_video', 'detect_subtitles', 'localizer_context', 'assemble_script',
  'agent__localizer_single__model_request', 'dubber_setup', 'measure_fit', 'segment_room',
  'write_dubbed_script', 'commit_dub', 'agent__dubber_code__model_request',
  'agent__dubber_code__toolset__<agent>__call_tool', 'review_before_render',
];

/** `llm_calls.stage` values in the real table. */
const STAGES = [
  'agent__localizer_repair__model_request', 'agent__localizer_single__model_request',
  'review_jev', 'review_render', 'detect_subtitles',
  'agent__dubber__model_request', 'agent__dubber_single__model_request',
  'agent__dubber_code__model_request', 'agent__localizer__model_request', 'classify_video',
];

describe('workspace catalog', () => {
  it.each([...SEEN_IN_REAL_HISTORY, ...STAGES])('places %s on a desk', (name) => {
    expect(resolveAgent(name).id).not.toBe('unknown');
  });

  it('sends an activity it does not know to unknown, so it is shown and not dropped', () => {
    expect(resolveAgent('some_new_activity')).toEqual({ id: 'unknown', zone: 'unknown' });
  });

  it('puts the Dubber tool calls on the Dubber whichever queue they ran on', () => {
    expect(resolveAgent('agent__dubber_code__toolset__<agent>__call_tool').id).toBe('dubber');
  });

  it('lists each desk once', () => {
    const ids = AGENTS.map((a) => a.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it.each(['agent__slot_shortener__model_request', 'vbee_stt', 'plan_slots', 'write_slot_script', 'review_dub'])(
    'does not assign the Vbee-only step %s to a standard-pipeline desk', (name) => {
      expect(resolveAgent(name).id).toBe('unknown');
    },
  );
});

it.each([
  ['capture_review_source', 'media'], ['build_review_project', 'media'],
  ['revoice_synthesize', 'tts'], ['revoice_mix', 'media'],
  ['revoice_commit', 'publish'], ['revoice_failed', 'publish'],
])('maps deterministic review activity %s to existing desk %s', (name, desk) => {
  expect(resolveAgent(name).id).toBe(desk);
});
