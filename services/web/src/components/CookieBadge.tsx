import type { CookieStatus } from '../api/types';

/**
 * phase-08 §8.3: "Badge cookie là thứ đầu tiên cần nhìn khi job fail" -- most
 * `blocked` jobs are a cookie problem, and this badge answers that question
 * in one glance. It must render somewhere always visible (the dashboard shows it next to
 * the download form and in the health row), never tucked into a menu.
 *
 * Three states, not two:
 *   - RED   -- no cookie present at all (`present === false`). This is the
 *     state that answers "why is my job blocked" fastest, so it gets the
 *     loudest color, not amber.
 *   - GREEN -- present and pushed less than 30 minutes ago.
 *   - AMBER -- present but older than 30 minutes (still might work, but old
 *     enough to be worth a second look before assuming a `blocked` job is a
 *     bug in something else).
 *
 * `age_seconds === null` (present but `pushed_at` unparseable/missing --
 * cookie.service.ts's `ageSecondsFrom`) is treated as AMBER, not green: an
 * unknown age is not evidence of freshness.
 */
const FRESH_THRESHOLD_SECONDS = 30 * 60;

export type CookieBadgeState = 'red' | 'amber' | 'green';

export function cookieBadgeState(status: CookieStatus): CookieBadgeState {
  if (!status.present) return 'red';
  if (status.age_seconds === null || status.age_seconds >= FRESH_THRESHOLD_SECONDS) return 'amber';
  return 'green';
}

const STYLES: Record<CookieBadgeState, string> = {
  red: 'bg-status-error-bg text-status-error-text ring-status-error-border',
  amber: 'bg-status-warning-bg text-status-warning-text ring-status-warning-border',
  green: 'bg-status-success-bg text-status-success-text ring-status-success-border',
};

const LABELS: Record<CookieBadgeState, string> = {
  red: 'Cookie: không có',
  amber: 'Cookie: cũ',
  green: 'Cookie: mới',
};

export interface CookieBadgeProps {
  status: CookieStatus | null;
  /** True while the first fetch of /internal/cookie/status is in flight. */
  loading?: boolean;
}

export function CookieBadge({ status, loading }: CookieBadgeProps) {
  if (loading || !status) {
    return (
      <span
        data-testid="cookie-badge"
        data-state="loading"
        className="inline-flex items-center rounded bg-status-idle-bg px-2 py-1 font-mono text-xs font-semibold text-status-idle-text ring-1 ring-inset ring-status-idle-border"
      >
        Cookie: đang tải…
      </span>
    );
  }

  const state = cookieBadgeState(status);
  return (
    <span
      data-testid="cookie-badge"
      data-state={state}
      className={`inline-flex items-center rounded px-2 py-1 font-mono text-xs font-semibold ring-1 ring-inset ${STYLES[state]}`}
      title={status.age_seconds === null ? undefined : `Pushed ${status.age_seconds}s ago`}
    >
      {LABELS[state]}
    </span>
  );
}
