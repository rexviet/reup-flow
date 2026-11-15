import { Icon } from '../components/Icon';
import type { ActionRunner } from './useDashboard';
import { ACTION_LABELS } from './format';
import type { DashboardActionName } from '../api/types';

/** What the last action did: how many went through and, for each skipped video, the server's reason. */
export function ActionNotice({ runner }: { runner: Pick<ActionRunner, 'result' | 'error' | 'clear'> }) {
  const { result, error, clear } = runner;
  if (!result && !error) return null;
  return (
    <div role="status" data-testid="action-notice" className="rounded-xl bg-surface-container p-3 text-sm ring-1 ring-border-prominent">
      <div className="flex items-start justify-between gap-2">
        {error ? (
          <p className="text-status-error-text">Lỗi: {error}</p>
        ) : result ? (
          <p className="text-text-primary">
            {ACTION_LABELS[result.action as DashboardActionName] ?? result.action}: <strong>{result.done.length}</strong> thành công
            {result.skipped.length > 0 ? `, ${result.skipped.length} bị bỏ qua` : ''}.
          </p>
        ) : null}
        <button type="button" onClick={clear} aria-label="Đóng thông báo" className="inline-flex min-h-8 min-w-8 items-center justify-center rounded text-text-muted hover:text-text-primary">
          <Icon name="close" size={14} />
        </button>
      </div>
      {result && result.skipped.length > 0 ? (
        <ul className="mt-2 list-disc pl-5 text-xs text-text-secondary">
          {result.skipped.map((s) => (
            <li key={s.aweme_id}>
              <span className="font-mono">{s.aweme_id}</span>: {s.reason}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
