/**
 * The one table that says which agent or workflow step an activity (or an
 * `llm_calls.stage`) belongs to. Every other part of the workspace reads
 * through `resolveAgent`; none of them match a name on its own.
 *
 * The names come from reading the history of real workflows
 * (plans/reports/spike-261031-1430-temporal-activity-names.md). The queue is no
 * help: the Dubber's activities run on `io`, `llm` and `tts`. `llm_calls.stage`
 * uses the same names as the activities, so one table serves both sources.
 *
 * A name that matches no rule resolves to `unknown`, which the snapshot still
 * shows, so a new activity is visible instead of silently missing.
 */

export type Zone = 'media' | 'voice' | 'llm' | 'unknown';

export interface AgentRef {
  id: string;
  zone: Zone;
}

export const UNKNOWN_AGENT: AgentRef = { id: 'unknown', zone: 'unknown' };

/** Every desk the office draws, in display order. `unknown` is not a desk. */
export const AGENTS: readonly AgentRef[] = [
  { id: 'media', zone: 'media' },
  { id: 'render', zone: 'media' },
  { id: 'publish', zone: 'media' },
  { id: 'stt', zone: 'voice' },
  { id: 'tts', zone: 'voice' },
  { id: 'voice_check', zone: 'voice' },
  { id: 'ad_classifier', zone: 'llm' },
  { id: 'subtitle_detector', zone: 'llm' },
  { id: 'localizer', zone: 'llm' },
  { id: 'dubber', zone: 'llm' },
  { id: 'reviewer', zone: 'llm' },
];

const BY_ID = new Map(AGENTS.map((agent) => [agent.id, agent]));

/** First match wins. Prefix rules for the agents' generated names come first. */
const RULES: ReadonlyArray<readonly [RegExp, string]> = [
  [/^agent__localizer/, 'localizer'],
  [/^agent__dubber/, 'dubber'],
  [/^agent__review_jev/, 'reviewer'],
  [/^agent__reviewer/, 'reviewer'],
  [/^(ingest|assemble_audio|layout_captions|caption_images|persist_json|capture_review_source|build_review_project|revoice_mix)$/, 'media'],
  [/^render$/, 'render'],
  [/^(publish|revoice_commit|revoice_failed)$/, 'publish'],
  [/^transcribe$/, 'stt'],
  [/^(voice_rate|voice_profile|plan_synthesis|synthesize_segments|revoice_synthesize)$/, 'tts'],
  [/^check_voice$/, 'voice_check'],
  [/^classify_video$/, 'ad_classifier'],
  [/^detect_subtitles$/, 'subtitle_detector'],
  [/^(localizer_context|assemble_script)$/, 'localizer'],
  [
    /^(dubber_context|dubber_setup|measure_fit|segment_room|write_dubbed_script|commit_dub)$/,
    'dubber',
  ],
  [/^(review_render|review_before_render)$/, 'reviewer'],
  [/^review_jev$/, 'reviewer'],
];

export function resolveAgent(name: string): AgentRef {
  for (const [pattern, id] of RULES) {
    if (pattern.test(name)) return BY_ID.get(id) ?? UNKNOWN_AGENT;
  }
  return UNKNOWN_AGENT;
}
