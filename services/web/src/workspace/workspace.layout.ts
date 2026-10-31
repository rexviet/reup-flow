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

export const OFFICE_SIZE = { w: 1000, h: 660 };
export const STATION = { w: 130, h: 100 };

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
      { id: 'localizer', label: 'Localizer', x: 530, y: 250 },
      { id: 'dubber', label: 'Dubber', x: 770, y: 250 },
      { id: 'slot_shortener', label: 'Slot Shortener', x: 170, y: 370 },
      { id: 'reviewer', label: 'Reviewer', x: 410, y: 370 },
      { id: 'review_jev', label: 'Review JEV', x: 650, y: 370 },
    ],
  },
];

export const UNKNOWN_DESK: DeskSpec = { id: 'unknown', label: 'Chưa rõ', x: 850, y: 370 };

/** Where a desk's person sits, for drawing a handoff from one desk to another. */
export function deskCenter(id: string): { x: number; y: number } | null {
  const d = [...ALL_DESKS, UNKNOWN_DESK].find((s) => s.id === id);
  return d ? { x: d.x + STATION.w / 2, y: d.y + 40 } : null;
}

/** The gate bins along the bottom of the office. */
export const GATE_ROOM: Rect = { x: 20, y: 520, w: 960, h: 130 };

export const ALL_DESKS: DeskSpec[] = ZONES.flatMap((z) => z.desks);

export function deskLabel(id: string): string {
  return [...ALL_DESKS, UNKNOWN_DESK].find((d) => d.id === id)?.label ?? id;
}

/** The four decisions that send a video to the gate area, with their labels. */
export const GATE_COLUMNS: Array<{ outcome: GateOutcome; label: string }> = [
  { outcome: 'skipped_ad', label: 'Quảng cáo' },
  { outcome: 'skipped_no_video', label: 'Không có tiếng hoặc hình' },
  { outcome: 'published_original', label: 'Đăng bản gốc' },
  { outcome: 'needs_human', label: 'Cần người xem' },
];

export function outcomeLabel(outcome: GateOutcome): string {
  return GATE_COLUMNS.find((c) => c.outcome === outcome)?.label ?? outcome;
}
