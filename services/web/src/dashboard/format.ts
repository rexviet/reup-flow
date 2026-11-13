import type { IconName } from '../components/Icon';
import type { DashboardActionName, LocalizingStep, Stage, VideoListItem } from '../api/types';

export const STAGE_LABELS: Record<Stage, string> = {
  downloading: 'Đang tải',
  download_failed: 'Tải lỗi',
  held: 'Đang giữ',
  queued: 'Chờ chạy',
  localizing: 'Đang localize',
  dispatch_failed: 'Gửi lỗi',
  run_failed: 'Chạy lỗi',
  skipped: 'Bỏ qua',
  awaiting_review: 'Chờ duyệt',
  returned: 'Đã trả về',
  render_queued: 'Chờ render',
  rendering: 'Đang render',
  render_failed: 'Render lỗi',
  rendered: 'Đã render',
  published: 'Đã đăng',
};

export const STEP_LABELS: Record<LocalizingStep, string> = {
  prepare: 'chuẩn bị',
  translate: 'dịch',
  dub: 'lồng tiếng',
  review: 'kiểm tra',
  unknown: 'không rõ',
};

export type StageTone = 'neutral' | 'active' | 'warn' | 'bad' | 'good';

export const STAGE_TONES: Record<Stage, StageTone> = {
  downloading: 'active',
  download_failed: 'bad',
  held: 'neutral',
  queued: 'neutral',
  localizing: 'active',
  dispatch_failed: 'bad',
  run_failed: 'bad',
  skipped: 'neutral',
  awaiting_review: 'warn',
  returned: 'warn',
  render_queued: 'active',
  rendering: 'active',
  render_failed: 'bad',
  rendered: 'good',
  published: 'good',
};

export const TONE_CLASSES: Record<StageTone, string> = {
  neutral: 'bg-status-idle-bg text-status-idle-text ring-status-idle-border',
  active: 'bg-status-active-bg text-status-active-text ring-status-active-border',
  warn: 'bg-status-warning-bg text-status-warning-text ring-status-warning-border',
  bad: 'bg-status-error-bg text-status-error-text ring-status-error-border',
  good: 'bg-status-success-bg text-status-success-text ring-status-success-border',
};

/** Every stage badge carries an icon as well as a colour, so status never rests on colour alone. */
export const TONE_ICONS: Record<StageTone, IconName> = {
  neutral: 'circle',
  active: 'spinner',
  warn: 'warning',
  bad: 'x-circle',
  good: 'check-circle',
};

export function stageText(item: Pick<VideoListItem, 'stage' | 'step' | 'unknown'>): string {
  const base = STAGE_LABELS[item.stage] ?? item.stage;
  const step = item.step ? ` (${STEP_LABELS[item.step] ?? item.step})` : '';
  return `${base}${step}${item.unknown ? ' ?' : ''}`;
}

export const ACTION_LABELS: Record<DashboardActionName, string> = {
  localize: 'Đưa vào localization',
  retry_download: 'Tải lại',
  resume: 'Chạy tiếp từ bước lỗi',
  rerun: 'Chạy lại',
  approve: 'Duyệt',
  return: 'Trả về',
  rerender: 'Render lại',
  mark_published: 'Đánh dấu đã đăng',
  mark_unpublished: 'Bỏ đánh dấu đã đăng',
  open_studio: 'Mở Studio',
};

/** The buttons worth showing at each stage. The server re-checks every one; this only hides the pointless ones. */
export const ACTIONS_BY_STAGE: Partial<Record<Stage, DashboardActionName[]>> = {
  download_failed: ['retry_download'],
  held: ['localize'],
  dispatch_failed: ['rerun'],
  run_failed: ['resume', 'rerun'],
  queued: ['rerun'],
  awaiting_review: ['open_studio', 'approve', 'return'],
  returned: ['open_studio', 'approve', 'rerun'],
  render_failed: ['rerender'],
  rendered: ['rerender', 'mark_published'],
  published: ['mark_unpublished'],
};

/** Actions that spend GPU time or LLM money and so ask first, naming how many videos. */
export const CONFIRMED_ACTIONS: readonly DashboardActionName[] = ['localize', 'resume', 'rerun', 'approve', 'rerender'];

export function relativeTime(iso: string | null | undefined, nowMs: number = Date.now()): string {
  if (!iso) return '-';
  const ms = nowMs - Date.parse(iso);
  if (Number.isNaN(ms)) return '-';
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s} giây`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} phút`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h} giờ`;
  return `${Math.round(h / 24)} ngày`;
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '-';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '-';
  return d.toLocaleString('vi-VN', { timeZone: 'Asia/Saigon', hour12: false });
}

export function formatUsd(n: number): string {
  return `$${n.toFixed(4)}`;
}

/** `m:ss` or `h:mm:ss` for a video length; null when the length is unknown. */
export function formatDuration(ms: number | null | undefined): string | null {
  if (ms === null || ms === undefined || !Number.isFinite(ms) || ms < 0) return null;
  const total = Math.round(ms / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const sec = String(total % 60).padStart(2, '0');
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`;
}
