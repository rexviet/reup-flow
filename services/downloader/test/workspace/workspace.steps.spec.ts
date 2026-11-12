import { readdirSync, readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { stepOf } from '../../src/workspace/workspace.steps';

/**
 * Hand-written from the `@activity.defn(name=...)` and `execute_activity` names in
 * services/localization/src. A new activity that is not added here and to
 * `workspace.steps.ts` fails the second test instead of showing up as "unknown".
 */
const EXPECTED: Record<string, 'prepare' | 'translate' | 'dub' | 'review'> = {
  ingest: 'prepare',
  classify_video: 'prepare',
  detect_subtitles: 'prepare',
  detect_singing: 'prepare',
  transcribe: 'prepare',
  vbee_stt: 'prepare',
  complete_transcript: 'prepare',
  clip_durations: 'prepare',
  localizer_source_evidence: 'prepare',
  localizer_context: 'translate',
  assemble_script: 'translate',
  agent__localizer: 'translate',
  plan_slots: 'dub',
  write_slot_script: 'dub',
  dubber_context: 'dub',
  dubber_setup: 'dub',
  measure_fit: 'dub',
  segment_room: 'dub',
  write_dubbed_script: 'dub',
  commit_dub: 'dub',
  voice_rate: 'dub',
  voice_profile: 'dub',
  plan_synthesis: 'dub',
  synthesize_segments: 'dub',
  check_voice: 'dub',
  assemble_audio: 'dub',
  revoice_synthesize: 'dub',
  revoice_mix: 'dub',
  revoice_commit: 'dub',
  revoice_failed: 'dub',
  agent__dubber: 'dub',
  layout_captions: 'review',
  persist_json: 'review',
  capture_review_source: 'review',
  build_review_project: 'review',
  review_render: 'review',
  review_before_render: 'review',
  review_dub: 'review',
  review_jev: 'review',
  agent__review_jev: 'review',
  agent__reviewer: 'review',
  render: 'review',
  publish: 'review',
};

describe('stepOf', () => {
  it.each(Object.entries(EXPECTED))('%s is %s', (name, step) => {
    expect(stepOf(name)).toBe(step);
  });

  it('covers the eight activities no workspace desk rule matches', () => {
    for (const name of [
      'clip_durations',
      'complete_transcript',
      'detect_singing',
      'localizer_source_evidence',
      'plan_slots',
      'review_dub',
      'vbee_stt',
      'write_slot_script',
    ]) {
      expect(stepOf(name)).toBeDefined();
    }
  });

  it('does not guess for a name it has never seen', () => {
    expect(stepOf('brand_new_activity')).toBeUndefined();
    expect(stepOf('')).toBeUndefined();
  });
});

describe('stepOf against the localization source', () => {
  function pythonFiles(dir: string): string[] {
    return readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
      e.isDirectory() ? pythonFiles(join(dir, e.name)) : e.name.endsWith('.py') ? [join(dir, e.name)] : [],
    );
  }

  it('maps every name declared with @activity.defn(name=...)', () => {
    const root = resolve(__dirname, '../../../localization/src');
    const names = new Set<string>();
    for (const file of pythonFiles(root)) {
      for (const m of readFileSync(file, 'utf8').matchAll(/@activity\.defn\(\s*name="([a-z_]+)"/g)) names.add(m[1]);
    }
    expect(names.size).toBeGreaterThan(20);
    expect([...names].filter((n) => stepOf(n) === undefined)).toEqual([]);
  });
});
