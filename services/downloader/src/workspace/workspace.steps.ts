/**
 * Which of the dashboard's four localization steps an activity belongs to.
 * Sits next to `workspace.catalog.ts`, which answers a different question (which
 * desk of the workspace office an activity belongs to).
 *
 * The order follows the workflow: prepare (download probe, speech to text,
 * singing and subtitle detection), translate (context and script), dub (slot
 * plan, dubbed script, voice, mixed audio), review (captions, the review
 * project, final render, publish). A name no rule matches is not guessed at:
 * `stepOf` answers `undefined` and the dashboard shows the step as unknown.
 */
export const STEPS = ['prepare', 'translate', 'dub', 'review'] as const;
export type Step = (typeof STEPS)[number];

/** First match wins; the generated `agent__*` names come before the plain ones. */
const RULES: ReadonlyArray<readonly [RegExp, Step]> = [
  [/^agent__localizer/, 'translate'],
  [/^agent__dubber/, 'dub'],
  [/^agent__review/, 'review'],
  [
    /^(ingest|classify_video|detect_subtitles|detect_singing|transcribe|vbee_stt|complete_transcript|clip_durations|localizer_source_evidence)$/,
    'prepare',
  ],
  [/^(localizer_context|assemble_script)$/, 'translate'],
  [
    /^(plan_slots|write_slot_script|dubber_context|dubber_setup|measure_fit|segment_room|write_dubbed_script|commit_dub|voice_rate|voice_profile|plan_synthesis|synthesize_segments|check_voice|assemble_audio|revoice_synthesize|revoice_mix|revoice_commit|revoice_failed)$/,
    'dub',
  ],
  [
    /^(layout_captions|caption_images|persist_json|capture_review_source|build_review_project|review_render|review_before_render|review_dub|review_jev|render|publish)$/,
    'review',
  ],
];

export function stepOf(activityName: string): Step | undefined {
  for (const [pattern, step] of RULES) {
    if (pattern.test(activityName)) return step;
  }
  return undefined;
}
