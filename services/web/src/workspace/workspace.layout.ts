import type { GateOutcome, Zone } from '../api/types';

/**
 * The fixed floor plan: which desks exist and in which zone. The ids are the
 * downloader's catalog ids (services/downloader/src/workspace/workspace.catalog.ts);
 * workspace.layout.test.ts fails if the two lists drift apart.
 */
export interface DeskSpec {
  id: string;
  label: string;
  /** Top-left of the station in the office's 1000x660 drawing. */
  x: number;
  y: number;
}

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface ZoneSpec {
  room: Rect;
  zone: Exclude<Zone, 'unknown'>;
  title: string;
  desks: DeskSpec[];
}

export const OFFICE_SIZE = { w: 1000, h: 520 };
// Includes the desk name, state and video tag below the furniture.
export const STATION = { w: 130, h: 128 };

export const ZONES: ZoneSpec[] = [
  {
    room: { x: 20, y: 20, w: 450, h: 180 },
    zone: 'media',
    title: 'Media',
    desks: [
      { id: 'media', label: 'Media', x: 40, y: 50 },
      { id: 'render', label: 'Render', x: 190, y: 50 },
      { id: 'publish', label: 'Publish', x: 330, y: 50 },
    ],
  },
  {
    room: { x: 490, y: 20, w: 490, h: 180 },
    zone: 'voice',
    title: 'Voice',
    desks: [
      { id: 'stt', label: 'STT', x: 510, y: 50 },
      { id: 'tts', label: 'TTS', x: 660, y: 50 },
      { id: 'voice_check', label: 'Voice check', x: 810, y: 50 },
    ],
  },
  {
    room: { x: 20, y: 215, w: 960, h: 290 },
    zone: 'llm',
    title: 'LLM',
    desks: [
      { id: 'ad_classifier', label: 'Ad Classifier', x: 50, y: 250 },
      { id: 'subtitle_detector', label: 'Subtitle Detector', x: 290, y: 250 },
      { id: 'localizer', label: 'Localizer Agent', x: 530, y: 250 },
      { id: 'dubber', label: 'Dubber Agent', x: 770, y: 250 },
      { id: 'reviewer', label: 'Reviewer Agent', x: 410, y: 370 },
    ],
  },
];

export const UNKNOWN_DESK: DeskSpec = { id: 'unknown', label: 'Chưa rõ', x: 850, y: 370 };
export const PORTRAIT_OFFICE_SIZE = { w: 560, h: 1120 };
const PORTRAIT_POSITIONS: Record<string, { x: number; y: number }> = {
  media: { x: 40, y: 170 }, render: { x: 200, y: 170 }, publish: { x: 360, y: 170 },
  stt: { x: 40, y: 365 }, tts: { x: 200, y: 365 }, voice_check: { x: 360, y: 365 },
  ad_classifier: { x: 60, y: 575 }, subtitle_detector: { x: 330, y: 575 },
  localizer: { x: 60, y: 745 }, dubber: { x: 330, y: 745 }, reviewer: { x: 195, y: 925 },
};
const PORTRAIT_ROOMS: Record<Exclude<Zone, 'unknown'>, Rect> = {
  media: { x: 20, y: 140, w: 520, h: 180 },
  voice: { x: 20, y: 335, w: 520, h: 180 },
  llm: { x: 20, y: 530, w: 520, h: 560 },
};

export function officeZones(portrait: boolean): ZoneSpec[] {
  return portrait ? ZONES.map((z) => ({ ...z, room: PORTRAIT_ROOMS[z.zone],
    desks: z.desks.map((d) => ({ ...d, ...PORTRAIT_POSITIONS[d.id] })),
  })) : ZONES;
}

export function unknownDesk(portrait: boolean): DeskSpec {
  return portrait ? { ...UNKNOWN_DESK, x: 365, y: 925 } : UNKNOWN_DESK;
}

/** Where a desk's person sits, for drawing a handoff from one desk to another. */
export function deskCenter(id: string, portrait = false): { x: number; y: number } | null {
  const d = [...officeZones(portrait).flatMap((z) => z.desks), unknownDesk(portrait)].find((s) => s.id === id);
  return d ? { x: d.x + STATION.w / 2, y: d.y + 40 } : null;
}

export const ALL_DESKS: DeskSpec[] = ZONES.flatMap((z) => z.desks);

export function deskLabel(id: string): string {
  return [...ALL_DESKS, UNKNOWN_DESK].find((d) => d.id === id)?.label ?? id;
}

/** The decisions that send a video to the gate area, with their labels. */
export const GATE_COLUMNS: Array<{ outcome: GateOutcome; label: string }> = [
  { outcome: 'skipped_ad', label: 'Quảng cáo' },
  { outcome: 'skipped_no_video', label: 'Không có tiếng hoặc hình' },
  { outcome: 'published_original', label: 'Đăng bản gốc' },
  { outcome: 'ready_for_review', label: 'Sẵn sàng review' },
  { outcome: 'needs_human', label: 'Cần người xem' },
];

export function outcomeLabel(outcome: GateOutcome): string {
  return GATE_COLUMNS.find((c) => c.outcome === outcome)?.label ?? outcome;
}
